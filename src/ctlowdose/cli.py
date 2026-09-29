"""コマンドラインでの一括処理.

例:
    ctlowdose INPUT_DIR OUTPUT_DIR --ratio 0.5 --calibrate-roi 48:280:115:18 --calibrate-roi 48:380:222:10
"""
from __future__ import annotations

import argparse
import sys

from . import __version__, core


def _parse_roi(text: str):
    try:
        s, r, c, rad = (float(v) for v in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"ROIは SLICE:ROW:COL:RADIUS の形式で指定してください: {text}")
    return int(s), r, c, rad


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ctlowdose", description="DICOM CT画像から低線量CT画像を模擬します")
    p.add_argument("input", help="入力DICOMフォルダ")
    p.add_argument("output", help="出力フォルダ")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ratio", type=float, help="線量比 (例: 0.5 = 1/2線量) [既定 0.5]")
    g.add_argument("--target-mAs", type=float, help="目標mAs")
    p.add_argument("--photons-per-mAs", type=float, default=core.SimConfig.photons_per_mAs,
                   help="1mAsあたりの入射光子数 (--calibrate-roi 指定時は校正値で上書き)")
    p.add_argument("--calibrate-roi", type=_parse_roi, action="append", default=[], metavar="S:R:C:RAD",
                   help="校正用ROI (スライス番号は0始まり、行・列・半径は画素)。複数指定可")
    p.add_argument("--default-mAs", type=float, help="DICOMにmAs情報がない場合の仮定値")
    p.add_argument("--series", type=int, default=0, help="フォルダ内に複数シリーズがある場合の番号 (枚数順, 0始まり)")
    p.add_argument("--angles", type=int, default=core.SimConfig.num_angles, help="投影数")
    p.add_argument("--det-spacing", type=float, default=core.SimConfig.det_spacing, help="検出器ピッチ [画素]")
    p.add_argument("--sigma-readout", type=float, default=core.SimConfig.sigma_readout, help="電子ノイズSD [カウント]")
    p.add_argument("--mu-water", type=float, default=None,
                   help="水の線減弱係数 [1/cm] (省略時はDICOMの管電圧から自動)")
    p.add_argument("--full-noise", action="store_true", help="元画像をノイズなしとみなしてフルのノイズを付加する")
    p.add_argument("--reproject", action="store_true",
                   help="ノイズ画像の加算ではなく、画像全体を再投影→FBPし直す (解像度が低下する)")
    p.add_argument("--seed", type=int, default=0, help="乱数シード (-1 でランダム)")
    p.add_argument("--backend", choices=["auto", "gpu", "cpu"], default="auto")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = core.SimConfig(
        num_angles=args.angles,
        det_spacing=args.det_spacing,
        mu_water=args.mu_water,
        photons_per_mAs=args.photons_per_mAs,
        sigma_readout=args.sigma_readout,
        subtract_existing_noise=not args.full_noise,
        insert_noise_only=not args.reproject,
        seed=None if args.seed < 0 else args.seed,
        backend=args.backend,
    )
    if args.target_mAs is not None:
        dose = core.DoseSpec(mode="mAs", target_mAs=args.target_mAs)
    else:
        dose = core.DoseSpec(mode="ratio", ratio=0.5 if args.ratio is None else args.ratio)

    series_list = core.scan_folder(args.input)
    if not series_list:
        print(f"CT画像が見つかりません: {args.input}", file=sys.stderr)
        return 1
    if len(series_list) > 1:
        for i, s in enumerate(series_list):
            print(f"  series {i}: {s.label()}")
    series = series_list[args.series]
    print(f"入力: {series.label()}")

    if args.calibrate_roi:
        samples = {}
        for s_idx, r, c, rad in args.calibrate_roi:
            if s_idx not in samples:
                ds, hu = core.read_slice(series.paths[s_idx])
                mAs, _ = core.get_mAs(ds)
                if mAs is None:
                    if args.default_mAs is None:
                        print("校正スライスにmAs情報がありません。--default-mAs を指定してください", file=sys.stderr)
                        return 1
                    mAs = args.default_mAs
                samples[s_idx] = core.CalibrationSample(hu, core.pixel_size_cm(ds), mAs, [])
            smp = samples[s_idx]
            smp.masks.append(core.circle_mask(smp.hu.shape, r, c, rad))
        with core.Simulator(cfg, kvp=series.kvp) as sim:
            res = sim.calibrate(list(samples.values()), log=print)
        cfg.photons_per_mAs = res.photons_per_mAs
        state = "収束" if res.converged else "未収束"
        print(f"校正結果 ({state}): photons_per_mAs = {res.photons_per_mAs:.1f}")

    core.process_series(series.paths, args.output, cfg, dose, default_mAs=args.default_mAs,
                        progress=lambda i, n: print(f"\r{i}/{n}", end="", flush=True),
                        log=lambda m: print("\n" + m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
