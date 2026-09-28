"""低線量CTシミュレーションのコア処理 (GUI/CLI 非依存).

処理の流れ:
    HU → μ → 順投影 → 透過率 → ノイズ付加 → 対数変換 → FBP → HU

元画像は既に撮影線量相当のノイズを含むため、既定では目標線量との
差分ノイズのみを付加する (log領域の分散 1/I_low - 1/I_ref).
また既定ではノイズ成分のみをFBPして元画像に加算するため、
再投影・再構成による解像度低下やCT値のずれは生じない.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime
import json
import os
import warnings
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import astra
import numpy as np
import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from . import __version__

ProgressFn = Callable[[int, int], None]
LogFn = Callable[[str], None]
StopFn = Callable[[], bool]

PARAMS_FILENAME = "ctlowdose_params.json"


###########
###設定###
@dataclass
class SimConfig:
    num_angles: int = 1024               #投影数 (0〜π)
    det_spacing: float = 1.0             #検出器ピッチ [画素]
    mu_water: float = 0.2                #水の線減弱係数 [1/cm]
    photons_per_mAs: float = 3000.0      #1mAsあたりの入射光子数 (校正で決める実効値)
    sigma_readout: float = 5.0           #電子ノイズ標準偏差 [カウント]
    subtract_existing_noise: bool = True #元画像のノイズ分を差し引く
    insert_noise_only: bool = True       #True: ノイズ画像のみFBPして元画像に加算 (解像度・CT値を保持)
                                         #False: 画像全体を再投影→FBPし直す
    eps: float = 1.0                     #log回避用の最小カウント
    filter_type: str = "Ram-Lak"         #FBPフィルタ
    seed: Optional[int] = 0              #乱数シード (Noneで毎回変わる)
    backend: str = "auto"                #"auto" / "gpu" / "cpu"


@dataclass
class DoseSpec:
    mode: str = "ratio"                  #"ratio": 元mAs×ratio, "mAs": 目標mAsを直接指定
    ratio: float = 0.5
    target_mAs: float = 20.0

    def ratio_for(self, mAs_orig: float) -> float:
        if self.mode == "ratio":
            a = float(self.ratio)
        elif self.mode == "mAs":
            a = float(self.target_mAs) / float(mAs_orig)
        else:
            raise ValueError(f"不正な線量モードです: {self.mode}")
        if a <= 0:
            raise ValueError(f"線量比は正の値にしてください: {a}")
        return a

    def describe(self) -> str:
        if self.mode == "ratio":
            return f"x{self.ratio:g}"
        return f"{self.target_mAs:g} mAs"


############
###ASTRA###
def cuda_available() -> bool:
    try:
        return bool(astra.use_cuda())
    except Exception:
        return False


def resolve_backend(backend: str) -> str:
    if backend == "auto":
        return "gpu" if cuda_available() else "cpu"
    if backend == "gpu" and not cuda_available():
        raise RuntimeError("CUDA対応GPUが見つかりません。backend を 'cpu' か 'auto' にしてください")
    if backend not in ("gpu", "cpu"):
        raise ValueError(f"不正な backend です: {backend}")
    return backend


class Reconstructor:
    """1つの画像サイズに対する平行ビームの順投影 / FBP."""

    def __init__(self, shape: Tuple[int, int], cfg: SimConfig, backend: str):
        nrows, ncols = shape
        self.backend = backend
        self.filter_type = cfg.filter_type
        n_det = int(np.ceil(np.hypot(nrows, ncols) / cfg.det_spacing)) + 2   #対角線をカバー
        angles = np.linspace(0, np.pi, cfg.num_angles, endpoint=False)
        self.proj_geom = astra.create_proj_geom("parallel", cfg.det_spacing, n_det, angles)
        self.vol_geom = astra.create_vol_geom(nrows, ncols)
        #CPUは strip がGPU(FBP_CUDA)に最も近いノイズ特性になる
        ptype = "cuda" if backend == "gpu" else "strip"
        self.projector_id = astra.create_projector(ptype, self.proj_geom, self.vol_geom)

    def _alg(self, name: str) -> str:
        return name + "_CUDA" if self.backend == "gpu" else name

    def forward(self, img: np.ndarray) -> np.ndarray:
        #出力: 画像値 × 画素数 の線積分
        vol_id = astra.data2d.create("-vol", self.vol_geom, img.astype(np.float32))
        sino_id = astra.data2d.create("-sino", self.proj_geom)
        cfg = astra.astra_dict(self._alg("FP"))
        cfg["ProjectionDataId"] = sino_id
        cfg["VolumeDataId"] = vol_id
        cfg["ProjectorId"] = self.projector_id
        alg_id = astra.algorithm.create(cfg)
        try:
            astra.algorithm.run(alg_id)
            return astra.data2d.get(sino_id)
        finally:
            astra.algorithm.delete(alg_id)
            astra.data2d.delete([vol_id, sino_id])

    def fbp(self, sino: np.ndarray) -> np.ndarray:
        sino_id = astra.data2d.create("-sino", self.proj_geom, sino.astype(np.float32))
        rec_id = astra.data2d.create("-vol", self.vol_geom)
        cfg = astra.astra_dict(self._alg("FBP"))
        cfg["ProjectionDataId"] = sino_id
        cfg["ReconstructionDataId"] = rec_id
        cfg["ProjectorId"] = self.projector_id
        cfg["FilterType"] = self.filter_type
        alg_id = astra.algorithm.create(cfg)
        try:
            astra.algorithm.run(alg_id)
            return astra.data2d.get(rec_id)
        finally:
            astra.algorithm.delete(alg_id)
            astra.data2d.delete([sino_id, rec_id])

    def close(self) -> None:
        if self.projector_id is not None:
            astra.projector.delete(self.projector_id)
            self.projector_id = None


##################
###シミュレータ###
@dataclass
class CalibrationSample:
    hu: np.ndarray
    pixel_size_cm: float
    mAs: float
    masks: List[np.ndarray]


@dataclass
class CalibrationResult:
    photons_per_mAs: float
    sigma_input: float        #元画像のROI内SD [HU]
    sigma_target: float       #出力画像に残る元ノイズのSD [HU] (校正の目標値)
    sigma_added: float        #最終反復で付加したノイズのSD [HU]
    converged: bool
    history: List[Tuple[float, float]] = field(default_factory=list)   #(photons_per_mAs, σ_add)


class Simulator:
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.backend = resolve_backend(cfg.backend)
        self.rng = np.random.default_rng(cfg.seed)
        self._recons = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        for rec in self._recons.values():
            rec.close()
        self._recons.clear()

    def reconstructor(self, shape: Tuple[int, int]) -> Reconstructor:
        shape = tuple(int(s) for s in shape)
        if shape not in self._recons:
            self._recons[shape] = Reconstructor(shape, self.cfg, self.backend)
        return self._recons[shape]

    def hu_to_mu(self, hu: np.ndarray) -> np.ndarray:
        return self.cfg.mu_water * (1 + np.maximum(hu, -1000) / 1000)

    def mu_to_hu(self, mu: np.ndarray) -> np.ndarray:
        return 1000 * (mu / self.cfg.mu_water - 1)

    def project(self, hu: np.ndarray, pixel_size_cm: float):
        #(線積分[μ×画素], 透過率, Reconstructor) を返す
        rec = self.reconstructor(hu.shape)
        P = rec.forward(self.hu_to_mu(hu))
        return P, np.exp(-P * pixel_size_cm), rec

    def add_noise(self, T: np.ndarray, I0_ref: float, a: float, rng=None):
        #I0_ref: 元画像の入射光子数, a: 線量比。 (I_noisy, I0) を返す
        rng = self.rng if rng is None else rng
        cfg = self.cfg
        I0 = I0_ref * a
        if a >= 1.0:
            I_noisy = I0 * T
        elif cfg.subtract_existing_noise:
            #差分ノイズ: 分散 a(1-a)·I_ref → log領域で 1/I_low - 1/I_ref
            I_ref_cnt = I0_ref * T
            quantum = rng.normal(size=T.shape) * np.sqrt(a * (1 - a) * I_ref_cnt)
            electronic = rng.normal(0.0, cfg.sigma_readout, size=T.shape)
            I_noisy = a * I_ref_cnt + quantum + electronic
        else:
            #元画像をノイズなしとみなしてフルのノイズを付加
            I1 = I0 * T
            electronic = rng.normal(0.0, cfg.sigma_readout, size=T.shape)
            I_noisy = rng.poisson(I1).astype(np.float64) + electronic
        return np.maximum(I_noisy, cfg.eps), I0

    def simulate(self, hu: np.ndarray, pixel_size_cm: float, mAs_orig: float, a: float) -> np.ndarray:
        """1スライスを線量比 a の低線量画像に変換する [HU]."""
        P, T, rec = self.project(hu, pixel_size_cm)
        I_noisy, I0 = self.add_noise(T, self.cfg.photons_per_mAs * mAs_orig, a)
        P_noisy = np.log(I0 / I_noisy) / pixel_size_cm
        if self.cfg.insert_noise_only:
            #FBPは線形なので ノイズ成分だけ再構成して元画像に足す
            return hu + 1000 * rec.fbp(P_noisy - P) / self.cfg.mu_water
        return self.mu_to_hu(rec.fbp(P_noisy))

    def reconstruct_clean(self, hu: np.ndarray, pixel_size_cm: float) -> np.ndarray:
        """ノイズを付加せずに再投影→FBPした画像 [HU]."""
        P, _, rec = self.project(hu, pixel_size_cm)
        return self.mu_to_hu(rec.fbp(P))

    def calibrate(self, samples: Sequence[CalibrationSample], n_realizations: int = 4,
                  max_iter: int = 8, tol: float = 0.02,
                  progress: Optional[ProgressFn] = None, log: Optional[LogFn] = None,
                  should_stop: Optional[StopFn] = None) -> CalibrationResult:
        """元画像と同線量のノイズSDがROI内の元ノイズSDと一致する photons_per_mAs を求める.

        SD ∝ 1/√光子数 より photons_per_mAs ← photons_per_mAs × (σ_add/σ_orig)² を反復する.
        σ_orig は出力画像に残る元ノイズ量: insert_noise_only なら元画像のSD、
        そうでなければ再投影→FBP後のSD (投影・再構成で元ノイズも平滑化されるため).
        self.cfg は変更しない.
        """
        if not samples or not any(len(s.masks) for s in samples):
            raise ValueError("校正用のROIがありません")
        log = log or (lambda msg: None)
        cfg = self.cfg
        rng = np.random.default_rng(None if cfg.seed is None else cfg.seed + 1)   #本処理の乱数列に影響させない

        prepared = []
        var_in, var_target = [], []
        for s in samples:
            P, T, rec = self.project(s.hu, s.pixel_size_cm)
            hu_ref = s.hu if cfg.insert_noise_only else self.mu_to_hu(rec.fbp(P))
            for m in s.masks:
                if m.sum() < 10:
                    raise ValueError("ROIが小さすぎます (10画素未満)")
                var_in.append(float(s.hu[m].var()))
                var_target.append(float(hu_ref[m].var()))
            prepared.append((T, s.pixel_size_cm, s.mAs, rec, s.masks))
        var_orig = float(np.mean(var_target))
        if var_orig <= 0:
            raise ValueError("ROI内のSDが0です。均一でノイズのある領域にROIを置いてください")
        if cfg.insert_noise_only:
            log(f"σ_orig: {np.sqrt(var_orig):.2f} HU")
        else:
            log(f"σ_orig: 元画像 {np.sqrt(np.mean(var_in)):.2f} HU → 再構成後 {np.sqrt(var_orig):.2f} HU")

        def added_noise_var(ppm: float) -> float:
            vs = []
            for _ in range(n_realizations):
                for T, L, mAs, rec, masks in prepared:
                    I_clean = ppm * mAs * T
                    I_noisy = rng.poisson(I_clean).astype(np.float64) \
                        + rng.normal(0.0, cfg.sigma_readout, size=T.shape)
                    I_noisy = np.maximum(I_noisy, cfg.eps)
                    noise_hu = 1000 * rec.fbp(np.log(I_clean / I_noisy) / L) / cfg.mu_water
                    vs.extend(float(noise_hu[m].var()) for m in masks)
            return float(np.mean(vs))

        ppm = float(cfg.photons_per_mAs)
        history = []
        converged = False
        var_add = float("nan")
        for it in range(max_iter):
            if should_stop and should_stop():
                raise InterruptedError("校正を中止しました")
            var_add = added_noise_var(ppm)
            ratio = np.sqrt(var_add / var_orig)
            history.append((ppm, float(np.sqrt(var_add))))
            log(f"iter {it}: photons_per_mAs = {ppm:10.1f}  σ_add = {np.sqrt(var_add):6.2f} HU  ratio = {ratio:.3f}")
            if progress:
                progress(it + 1, max_iter)
            if abs(ratio - 1) < tol:
                converged = True
                break
            ppm *= ratio ** 2
        return CalibrationResult(
            photons_per_mAs=ppm,
            sigma_input=float(np.sqrt(np.mean(var_in))),
            sigma_target=float(np.sqrt(var_orig)),
            sigma_added=float(np.sqrt(var_add)),
            converged=converged,
            history=history,
        )


##########
###ROI###
def circle_mask(shape: Tuple[int, int], row: float, col: float, radius: float) -> np.ndarray:
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (yy - row) ** 2 + (xx - col) ** 2 <= radius ** 2


############
###DICOM###
def _num(ds, keyword: str) -> Optional[float]:
    v = ds.get(keyword)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_mAs(ds) -> Tuple[Optional[float], str]:
    """(mAs, 取得元タグ名) を返す。見つからなければ (None, "")."""
    v = _num(ds, "Exposure")                      #(0018,1152) mAs
    if v:
        return v, "Exposure"
    v = _num(ds, "ExposureInmAs")                 #(0018,9332) mAs
    if v:
        return v, "ExposureInmAs"
    v = _num(ds, "ExposureInuAs")                 #(0018,1153) μAs
    if v:
        return v / 1000.0, "ExposureInuAs"
    t = _num(ds, "ExposureTime")                  #(0018,1150) ms
    for kw in ("XRayTubeCurrent", "XRayTubeCurrentInmA"):
        i = _num(ds, kw)
        if i and t:
            return i * t / 1000.0, f"{kw}×ExposureTime"
    return None, ""


def pixel_size_cm(ds) -> float:
    ps = ds.get("PixelSpacing")
    if not ps:
        raise ValueError("PixelSpacing がありません")
    r, c = float(ps[0]), float(ps[1])
    if abs(r - c) > 1e-3 * max(r, c):
        warnings.warn(f"非正方画素です ({r}, {c})。行方向の値を使用します")
    return r / 10.0


def read_slice(path: str):
    """(Dataset, HU画像[float32]) を返す."""
    ds = pydicom.dcmread(path, force=True)
    hu = ds.pixel_array.astype(np.float32) * float(ds.get("RescaleSlope", 1) or 1) \
        + float(ds.get("RescaleIntercept", 0) or 0)
    return ds, hu


@dataclass
class Series:
    uid: str
    description: str
    paths: List[str]
    mAs: List[Optional[float]]
    rows: int = 0
    cols: int = 0
    pixel_spacing_mm: Optional[float] = None
    kvp: Optional[float] = None
    kernel: str = ""
    manufacturer: str = ""
    positions: List[Optional[float]] = field(default_factory=list)   #スライス位置 z [mm]

    def label(self) -> str:
        desc = self.description or "(no description)"
        return f"{desc}  [{len(self.paths)} slices]"

    def reversed(self) -> "Series":
        """スライス順を逆にしたコピーを返す."""
        return dataclasses.replace(self, paths=self.paths[::-1], mAs=self.mAs[::-1],
                                   positions=self.positions[::-1])


def scan_folder(folder: str) -> List[Series]:
    """フォルダ直下のCT画像DICOMをシリーズごとにまとめ、スライス位置順に並べて返す (枚数の多い順)."""
    groups = {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)   #プリアンブルなしも許容
        except Exception:
            continue
        if "Rows" not in ds or "PixelSpacing" not in ds:
            continue
        uid = str(ds.get("SeriesInstanceUID", "")) or "unknown"
        groups.setdefault(uid, []).append((path, ds))

    def slice_z(ds) -> Optional[float]:
        ipp = ds.get("ImagePositionPatient")
        return float(ipp[2]) if ipp and len(ipp) == 3 else None

    series = []
    for uid, items in groups.items():
        def sort_key(item):
            path, ds = item
            z = slice_z(ds)
            inst = ds.get("InstanceNumber")
            return (z is None, z if z is not None else 0.0,
                    int(inst) if inst not in (None, "") else 0, os.path.basename(path))
        items.sort(key=sort_key)
        ds0 = items[0][1]
        series.append(Series(
            uid=uid,
            description=str(ds0.get("SeriesDescription", "")),
            paths=[p for p, _ in items],
            mAs=[get_mAs(ds)[0] for _, ds in items],
            rows=int(ds0.Rows),
            cols=int(ds0.Columns),
            pixel_spacing_mm=float(ds0.PixelSpacing[0]),
            kvp=_num(ds0, "KVP"),
            kernel=str(ds0.get("ConvolutionKernel", "")),
            manufacturer=str(ds0.get("Manufacturer", "")),
            positions=[slice_z(ds) for _, ds in items],
        ))
    series.sort(key=lambda s: -len(s.paths))
    return series


def save_dicom(ds, path: str) -> None:
    """プリアンブル・ファイルメタ付きの正式なDICOMファイル形式で保存する."""
    try:
        ds.save_as(path, enforce_file_format=True)     #pydicom >= 3
    except TypeError:
        ds.save_as(path, write_like_original=False)    #pydicom 2.x


def _scale_tag(ds, keyword: str, a: float, as_int: bool) -> None:
    v = _num(ds, keyword)
    if v is None:
        return
    v *= a
    setattr(ds, keyword, int(round(v)) if as_int else float(v))


def write_slice(ds, hu: np.ndarray, path: str, series_uid: str, a: float, description: str) -> None:
    """HU画像を元DICOMのヘッダを引き継いで保存する (int16, slope=1, intercept=0)."""
    out = copy.deepcopy(ds)
    arr = np.clip(np.round(hu), -32768, 32767).astype(np.int16)
    if hasattr(out, "set_pixel_data"):   #pydicom >= 3
        out.set_pixel_data(arr, photometric_interpretation="MONOCHROME2", bits_stored=16)
    else:
        if getattr(out, "file_meta", None) is None:
            out.file_meta = FileMetaDataset()
        out.PixelData = arr.tobytes()
        out.Rows, out.Columns = arr.shape
        out.PixelRepresentation = 1
        out.BitsAllocated = 16
        out.BitsStored = 16
        out.HighBit = 15
        out.SamplesPerPixel = 1
        out.PhotometricInterpretation = "MONOCHROME2"
        out.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        out.is_little_endian = True
        out.is_implicit_VR = False
    out.RescaleIntercept = 0
    out.RescaleSlope = 1
    #新しい画素値と矛盾するタグは削除
    for kw in ("PixelPaddingValue", "PixelPaddingRangeLimit",
               "SmallestImagePixelValue", "LargestImagePixelValue"):
        if kw in out:
            delattr(out, kw)
    #撮影条件を模擬線量に合わせる (管電流を下げたとみなす)
    for kw in ("XRayTubeCurrent", "Exposure", "ExposureInuAs"):
        _scale_tag(out, kw, a, as_int=True)
    for kw in ("XRayTubeCurrentInmA", "ExposureInmAs", "CTDIvol"):
        _scale_tag(out, kw, a, as_int=False)
    if "ImageType" in out:
        it = list(out.ImageType)
        out.ImageType = ["DERIVED", "SECONDARY"] + it[2:]
    #元画像と衝突しないようUIDを振り直す
    out.SeriesInstanceUID = series_uid
    out.SOPInstanceUID = generate_uid()
    out.file_meta.MediaStorageSOPInstanceUID = out.SOPInstanceUID
    if "SOPClassUID" in out:
        out.file_meta.MediaStorageSOPClassUID = out.SOPClassUID
    out.SeriesDescription = description[:64]
    save_dicom(out, path)


##############
###一括処理###
def process_series(paths: Sequence[str], out_dir: str, cfg: SimConfig, dose: DoseSpec,
                   default_mAs: Optional[float] = None,
                   progress: Optional[ProgressFn] = None, log: Optional[LogFn] = None,
                   should_stop: Optional[StopFn] = None) -> dict:
    """シリーズ全体を低線量化して out_dir に保存し、処理条件を JSON で残す."""
    log = log or (lambda msg: None)
    if not paths:
        raise ValueError("入力画像がありません")
    in_dirs = {os.path.normcase(os.path.abspath(os.path.dirname(p))) for p in paths}
    if os.path.normcase(os.path.abspath(out_dir)) in in_dirs:
        raise ValueError("出力フォルダは入力フォルダと別にしてください")
    os.makedirs(out_dir, exist_ok=True)

    series_uid = generate_uid()
    description = f"Simulated low dose {dose.describe()}"
    records = []
    warned_high = False
    stopped = False
    with Simulator(cfg) as sim:
        log(f"backend: {sim.backend.upper()}  photons_per_mAs = {cfg.photons_per_mAs:.1f}")
        for i, path in enumerate(paths):
            if should_stop and should_stop():
                log("中止しました")
                stopped = True
                break
            ds, hu = read_slice(path)
            mAs, src = get_mAs(ds)
            if mAs is None:
                if default_mAs is None:
                    raise ValueError(f"{os.path.basename(path)}: mAs情報がありません。仮定値を指定してください")
                mAs, src = float(default_mAs), "default"
            a = dose.ratio_for(mAs)
            if a >= 1.0 and not warned_high:
                log(f"警告: 目標線量が元画像以上です (x{a:.3g})。ノイズは付加されません")
                warned_high = True
            out = sim.simulate(hu, pixel_size_cm(ds), mAs, a)
            name = os.path.basename(path)
            write_slice(ds, out, os.path.join(out_dir, name), series_uid, min(a, 1.0), description)
            records.append({"file": name, "mAs_orig": mAs, "mAs_source": src,
                            "dose_ratio": a, "mAs_simulated": mAs * a})
            if progress:
                progress(i + 1, len(paths))
        backend = sim.backend

    summary = {
        "software": f"ct-lowdose-sim {__version__}",
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "backend": backend,
        "astra_version": getattr(astra, "__version__", ""),
        "config": asdict(cfg),
        "dose": asdict(dose),
        "default_mAs": default_mAs,
        "series_instance_uid": series_uid,
        "completed": not stopped,
        "slices": records,
    }
    with open(os.path.join(out_dir, PARAMS_FILENAME), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log(f"{len(records)} 枚を保存しました → {out_dir}")
    return summary
