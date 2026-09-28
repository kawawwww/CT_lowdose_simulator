"""デスクトップGUI (PySide6 / PyQt6 / PyQt5 のいずれかで動作)."""
from __future__ import annotations

import os
import sys
import traceback
from collections import OrderedDict

import matplotlib

matplotlib.use("QtAgg")
import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.backends.qt_compat import QtCore, QtWidgets
from matplotlib.figure import Figure
from matplotlib.patches import Circle

from . import __version__, core

Signal = getattr(QtCore, "Signal", None) or getattr(QtCore, "pyqtSignal")
Qt = QtCore.Qt

WINDOW_PRESETS = [
    ("軟部 (W400/L40)", 400, 40),
    ("肝臓 (W150/L60)", 150, 60),
    ("肺 (W1500/L-600)", 1500, -600),
    ("骨 (W2000/L400)", 2000, 400),
    ("脳 (W80/L40)", 80, 40),
]


def _exec(obj):
    return obj.exec() if hasattr(obj, "exec") else obj.exec_()


################
###ワーカー###
class TaskThread(QtCore.QThread):
    """重い処理をバックグラウンドで実行する。fn(thread) の戻り値を succeeded で返す."""
    progress = Signal(int, int)
    message = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._stop = False

    def request_stop(self):
        self._stop = True

    def stop_requested(self) -> bool:
        return self._stop

    def run(self):
        try:
            self.succeeded.emit(self._fn(self))
        except InterruptedError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


################
###画像表示###
class SliceCanvas(FigureCanvasQTAgg):
    clicked = Signal(float, float, int)   #row, col, button
    scrolled = Signal(int)                #+1 / -1

    def __init__(self, parent=None):
        self.fig = Figure(figsize=(8, 5))
        super().__init__(self.fig)
        self.setParent(parent)
        self.toolbar = None
        self.mpl_connect("button_press_event", self._on_click)
        self.mpl_connect("scroll_event", self._on_scroll)

    def show_images(self, panels, window, level, rois):
        """panels: [(image, title)], rois: [(row, col, radius, label)]."""
        self.fig.clear()
        if not panels:
            self.draw_idle()
            return
        axes = self.fig.subplots(1, len(panels), sharex=True, sharey=True, squeeze=False)[0]
        vmin, vmax = level - window / 2, level + window / 2
        for ax, (img, title) in zip(axes, panels):
            ax.imshow(img, cmap="gray", vmin=vmin, vmax=vmax, interpolation="nearest")
            ax.set_title(title, fontsize=9)
            ax.set_axis_off()
            for r, c, rad, label in rois:
                ax.add_patch(Circle((c, r), rad, fill=False, ec="red", lw=1.2))
                ax.text(c + rad + 3, r, label, color="yellow", fontsize=8, va="center")
        self.fig.subplots_adjust(left=0.01, right=0.99, top=0.94, bottom=0.01, wspace=0.03)
        self.draw_idle()

    def _on_click(self, ev):
        if ev.inaxes is None or ev.xdata is None:
            return
        if self.toolbar is not None and str(self.toolbar.mode):   #ズーム/パン中はROI操作しない
            return
        self.clicked.emit(float(ev.ydata), float(ev.xdata), int(ev.button))

    def _on_scroll(self, ev):
        self.scrolled.emit(1 if ev.button == "up" else -1)


