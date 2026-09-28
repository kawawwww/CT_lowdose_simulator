"""合成ファントム/合成DICOMによるテスト (CPUで実行、GPU不要)."""
import json
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from ctlowdose import core

N = 128
PIXEL_MM = 3.0   #小さい画像でも人体程度の大きさ (約38cm) にする


def water_phantom(noise_sd=0.0, seed=0):
    yy, xx = np.mgrid[:N, :N] - (N - 1) / 2
    hu = np.full((N, N), -1000.0, dtype=np.float32)
    hu[xx ** 2 + yy ** 2 < (0.42 * N) ** 2] = 0.0      #水
    hu[xx ** 2 + yy ** 2 < (0.10 * N) ** 2] = 300.0    #高吸収域
    if noise_sd:
        hu += np.random.default_rng(seed).normal(0, noise_sd, hu.shape).astype(np.float32)
    return hu


def cfg(**kw):
    base = dict(num_angles=180, det_spacing=1.0, backend="cpu", sigma_readout=0.0)
    base.update(kw)
    return core.SimConfig(**base)


ROI = (N / 2, N / 2 - 30, 10)   #水領域 (高吸収域の外)


def roi_sd(img, roi=ROI):
    return float(img[core.circle_mask(img.shape, *roi)].std())


def roi_mean(img, roi=ROI):
    return float(img[core.circle_mask(img.shape, *roi)].mean())


def test_dose_spec():
    assert core.DoseSpec("ratio", ratio=0.25).ratio_for(140) == 0.25
    assert core.DoseSpec("mAs", target_mAs=35).ratio_for(140) == pytest.approx(0.25)
    with pytest.raises(ValueError):
        core.DoseSpec("foo").ratio_for(100)


def test_clean_reconstruction_preserves_hu():
    hu = water_phantom()
    with core.Simulator(cfg()) as sim:
        rec = sim.reconstruct_clean(hu, PIXEL_MM / 10)
    assert abs(roi_mean(rec)) < 5
    assert roi_mean(rec, (N / 2, N / 2, 5)) == pytest.approx(300, abs=10)


def test_full_dose_adds_no_noise():
    hu = water_phantom()
    with core.Simulator(cfg()) as sim:
        out = sim.simulate(hu, PIXEL_MM / 10, mAs_orig=100, a=1.0)
    np.testing.assert_allclose(out, hu, atol=1e-3)            #ノイズ加算方式: 元画像そのもの
    with core.Simulator(cfg(insert_noise_only=False)) as sim:
        clean = sim.reconstruct_clean(hu, PIXEL_MM / 10)
        out = sim.simulate(hu, PIXEL_MM / 10, mAs_orig=100, a=1.0)
    np.testing.assert_allclose(out, clean, atol=1e-2)         #再投影方式: 再構成画像


def test_noise_scales_with_dose():
    #フルノイズ方式: SD ∝ 1/√a
    hu = water_phantom()
    c = cfg(subtract_existing_noise=False, photons_per_mAs=2000)
    with core.Simulator(c) as sim:
        sd = {a: roi_sd(sim.simulate(hu, PIXEL_MM / 10, 100, a) - hu) for a in (1.0 - 1e-9, 0.25)}
    assert sd[0.25] / sd[1.0 - 1e-9] == pytest.approx(2.0, rel=0.15)


@pytest.mark.parametrize("insert", [True, False])
def test_calibration_then_half_dose_matches_theory(insert):
    #元ノイズ入りファントムで校正 → 1/2線量の SD ≈ σ_target × √2
    hu = water_phantom(noise_sd=20.0)
    mask = core.circle_mask(hu.shape, *ROI)
    sample = core.CalibrationSample(hu, PIXEL_MM / 10, 100.0, [mask])
    c = cfg(insert_noise_only=insert)
    with core.Simulator(c) as sim:
        res = sim.calibrate([sample], n_realizations=3)
    assert res.converged
    if insert:
        assert res.sigma_target == pytest.approx(res.sigma_input)
    else:
        assert res.sigma_target < res.sigma_input    #再投影で元ノイズは平滑化される
    c.photons_per_mAs = res.photons_per_mAs
    with core.Simulator(c) as sim:
        sds = [roi_sd(sim.simulate(hu, PIXEL_MM / 10, 100.0, 0.5)) for _ in range(4)]
    assert np.mean(sds) == pytest.approx(res.sigma_target * np.sqrt(2), rel=0.1)