##################
###メイン画面###
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"CT Low-Dose Simulator {__version__}")
        self.resize(1400, 900)
        self.settings = QtCore.QSettings("ct-lowdose-sim", "ct-lowdose-sim")

        self.series_list = []
        self.series = None
        self.cache = OrderedDict()      #index -> (ds, hu)
        self.rois = []                  #dict(slice, row, col, radius)
        self.preview = None             #(slice, hu_sim, a)
        self.thread = None
        self.calibrated = False

        self._build_ui()
        self._restore_settings()
        self._update_backend_label()
        self._update_dose_label()

    # ---------- UI構築 ----------
    def _build_ui(self):
        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        #左: 設定パネル
        panel = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(panel)
        pl.addWidget(self._build_io_group())
        pl.addWidget(self._build_dose_group())
        pl.addWidget(self._build_calib_group())
        pl.addWidget(self._build_advanced_group())
        pl.addWidget(self._build_run_group())
        pl.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(440)
        splitter.addWidget(scroll)

        #右: 画像ビューア + ログ
        right = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        viewer = QtWidgets.QWidget()
        vl = QtWidgets.QVBoxLayout(viewer)
        self.canvas = SliceCanvas(viewer)
        self.toolbar = NavigationToolbar2QT(self.canvas, viewer)
        self.canvas.toolbar = self.toolbar
        self.canvas.clicked.connect(self._on_canvas_click)
        self.canvas.scrolled.connect(lambda d: self.slider.setValue(self.slider.value() + d))

        nav = QtWidgets.QHBoxLayout()
        self.slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_slice_changed)
        self.lbl_slice = QtWidgets.QLabel("- / -")
        self.lbl_slice.setMinimumWidth(80)
        self.cb_window = QtWidgets.QComboBox()
        for name, w, l in WINDOW_PRESETS:
            self.cb_window.addItem(name, (w, l))
        self.cb_window.currentIndexChanged.connect(lambda _: self.refresh_view())
        nav.addWidget(QtWidgets.QLabel("スライス"))
        nav.addWidget(self.slider, 1)
        nav.addWidget(self.lbl_slice)
        nav.addWidget(QtWidgets.QLabel("表示"))
        nav.addWidget(self.cb_window)

        hint = QtWidgets.QLabel("左クリック: ROI追加 / 右クリック: ROI削除 / ホイール: スライス送り")
        hint.setStyleSheet("color: gray;")
        vl.addWidget(self.toolbar)
        vl.addWidget(self.canvas, 1)
        vl.addLayout(nav)
        vl.addWidget(hint)
        right.addWidget(viewer)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        right.addWidget(self.log_view)
        right.setStretchFactor(0, 4)
        right.setStretchFactor(1, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        self.lbl_backend = QtWidgets.QLabel()
        self.statusBar().addPermanentWidget(self.lbl_backend)

    def _build_io_group(self):
        g = QtWidgets.QGroupBox("入出力")
        f = QtWidgets.QFormLayout(g)
        self.ed_input = QtWidgets.QLineEdit()
        self.ed_input.setReadOnly(True)
        btn_in = QtWidgets.QPushButton("参照…")
        btn_in.clicked.connect(self._choose_input)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.ed_input, 1)
        row.addWidget(btn_in)
        f.addRow("入力フォルダ", row)

        self.cb_series = QtWidgets.QComboBox()
        self.cb_series.currentIndexChanged.connect(self._on_series_changed)
        f.addRow("シリーズ", self.cb_series)
        self.lbl_info = QtWidgets.QLabel("-")
        self.lbl_info.setWordWrap(True)
        f.addRow("撮影条件", self.lbl_info)

        self.ed_output = QtWidgets.QLineEdit()
        btn_out = QtWidgets.QPushButton("参照…")
        btn_out.clicked.connect(self._choose_output)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.ed_output, 1)
        row.addWidget(btn_out)
        f.addRow("出力フォルダ", row)
        return g

    def _build_dose_group(self):
        g = QtWidgets.QGroupBox("目標線量")
        grid = QtWidgets.QGridLayout(g)
        self.rb_ratio = QtWidgets.QRadioButton("線量比")
        self.rb_mAs = QtWidgets.QRadioButton("目標mAs")
        self.rb_ratio.setChecked(True)
        self.sp_ratio = QtWidgets.QDoubleSpinBox()
        self.sp_ratio.setRange(0.01, 1.0)
        self.sp_ratio.setDecimals(3)
        self.sp_ratio.setSingleStep(0.05)
        self.sp_ratio.setValue(0.5)
        self.sp_target = QtWidgets.QDoubleSpinBox()
        self.sp_target.setRange(0.1, 10000)
        self.sp_target.setDecimals(1)
        self.sp_target.setValue(20)
        self.sp_target.setSuffix(" mAs")
        self.sp_default_mAs = QtWidgets.QDoubleSpinBox()
        self.sp_default_mAs.setRange(1, 10000)
        self.sp_default_mAs.setValue(200)
        self.sp_default_mAs.setSuffix(" mAs")
        self.sp_default_mAs.setToolTip("DICOMにmAs情報がない場合に元画像のmAsとみなす値")
        self.lbl_dose = QtWidgets.QLabel("-")
        grid.addWidget(self.rb_ratio, 0, 0)
        grid.addWidget(self.sp_ratio, 0, 1)
        grid.addWidget(self.rb_mAs, 1, 0)
        grid.addWidget(self.sp_target, 1, 1)
        grid.addWidget(QtWidgets.QLabel("mAs不明時の仮定値"), 2, 0)
        grid.addWidget(self.sp_default_mAs, 2, 1)
        grid.addWidget(self.lbl_dose, 3, 0, 1, 2)
        for w in (self.rb_ratio, self.rb_mAs):
            w.toggled.connect(self._update_dose_label)
        for w in (self.sp_ratio, self.sp_target, self.sp_default_mAs):
            w.valueChanged.connect(self._update_dose_label)
        return g

    def _build_calib_group(self):
        g = QtWidgets.QGroupBox("ノイズ量の校正")
        v = QtWidgets.QVBoxLayout(g)
        note = QtWidgets.QLabel("肝実質・筋肉など均一な領域をクリックしてROIを置き、校正を実行します。"
                                "元画像のノイズSDから photons_per_mAs を決めます。")
        note.setWordWrap(True)
        v.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("ROI半径"))
        self.sp_roi_radius = QtWidgets.QSpinBox()
        self.sp_roi_radius.setRange(3, 100)
        self.sp_roi_radius.setValue(15)
        self.sp_roi_radius.setSuffix(" px")
        row.addWidget(self.sp_roi_radius)
        row.addStretch(1)
        v.addLayout(row)
        self.lst_roi = QtWidgets.QListWidget()
        self.lst_roi.setMaximumHeight(120)
        self.lst_roi.itemDoubleClicked.connect(self._on_roi_double_clicked)
        v.addWidget(self.lst_roi)
        row = QtWidgets.QHBoxLayout()
        btn_del = QtWidgets.QPushButton("選択ROIを削除")
        btn_del.clicked.connect(self._delete_selected_roi)
        btn_clear = QtWidgets.QPushButton("全削除")
        btn_clear.clicked.connect(self._clear_rois)
        row.addWidget(btn_del)
        row.addWidget(btn_clear)
        v.addLayout(row)
        self.btn_calib = QtWidgets.QPushButton("校正を実行")
        self.btn_calib.clicked.connect(self.run_calibration)
        v.addWidget(self.btn_calib)
        form = QtWidgets.QFormLayout()
        self.sp_ppm = QtWidgets.QDoubleSpinBox()
        self.sp_ppm.setRange(1, 1e9)
        self.sp_ppm.setDecimals(1)
        self.sp_ppm.setValue(core.SimConfig.photons_per_mAs)
        self.sp_ppm.setToolTip("1mAsあたりの入射光子数 (実効値)。校正で自動設定されます")
        self.sp_ppm.valueChanged.connect(self._invalidate_preview)
        form.addRow("photons_per_mAs", self.sp_ppm)
        v.addLayout(form)
        self.lbl_calib = QtWidgets.QLabel("未校正")
        self.lbl_calib.setWordWrap(True)
        v.addWidget(self.lbl_calib)
        return g

    def _build_advanced_group(self):
        g = QtWidgets.QGroupBox("詳細設定")
        g.setCheckable(True)
        g.setChecked(False)
        inner = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(inner)
        d = core.SimConfig()
        self.sp_angles = QtWidgets.QSpinBox()
        self.sp_angles.setRange(16, 4096)
        self.sp_angles.setValue(d.num_angles)
        self.sp_det = QtWidgets.QDoubleSpinBox()
        self.sp_det.setRange(0.25, 4.0)
        self.sp_det.setSingleStep(0.25)
        self.sp_det.setValue(d.det_spacing)
        self.sp_det.setSuffix(" px")
        self.sp_sigma = QtWidgets.QDoubleSpinBox()
        self.sp_sigma.setRange(0, 1000)
        self.sp_sigma.setValue(d.sigma_readout)
        self.sp_mu = QtWidgets.QDoubleSpinBox()
        self.sp_mu.setRange(0.01, 2.0)
        self.sp_mu.setDecimals(4)
        self.sp_mu.setSingleStep(0.005)
        self.sp_mu.setValue(d.mu_water)
        self.sp_mu.setSuffix(" /cm")
        self.chk_subtract = QtWidgets.QCheckBox("元画像のノイズ分を差し引く (推奨)")
        self.chk_subtract.setChecked(d.subtract_existing_noise)
        self.chk_insert = QtWidgets.QCheckBox("ノイズ画像を元画像に加算 (推奨: 解像度・CT値を保持)")
        self.chk_insert.setChecked(d.insert_noise_only)
        self.chk_insert.setToolTip("オフにすると画像全体を再投影→FBPし直します (解像度が低下します)")
        self.sp_seed = QtWidgets.QSpinBox()
        self.sp_seed.setRange(-1, 2 ** 31 - 1)
        self.sp_seed.setValue(d.seed)
        self.sp_seed.setToolTip("-1 で毎回ランダム")
        self.cb_backend = QtWidgets.QComboBox()
        self.cb_backend.addItem("自動", "auto")
        self.cb_backend.addItem("GPU (CUDA)", "gpu")
        self.cb_backend.addItem("CPU", "cpu")
        self.cb_backend.currentIndexChanged.connect(self._update_backend_label)
        f.addRow("投影数", self.sp_angles)
        f.addRow("検出器ピッチ", self.sp_det)
        f.addRow("電子ノイズSD [count]", self.sp_sigma)
        f.addRow("水のμ", self.sp_mu)
        f.addRow(self.chk_subtract)
        f.addRow(self.chk_insert)
        f.addRow("乱数シード", self.sp_seed)
        f.addRow("計算", self.cb_backend)
        note = QtWidgets.QLabel("※ 詳細設定を変えたら校正をやり直してください")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        f.addRow(note)
        lay = QtWidgets.QVBoxLayout(g)
        lay.addWidget(inner)
        inner.setVisible(False)
        g.toggled.connect(inner.setVisible)
        for w in (self.sp_angles, self.sp_det, self.sp_sigma, self.sp_mu, self.sp_seed):
            w.valueChanged.connect(self._invalidate_preview)
        self.chk_subtract.toggled.connect(self._invalidate_preview)
        self.chk_insert.toggled.connect(self._invalidate_preview)
        return g

    def _build_run_group(self):
        g = QtWidgets.QGroupBox("実行")
        v = QtWidgets.QVBoxLayout(g)
        row = QtWidgets.QHBoxLayout()
        self.btn_preview = QtWidgets.QPushButton("プレビュー (表示中のスライス)")
        self.btn_preview.clicked.connect(self.run_preview)
        row.addWidget(self.btn_preview)
        v.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("一括処理して保存")
        self.btn_run.clicked.connect(self.run_batch)
        self.btn_stop = QtWidgets.QPushButton("中止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_task)
        row.addWidget(self.btn_run, 1)
        row.addWidget(self.btn_stop)
        v.addLayout(row)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setValue(0)
        v.addWidget(self.progress)
        return g

    # ---------- 設定の保存/復元 ----------
    def _restore_settings(self):
        out = self.settings.value("output_dir", "")
        if out:
            self.ed_output.setText(str(out))
        last_in = self.settings.value("input_dir", "")
        if last_in and os.path.isdir(str(last_in)):
            self.ed_input.setText(str(last_in))
            QtCore.QTimer.singleShot(0, lambda: self.load_folder(str(last_in)))

    def closeEvent(self, ev):
        if self.thread is not None and self.thread.isRunning():
            self.thread.request_stop()
            self.thread.wait(10000)
        self.settings.setValue("input_dir", self.ed_input.text())
        self.settings.setValue("output_dir", self.ed_output.text())
        super().closeEvent(ev)

    # ---------- 共通 ----------
    def log(self, msg: str):
        self.log_view.appendPlainText(msg)

    def current_config(self) -> core.SimConfig:
        seed = self.sp_seed.value()
        return core.SimConfig(
            num_angles=self.sp_angles.value(),
            det_spacing=self.sp_det.value(),
            mu_water=self.sp_mu.value(),
            photons_per_mAs=self.sp_ppm.value(),
            sigma_readout=self.sp_sigma.value(),
            subtract_existing_noise=self.chk_subtract.isChecked(),
            insert_noise_only=self.chk_insert.isChecked(),
            seed=None if seed < 0 else seed,
            backend=self.cb_backend.currentData(),
        )

    def current_dose(self) -> core.DoseSpec:
        if self.rb_ratio.isChecked():
            return core.DoseSpec(mode="ratio", ratio=self.sp_ratio.value())
        return core.DoseSpec(mode="mAs", target_mAs=self.sp_target.value())

    def slice_mAs(self, idx: int) -> float:
        m = self.series.mAs[idx] if self.series else None
        return float(m) if m else self.sp_default_mAs.value()

    def load_slice(self, idx: int):
        if idx in self.cache:
            self.cache.move_to_end(idx)
            return self.cache[idx]
        item = core.read_slice(self.series.paths[idx])
        self.cache[idx] = item
        if len(self.cache) > 24:
            self.cache.popitem(last=False)
        return item

    def _update_backend_label(self, *_):
        try:
            backend = core.resolve_backend(self.cb_backend.currentData())
            text = "GPU (CUDA)" if backend == "gpu" else "CPU"
        except Exception as e:
            text = f"エラー: {e}"
        self.lbl_backend.setText(f"ASTRA {getattr(core.astra, '__version__', '')} / {text}")
        self._invalidate_preview()

    def _update_dose_label(self, *_):
        dose = self.current_dose()
        if not self.series:
            self.lbl_dose.setText(f"設定: {dose.describe()}")
            return
        known = [m for m in self.series.mAs if m]
        if known:
            lo, hi = min(known), max(known)
            src = f"{lo:g}" if lo == hi else f"{lo:g}〜{hi:g}"
        else:
            lo = hi = self.sp_default_mAs.value()
            src = f"{lo:g} (仮定)"
        try:
            a_lo, a_hi = dose.ratio_for(lo), dose.ratio_for(hi)
        except ValueError as e:
            self.lbl_dose.setText(str(e))
            return
        a_text = f"x{a_lo:.3g}" if abs(a_lo - a_hi) < 1e-9 else f"x{min(a_lo, a_hi):.3g}〜{max(a_lo, a_hi):.3g}"
        text = f"元 {src} mAs → 模擬 {lo * a_lo:.3g}" + ("" if lo == hi else f"〜{hi * a_hi:.3g}") + f" mAs ({a_text})"
        if max(a_lo, a_hi) >= 1.0:
            text += "\n⚠ 目標が元画像以上の線量です。ノイズは付加されません"
        self.lbl_dose.setText(text)
        self._invalidate_preview()

    def _invalidate_preview(self, *_):
        if self.preview is not None:
            self.preview = None
            self.refresh_view()
            self._refresh_roi_list()

    def _set_busy(self, busy: bool):
        for w in (self.btn_calib, self.btn_preview, self.btn_run):
            w.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)

    def _start_task(self, fn, on_success, label: str):
        if self.thread is not None and self.thread.isRunning():
            return
        self._set_busy(True)
        self.progress.setValue(0)
        self.statusBar().showMessage(label)
        th = TaskThread(fn, self)
        th.progress.connect(lambda i, n: (self.progress.setMaximum(n), self.progress.setValue(i)))
        th.message.connect(self.log)
        th.succeeded.connect(on_success)
        th.failed.connect(self._on_task_failed)
        th.finished.connect(lambda: (self._set_busy(False), self.statusBar().clearMessage()))
        self.thread = th
        th.start()

    def _stop_task(self):
        if self.thread is not None:
            self.thread.request_stop()
            self.log("中止を要求しました…")

    def _on_task_failed(self, msg: str):
        self.log(f"エラー: {msg}")
        QtWidgets.QMessageBox.warning(self, "エラー", msg.split("\n\n")[0])

    # ---------- 入力 ----------
    def _choose_input(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "入力DICOMフォルダ", self.ed_input.text())
        if d:
            self.load_folder(d)

    def _choose_output(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "出力フォルダ", self.ed_output.text())
        if d:
            self.ed_output.setText(d)

    def load_folder(self, folder: str):
        self.ed_input.setText(folder)
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.series_list = core.scan_folder(folder)
        except Exception as e:
            self.series_list = []
            self.log(f"読み込みエラー: {e}")
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.cb_series.blockSignals(True)
        self.cb_series.clear()
        for s in self.series_list:
            self.cb_series.addItem(s.label())
        self.cb_series.blockSignals(False)
        if not self.series_list:
            self.series = None
            self.lbl_info.setText("CT画像が見つかりません")
            self.slider.setEnabled(False)
            self.refresh_view()
            return
        self.log(f"{folder}: {len(self.series_list)} シリーズ")
        self._on_series_changed(0)

    def _on_series_changed(self, idx: int):
        if idx < 0 or idx >= len(self.series_list):
            return
        self.series = self.series_list[idx]
        self.cache.clear()
        self.rois.clear()
        self.preview = None
        s = self.series
        known = [m for m in s.mAs if m]
        if known:
            lo, hi = min(known), max(known)
            mAs_text = f"{lo:g} mAs" if lo == hi else f"{lo:g}〜{hi:g} mAs (管電流変調)"
            if len(known) < len(s.mAs):
                mAs_text += f"  ※{len(s.mAs) - len(known)}枚は不明"
        else:
            mAs_text = "不明 (仮定値を使用)"
        parts = [f"{len(s.paths)}枚  {s.rows}×{s.cols}  {s.pixel_spacing_mm:.3f} mm/px", mAs_text]
        extra = []
        if s.kvp:
            extra.append(f"{s.kvp:g} kVp")
        if s.kernel:
            extra.append(f"kernel {s.kernel}")
        if s.manufacturer:
            extra.append(s.manufacturer)
        if extra:
            parts.append("  ".join(extra))
        self.lbl_info.setText("\n".join(parts))
        self.slider.blockSignals(True)
        self.slider.setRange(0, len(s.paths) - 1)
        self.slider.setValue(len(s.paths) // 2)
        self.slider.blockSignals(False)
        self.slider.setEnabled(True)
        self._refresh_roi_list()
        self._update_dose_label()
        self._on_slice_changed(self.slider.value())

    def _on_slice_changed(self, idx: int):
        if self.series:
            self.lbl_slice.setText(f"{idx + 1} / {len(self.series.paths)}")
        self.refresh_view()

    # ---------- 表示 ----------
    def refresh_view(self):
        if not self.series:
            self.canvas.show_images([], 400, 40, [])
            return
        idx = self.slider.value()
        try:
            _, hu = self.load_slice(idx)
        except Exception as e:
            self.log(f"読み込みエラー: {e}")
            return
        w, l = self.cb_window.currentData()
        mAs = self.slice_mAs(idx)
        panels = [(hu, f"original  {mAs:g} mAs")]
        if self.preview is not None and self.preview[0] == idx:
            sim, a = self.preview[1], self.preview[2]
            panels.append((sim, f"simulated  {mAs * a:.3g} mAs (x{a:.3g})"))
        #画像上はROI番号のみ。統計値はROI一覧に表示
        rois = [(r["row"], r["col"], r["radius"], str(i + 1))
                for i, r in enumerate(self.rois) if r["slice"] == idx]
        self.canvas.show_images(panels, w, l, rois)

    @staticmethod
    def _roi_stats(img, roi) -> str:
        m = core.circle_mask(img.shape, roi["row"], roi["col"], roi["radius"])
        return f"{img[m].mean():.0f}±{img[m].std():.1f}"

    # ---------- ROI ----------
    def _on_canvas_click(self, row: float, col: float, button: int):
        if not self.series:
            return
        idx = self.slider.value()
        if button == 1:
            self.rois.append({"slice": idx, "row": round(row), "col": round(col),
                              "radius": self.sp_roi_radius.value()})
        elif button == 3:
            cand = [(np.hypot(r["row"] - row, r["col"] - col), i) for i, r in enumerate(self.rois)
                    if r["slice"] == idx]
            cand = [c for c in cand if c[0] <= self.rois[c[1]]["radius"]]
            if not cand:
                return
            del self.rois[min(cand)[1]]
        else:
            return
        self._refresh_roi_list()
        self.refresh_view()

    def _refresh_roi_list(self):
        self.lst_roi.clear()
        for i, r in enumerate(self.rois):
            text = f"#{i + 1}  スライス{r['slice'] + 1} ({r['row']},{r['col']}) r={r['radius']}"
            try:
                _, hu = self.load_slice(r["slice"])
                text += f"   元 {self._roi_stats(hu, r)}"
            except Exception:
                pass
            if self.preview is not None and self.preview[0] == r["slice"]:
                text += f"  → 模擬 {self._roi_stats(self.preview[1], r)}"
            self.lst_roi.addItem(text)

    def _on_roi_double_clicked(self, item):
        r = self.rois[self.lst_roi.row(item)]
        self.slider.setValue(r["slice"])

    def _delete_selected_roi(self):
        row = self.lst_roi.currentRow()
        if 0 <= row < len(self.rois):
            del self.rois[row]
            self._refresh_roi_list()
            self.refresh_view()

    def _clear_rois(self):
        self.rois.clear()
        self._refresh_roi_list()
        self.refresh_view()

    # ---------- 校正 ----------
    def run_calibration(self):
        if not self.series:
            return
        if not self.rois:
            QtWidgets.QMessageBox.information(self, "校正", "画像をクリックして均一な領域にROIを置いてください")
            return
        cfg = self.current_config()
        rois = list(self.rois)
        paths = {r["slice"]: self.series.paths[r["slice"]] for r in rois}
        mAs = {s: self.slice_mAs(s) for s in paths}   #ウィジェットはGUIスレッドで読む

        def task(th):
            samples = {}
            for r in rois:
                if r["slice"] not in samples:
                    ds, hu = core.read_slice(paths[r["slice"]])
                    samples[r["slice"]] = core.CalibrationSample(
                        hu, core.pixel_size_cm(ds), mAs[r["slice"]], [])
                smp = samples[r["slice"]]
                smp.masks.append(core.circle_mask(smp.hu.shape, r["row"], r["col"], r["radius"]))
            with core.Simulator(cfg) as sim:
                th.message.emit(f"校正開始 ({sim.backend.upper()}, ROI {len(rois)}個)")
                return sim.calibrate(list(samples.values()), progress=th.progress.emit,
                                     log=th.message.emit, should_stop=th.stop_requested)

        self._start_task(task, self._on_calibrated, "校正中…")

    def _on_calibrated(self, res):
        self.sp_ppm.setValue(res.photons_per_mAs)
        self.calibrated = True
        state = "収束" if res.converged else "未収束 (ROIを見直してください)"
        sd_text = f"元画像SD {res.sigma_input:.1f} HU"
        if abs(res.sigma_target - res.sigma_input) > 0.05:
            sd_text += f" → 再構成後 {res.sigma_target:.1f} HU"
        text = (f"校正済み ({state})\n{sd_text}\n"
                f"photons_per_mAs = {res.photons_per_mAs:.1f}")
        self.lbl_calib.setText(text)
        self.log(text.replace("\n", "  "))

    # ---------- プレビュー ----------
    def run_preview(self):
        if not self.series:
            return
        idx = self.slider.value()
        cfg = self.current_config()
        dose = self.current_dose()
        mAs = self.slice_mAs(idx)
        path = self.series.paths[idx]

        def task(th):
            ds, hu = core.read_slice(path)
            a = dose.ratio_for(mAs)
            with core.Simulator(cfg) as sim:
                return idx, sim.simulate(hu, core.pixel_size_cm(ds), mAs, a), a

        def done(result):
            self.preview = result
            if self.slider.value() != result[0]:
                self.slider.setValue(result[0])
            self.refresh_view()
            self._refresh_roi_list()

        self._start_task(task, done, "プレビュー計算中…")

    # ---------- 一括処理 ----------
    def run_batch(self):
        if not self.series:
            return
        out_dir = self.ed_output.text().strip()
        if not out_dir:
            QtWidgets.QMessageBox.information(self, "一括処理", "出力フォルダを指定してください")
            return
        yes = QtWidgets.QMessageBox.StandardButton.Yes
        no = QtWidgets.QMessageBox.StandardButton.No
        if not self.calibrated:
            ans = QtWidgets.QMessageBox.question(
                self, "一括処理", "ノイズ量の校正をしていません。現在の photons_per_mAs のまま処理しますか？",
                yes | no, no)
            if ans != yes:
                return
        if os.path.isdir(out_dir) and os.listdir(out_dir):
            ans = QtWidgets.QMessageBox.question(
                self, "一括処理", f"出力フォルダは空ではありません。同名ファイルは上書きされます。\n{out_dir}\n続行しますか？",
                yes | no, no)
            if ans != yes:
                return
        cfg = self.current_config()
        dose = self.current_dose()
        paths = list(self.series.paths)
        default_mAs = self.sp_default_mAs.value()
        self.settings.setValue("output_dir", out_dir)

        def task(th):
            return core.process_series(paths, out_dir, cfg, dose, default_mAs=default_mAs,
                                       progress=th.progress.emit, log=th.message.emit,
                                       should_stop=th.stop_requested)

        def done(summary):
            n = len(summary["slices"])
            if summary["completed"]:
                QtWidgets.QMessageBox.information(self, "一括処理", f"{n} 枚を保存しました\n{out_dir}")

        self._start_task(task, done, "一括処理中…")


def main(argv=None) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv if argv is None else argv)
    w = MainWindow()
    w.show()
    return _exec(app)


if __name__ == "__main__":
    raise SystemExit(main())