def test_no_nan_at_extreme_low_dose():
    hu = water_phantom()
    hu[:, N // 2 - 3:N // 2 + 3] = 3000     #光子飢餓になる金属相当
    with core.Simulator(cfg(photons_per_mAs=1.0)) as sim:
        out = sim.simulate(hu, PIXEL_MM / 10, 1.0, 0.01)
    assert np.isfinite(out).all()


def test_get_mAs_fallbacks():
    ds = Dataset()
    assert core.get_mAs(ds) == (None, "")
    ds.XRayTubeCurrent = 200
    ds.ExposureTime = 700
    assert core.get_mAs(ds)[0] == pytest.approx(140)
    ds.ExposureInuAs = 150000
    assert core.get_mAs(ds)[0] == pytest.approx(150)
    ds.Exposure = 160
    assert core.get_mAs(ds) == (160, "Exposure")


###################
###DICOM入出力###
def make_ct_file(path, hu, z, series_uid, mAs=True):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SeriesInstanceUID = series_uid
    ds.StudyInstanceUID = "1.2.3"
    ds.Modality = "CT"
    ds.SeriesDescription = "test"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.ImagePositionPatient = [0, 0, z]
    ds.InstanceNumber = int(z)
    ds.PixelSpacing = [PIXEL_MM, PIXEL_MM]
    ds.KVP = 120
    if mAs:
        ds.XRayTubeCurrent = 200
        ds.ExposureTime = 500
        ds.Exposure = 100
    ds.Rows, ds.Columns = hu.shape
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.RescaleIntercept = -1024
    ds.RescaleSlope = 1
    ds.PixelData = np.clip(hu + 1024, 0, 4095).astype(np.uint16).tobytes()
    if not hasattr(pydicom.Dataset, "set_pixel_data"):   #pydicom 2.x
        ds.is_little_endian = True
        ds.is_implicit_VR = False
    core.save_dicom(ds, path)


@pytest.fixture
def ct_folder(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    uid = generate_uid()
    hu = water_phantom(noise_sd=15.0)
    for i, z in enumerate([30.0, 10.0, 20.0]):     #ファイル名順とスライス位置順を変える
        make_ct_file(str(src / f"img{i}.dcm"), hu, z, uid)
    (src / "notes.txt").write_text("not dicom")
    return src


def test_scan_folder_sorts_by_position(ct_folder):
    series = core.scan_folder(str(ct_folder))
    assert len(series) == 1
    s = series[0]
    assert [os.path.basename(p) for p in s.paths] == ["img1.dcm", "img2.dcm", "img0.dcm"]
    assert s.mAs == [100.0] * 3
    assert s.kvp == 120


def test_process_series_writes_valid_dicom(ct_folder, tmp_path):
    out = tmp_path / "out"
    s = core.scan_folder(str(ct_folder))[0]
    summary = core.process_series(s.paths, str(out), cfg(photons_per_mAs=5000),
                                  core.DoseSpec("mAs", target_mAs=25))
    assert summary["completed"]
    assert summary["slices"][0]["dose_ratio"] == pytest.approx(0.25)
    params = json.loads((out / core.PARAMS_FILENAME).read_text(encoding="utf-8"))
    assert params["config"]["photons_per_mAs"] == 5000

    orig = pydicom.dcmread(s.paths[0])
    new = pydicom.dcmread(str(out / os.path.basename(s.paths[0])))
    assert new.SOPInstanceUID != orig.SOPInstanceUID
    assert new.SeriesInstanceUID != orig.SeriesInstanceUID
    assert int(new.Exposure) == 25
    assert int(new.XRayTubeCurrent) == 50
    assert new.ImageType[0] == "DERIVED"
    hu = new.pixel_array.astype(np.float32) * float(new.RescaleSlope) + float(new.RescaleIntercept)
    assert abs(roi_mean(hu)) < 15
    assert roi_sd(hu) > 5


def test_process_series_rejects_same_folder(ct_folder):
    s = core.scan_folder(str(ct_folder))[0]
    with pytest.raises(ValueError):
        core.process_series(s.paths, str(ct_folder), cfg(), core.DoseSpec())


def test_process_series_requires_mAs(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    make_ct_file(str(src / "a.dcm"), water_phantom(), 0.0, generate_uid(), mAs=False)
    s = core.scan_folder(str(src))[0]
    with pytest.raises(ValueError):
        core.process_series(s.paths, str(tmp_path / "out"), cfg(), core.DoseSpec())
    summary = core.process_series(s.paths, str(tmp_path / "out"), cfg(), core.DoseSpec(), default_mAs=100)
    assert summary["slices"][0]["mAs_source"] == "default"
