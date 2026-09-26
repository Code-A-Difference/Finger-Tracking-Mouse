"""Finger Mouse: control the desktop pointer with one hand and a webcam.

This file is the window. The work happens elsewhere, each piece on its own
thread so none can freeze another:

    camera.py           capture thread: newest frame only, reconnects itself
    tracking_engine.py  tracking thread: hand landmarks -> gestures
    gesture_state.py    what a hand means (click, drag, scroll, hide)
    pointer_output.py   output thread: the real OS pointer, always released
    diagnostics.py      logs, and a watchdog that names whatever stalls
    app_settings.py     settings: validated, migrated, saved atomically

The window polls the engine's latest snapshot on a timer instead of being
sent every frame, so a slow repaint can never back up the tracker.

Run ``python Finger_tracker.py --self-test [result.json]`` to check an
installed copy without a camera: it loads the hand model, runs it once, and
checks the pointer backend and Qt.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

try:
    import numpy as np
    from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, QUrl
    from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QGuiApplication, QIcon, QImage,
                               QKeySequence, QPainter, QPen, QPixmap, QShortcut)
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                                   QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                                   QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider,
                                   QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget)

    import app_settings
    from app_settings import Settings
    from app_version import APP_NAME, APP_VERSION, PUBLISHER, SOURCE_URL
    from camera import CameraCapabilities, CameraInfo, OpenCVCamera, list_cameras, probe_camera_indices, \
        probe_resolutions, validate_stream_url
    from diagnostics import Heartbeat, Watchdog, setup_logging
    from gesture_state import GazeCalibration
    from pointer_output import PointerOutput, create_backend
    from tracking_engine import TrackingEngine
except ImportError as exc:
    raise SystemExit(
        "Finger Mouse is missing a dependency. Install the packages from "
        "requirements.txt, then run this file again.\n"
        f"Details: {exc}"
    ) from exc

log = logging.getLogger("finger_mouse")

ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"
ICON_PATH = ASSETS / "icon.png"

GESTURE_COLORS = {
    "ready": "#fbbf24", "pinched": "#34d399", "dragging": "#38bdf8", "scrolling": "#a78bfa",
    "paused": "#f87171", "hide": "#f87171", "no_hand": "#8290a6", "open": "#cbd5e1",
    "pointing": "#cbd5e1", "starting": "#8290a6",
    "gazing": "#cbd5e1", "dwelling": "#fbbf24", "no_face": "#8290a6", "uncalibrated": "#f87171",
}


def app_icon() -> QIcon:
    return QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()


def native_to_logical(x: int, y: int) -> QPoint:
    """Turn native desktop coordinates into Qt's.

    On Windows the pointer works in physical pixels while Qt lays out in
    scaled ones, per screen; Qt keeps each screen's top-left corner in native
    units and scales from there. macOS points and X11 pixels already match Qt.
    """
    if sys.platform != "win32":
        return QPoint(x, y)
    for screen in QGuiApplication.screens():
        g = screen.geometry()
        dpr = screen.devicePixelRatio()
        native = QRect(g.topLeft(), QSize(round(g.width() * dpr), round(g.height() * dpr)))
        if native.contains(x, y):
            return QPoint(g.x() + round((x - g.x()) / dpr), g.y() + round((y - g.y()) / dpr))
    return QPoint(x, y)


# ---------------------------------------------------------------------------
# The halo that follows the pointer
# ---------------------------------------------------------------------------

class HaloOverlay(QWidget):
    """A small, click-through ring that follows the tracked pointer.

    It's a window the size of the ring that moves, not a transparent window
    over the whole desktop that repaints: moving a small window costs next to
    nothing, while repainting a desktop-sized translucent window every frame
    was enough to stall the whole app on large or multiple screens.
    """

    def __init__(self) -> None:
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.Tool
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setWindowTitle("Finger Mouse pointer halo")
        self.color = QColor(34, 211, 238)
        self.set_diameter(36)

    def set_diameter(self, diameter: int) -> None:
        self._diameter = diameter
        self.setFixedSize(diameter + 8, diameter + 8)
        self.update()

    def set_color(self, color: QColor) -> None:
        if color != self.color:
            self.color = color
            self.update()

    def place(self, logical: QPoint) -> None:
        self.move(logical.x() - self.width() // 2, logical.y() - self.height() // 2)
        if not self.isVisible():
            self.show()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 (Qt name)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QColor(self.color)
        c.setAlpha(235)
        p.setPen(QPen(c, 2))
        c.setAlpha(45)
        p.setBrush(c)
        r = self._diameter // 2
        center = QPoint(self.width() // 2, self.height() // 2)
        p.drawEllipse(center, r, r)
        c.setAlpha(220)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(center, 3, 3)


class CalibrationDialog(QDialog):
    """Look at nine dots in turn; fits gaze -> screen from what the eye
    tracker saw while each one was up.

    Needs eye tracking already running (MainWindow checks before opening
    this): it reads ``engine.last_gaze_offset`` on a timer, the same way the
    main window reads ``engine.view`` — nothing here touches the tracking
    thread directly.
    """

    POINTS = [(0.1, 0.1), (0.5, 0.1), (0.9, 0.1), (0.1, 0.5), (0.5, 0.5),
              (0.9, 0.5), (0.1, 0.9), (0.5, 0.9), (0.9, 0.9)]
    SETTLE_MS = 700     # give the eye time to actually get there before sampling
    SAMPLE_MS = 500     # then collect readings for this long

    def __init__(self, main: "MainWindow") -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.main = main
        self.setModal(True)
        left, top, width, height = main.output.backend.desktop_rect(main.settings.screen)
        self.setGeometry(left, top, width, height)
        self.setStyleSheet("background: #05070d;")
        self.samples: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self._index = 0
        self._readings: list[tuple[float, float]] = []

        self.hint = QLabel(self)
        self.hint.setStyleSheet("color: #aebbd0; font-size: 15px; background: transparent;")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.hint.setGeometry(0, height - 60, width, 40)

        self.dot = QLabel(self)
        self.dot.setFixedSize(28, 28)
        self.dot.setStyleSheet("background: #22d3ee; border-radius: 14px; border: 3px solid white;")

        self._settle = QTimer(self, singleShot=True, interval=self.SETTLE_MS)
        self._settle.timeout.connect(self._start_sampling)
        self._sample_timer = QTimer(self, interval=33)
        self._sample_timer.timeout.connect(self._sample)
        self._finish_sample = QTimer(self, singleShot=True, interval=self.SAMPLE_MS)
        self._finish_sample.timeout.connect(self._next_point)

        self._show_point()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    def _show_point(self) -> None:
        fx, fy = self.POINTS[self._index]
        x = int(fx * self.width()) - self.dot.width() // 2
        y = int(fy * self.height()) - self.dot.height() // 2
        self.dot.move(x, y)
        self.dot.show()
        self.hint.setText(f"Look at the dot… {self._index + 1} of {len(self.POINTS)}. Esc cancels.")
        self._readings = []
        self._settle.start()

    def _start_sampling(self) -> None:
        self._sample_timer.start()
        self._finish_sample.start()

    def _sample(self) -> None:
        engine = self.main.engine
        offset = engine.last_gaze_offset if engine is not None else None
        if offset is not None:
            self._readings.append(offset)

    def _next_point(self) -> None:
        self._sample_timer.stop()
        if self._readings:
            ox = sum(o[0] for o in self._readings) / len(self._readings)
            oy = sum(o[1] for o in self._readings) / len(self._readings)
            self.samples.append(((ox, oy), self.POINTS[self._index]))
        self._index += 1
        if self._index >= len(self.POINTS):
            self._finish()
        else:
            self._show_point()

    def _finish(self) -> None:
        self.dot.hide()
        if len(self.samples) < GazeCalibration.MIN_SAMPLES:
            QMessageBox.warning(self, "Calibration incomplete",
                "Finger Mouse couldn't see your eyes for enough of that. Make sure your face is well lit "
                "and centred in the camera, then try again.")
            self.reject()
            return
        cal = GazeCalibration()
        if not cal.fit(self.samples):
            QMessageBox.warning(self, "Calibration didn't take",
                "That didn't produce a usable mapping. Try again, keeping your head still and looking "
                "only at each dot as it appears.")
            self.reject()
            return
        self.main.change_settings(eye_calibration=cal.to_json())
        self.accept()


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(QDialog):
    """Every setting, in tabs. Changes apply to tracking at once and are saved."""

    def __init__(self, main: "MainWindow") -> None:
        super().__init__(main)
        self.main = main
        self.setWindowTitle(f"{APP_NAME} settings")
        self.setMinimumWidth(560)
        self._loading = True
        self.controls: dict[str, Any] = {}

        tabs = QTabWidget()
        for build, name in ((self._pointer_tab, "Pointer"), (self._click_tab, "Click && drag"),
                            (self._scroll_tab, "Scrolling"), (self._eye_tab, "Eye tracking"),
                            (self._camera_tab, "Camera"), (self._gestures_tab, "Hide gesture"),
                            (self._advanced_tab, "Advanced")):
            # Each page scrolls rather than squeezing its text when the window is short.
            scroll = QScrollArea()
            scroll.setObjectName("pageScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(build())
            tabs.addTab(scroll, name)
        self.resize(620, 680)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        reset = buttons.addButton("Restore defaults", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self._restore_defaults)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)
        self.load(main.settings)
        self._loading = False

    # -- building blocks ------------------------------------------------------
    def _page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        return page, layout

    def _hint(self, layout: QVBoxLayout, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hint")
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
        layout.addWidget(label)
        return label

    def _slider(self, layout: QVBoxLayout, key: str, label: str, lo: int, hi: int,
                fmt: str = "{}", hint: str = "", step: int = 1) -> QSlider:
        row = QHBoxLayout()
        title = QLabel(label)
        value = QLabel()
        value.setObjectName("value")
        row.addWidget(title)
        row.addStretch(1)
        row.addWidget(value)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setSingleStep(step)
        slider.setPageStep(max(step, (hi - lo) // 10))
        slider.setAccessibleName(label)
        title.setBuddy(slider)

        def changed(v: int) -> None:
            value.setText(fmt.format(v))
            self._set(key, v)

        slider.valueChanged.connect(changed)
        layout.addLayout(row)
        layout.addWidget(slider)
        if hint:
            self._hint(layout, hint)
        self.controls[key] = (slider, value, fmt)
        return slider

    def _check(self, layout: QVBoxLayout, key: str, label: str, hint: str = "") -> QCheckBox:
        box = QCheckBox(label)
        box.toggled.connect(lambda on: self._set(key, on))
        layout.addWidget(box)
        if hint:
            self._hint(layout, hint)
        self.controls[key] = box
        return box

    def _choice(self, layout: QVBoxLayout, key: str, label: str, options: list[tuple[str, str]],
                hint: str = "") -> QComboBox:
        row = QHBoxLayout()
        title = QLabel(label)
        combo = QComboBox()
        for value, text in options:
            combo.addItem(text, value)
        combo.setAccessibleName(label)
        title.setBuddy(combo)
        combo.currentIndexChanged.connect(lambda _i: self._set(key, combo.currentData()))
        row.addWidget(title)
        row.addStretch(1)
        row.addWidget(combo)
        layout.addLayout(row)
        if hint:
            self._hint(layout, hint)
        self.controls[key] = combo
        return combo

    # -- tabs ---------------------------------------------------------------
    def _pointer_tab(self) -> QWidget:
        page, l = self._page()
        self._choice(l, "tracking_mode", "Tracking mode",
                     [("hand", "Hand gestures"), ("eye", "Eye gaze (beta)")],
                     "Move the pointer with a hand pinching to click, or by looking at the screen. "
                     "The settings below are for hand mode; eye mode has its own tab.")
        self._slider(l, "smoothing", "Smoothing", 0, 100, "{}%",
                     "Higher holds the pointer steadier; lower follows faster. Small hand tremors are "
                     "smoothed away more strongly than real movement.")
        self._slider(l, "reach", "Hand reach", 50, 100, "{}% of the camera view",
                     "How much of the camera picture your fingertip sweeps to cover the whole screen. "
                     "Lower means less arm movement; the preview shows this area as a box.")
        self._choice(l, "screen", "Screen", [("primary", "Main screen"), ("all", "All screens together")])
        self._slider(l, "halo_size", "Tracking halo size", 16, 80, "{} px")
        self._check(l, "show_halo", "Show the halo around the pointer")
        self._check(l, "pause_on_physical_mouse", "Pause hand control while I use my mouse or trackpad",
                    "When you move your real mouse, hand control steps aside for a moment — "
                    "also a quick way to reach the Stop button.")
        sys_cursor = QPushButton("System pointer size…")
        sys_cursor.clicked.connect(self.main.open_system_cursor_settings)
        l.addWidget(sys_cursor, 0, Qt.AlignmentFlag.AlignLeft)
        l.addStretch(1)
        return page

    def _click_tab(self) -> QWidget:
        page, l = self._page()
        self._hint(l, "<b>Quick pinch</b> (thumb and index): left-click where the pinch began. "
                      "<b>Pinch and hold</b>: press and hold the button — move to drag, open your fingers "
                      "to drop. The pointer stays still while a click is being decided.")
        self._check(l, "click_enabled", "Pinch to click")
        self._slider(l, "pinch_threshold", "Pinch closes at", 10, 60, "{}% of hand size",
                     "How close thumb and index must come. Raise it if pinches are missed; lower it if "
                     "clicks happen before you mean them.")
        self._slider(l, "pinch_release_gap", "Opens again at", 5, 40, "+{}%",
                     "How much further apart they must open to let go. A wider gap ignores more wobble.")
        self._slider(l, "click_stability", "Click steadiness", 1, 5, "{} frames",
                     "How many camera frames must agree before a pinch or release counts.")
        self._check(l, "drag_enabled", "Pinch and hold to drag")
        self._slider(l, "pinch_hold_ms", "Hold for", 150, 1500, "{} ms", step=10,
                     hint="How long to hold a pinch before it becomes a press-and-hold.")
        l.addStretch(1)
        return page

    def _scroll_tab(self) -> QWidget:
        page, l = self._page()
        self._check(l, "scroll_enabled", "Scroll with a hand pose")
        self._choice(l, "scroll_pose", "Pose",
                     [("two_fingers", "Index and middle up (recommended)"), ("index", "Index finger only")],
                     "Hold the pose still for a moment to start. Then move up or down: the further from "
                     "where you started, the faster it scrolls. Lower your fingers to stop. "
                     "“Index only” is close to how most people point, so it scrolls by accident more.")
        self._slider(l, "scroll_sensitivity", "Speed", 5, 100, "{}%")
        self._slider(l, "scroll_dead_zone", "Dead zone", 5, 60, "{}% of hand size",
                     "How far to move before scrolling starts, so small wobbles don't scroll.")
        self._check(l, "scroll_reverse", "Reverse direction")
        l.addStretch(1)
        return page

    def _eye_tab(self) -> QWidget:
        page, l = self._page()
        self._hint(l, "Look at the screen to move the pointer instead of using your hand. Needs a short "
                      "calibration first, and is happiest when your head stays roughly still and facing "
                      "the camera — a head turn can throw it off more than a hand-tracking wobble would.")
        self._choice(l, "eye_click_mode", "Click by",
                     [("dwell", "Holding your gaze still (recommended)"),
                      ("blink", "A deliberate blink"),
                      ("both", "Either one")])
        self._slider(l, "eye_dwell_ms", "Dwell time", 300, 2500, "{} ms", step=50,
                     hint="How long a steady gaze takes to click.")
        self._slider(l, "eye_dwell_radius", "Dwell steadiness", 1, 15, "{}% of the screen",
                     hint="How far your gaze may drift and still count as “still”.")
        self._slider(l, "eye_blink_ms", "Blink hold time", 100, 800, "{} ms", step=25,
                     hint="How long an eye must stay shut to count as a deliberate blink, not an ordinary one.")
        self._slider(l, "eye_smoothing", "Smoothing", 0, 100, "{}%",
                     hint="Gaze tracking is noisier than hand tracking, so this usually wants to sit higher.")
        l.addSpacing(6)
        self.calibration_status = QLabel()
        self.calibration_status.setObjectName("value")
        l.addWidget(self.calibration_status)
        self.calibrate_button = QPushButton("Calibrate…")
        self.calibrate_button.clicked.connect(self.main.open_calibration)
        l.addWidget(self.calibrate_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._hint(l, "Calibrating needs tracking already running in eye mode: pick Eye gaze above, close "
                      "this window, press Start tracking, then open Settings again to calibrate.")
        l.addStretch(1)
        return page

    def _camera_tab(self) -> QWidget:
        page, l = self._page()
        row = QHBoxLayout()
        title = QLabel("Camera")
        self.camera_combo = QComboBox()
        self.camera_combo.setAccessibleName("Camera")
        self.camera_combo.currentIndexChanged.connect(self._camera_chosen)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.main.refresh_cameras)
        row.addWidget(title)
        row.addWidget(self.camera_combo, 1)
        row.addWidget(refresh)
        l.addLayout(row)

        self.stream_row = QWidget()
        self.stream_row.setObjectName("page")
        sl = QVBoxLayout(self.stream_row)
        sl.setContentsMargins(0, 0, 0, 0)
        self.stream_url = QLineEdit()
        self.stream_url.setPlaceholderText("http://192.168.1.20:8080/video")
        self.stream_url.setAccessibleName("Stream address")
        self.stream_url.editingFinished.connect(self._stream_url_done)
        sl.addWidget(QLabel("Stream address"))
        sl.addWidget(self.stream_url)
        self.stream_error = QLabel()
        self.stream_error.setObjectName("error")
        sl.addWidget(self.stream_error)
        self._hint(sl, "Experimental. Phone apps such as “IP Webcam” show an address like this while "
                       "they run. Use it on a network you trust — plain http video isn't encrypted.")
        l.addWidget(self.stream_row)

        self._hint(l, "<b>Using a phone as the camera:</b> apps such as DroidCam or Iriun, and iPhone "
                      "Continuity Camera on a Mac, add the phone to this list as a regular camera. "
                      "Cameras marked “virtual” are software cameras; Automatic tries real ones first.")

        self.camera_info = QLabel("Start tracking to see what the camera delivers.")
        self.camera_info.setObjectName("value")
        self.camera_info.setWordWrap(True)
        l.addWidget(self.camera_info)

        res_row = QHBoxLayout()
        self.resolution = self._choice(l, "camera_resolution", "Resolution",
                                       [(r, "Automatic (1280×720 if offered)" if r == "auto" else r.replace("x", "×"))
                                        for r in app_settings.RESOLUTIONS])
        self.detect_modes = QPushButton("Check which the camera supports")
        self.detect_modes.clicked.connect(self._detect_modes)
        res_row.addWidget(self.detect_modes)
        res_row.addStretch(1)
        l.addLayout(res_row)
        self.modes_note = QLabel()
        self.modes_note.setObjectName("hint")
        self.modes_note.setWordWrap(True)
        l.addWidget(self.modes_note)

        zoom_row = QHBoxLayout()
        self.zoom = QDoubleSpinBox()
        self.zoom.setRange(0, 1000)
        self.zoom.setDecimals(0)
        self.zoom.setAccessibleName("Camera zoom")
        self.zoom.valueChanged.connect(lambda v: None if self._loading else self._set("camera_zoom", v))
        self.zoom_label = QLabel("Zoom")
        zoom_row.addWidget(self.zoom_label)
        zoom_row.addWidget(self.zoom)
        self.driver_button = QPushButton("Camera driver settings…")
        self.driver_button.clicked.connect(lambda: self.main.camera_command("driver_settings"))
        zoom_row.addStretch(1)
        zoom_row.addWidget(self.driver_button)
        l.addLayout(zoom_row)
        self._hint(l, "Field of view is set by the camera's lens; software can't widen it. Some cameras "
                      "show a wider area in 16:9 modes such as 1280×720, so try those. Zoom and the driver "
                      "settings window appear only when your camera's driver really offers them.")
        self._check(l, "mirror", "Mirror the picture (move right, pointer goes right)")
        l.addStretch(1)
        return page

    def _gestures_tab(self) -> QWidget:
        page, l = self._page()
        self._hint(l, "An optional shortcut: hold up only your middle finger for a moment to stop tracking "
                      "and put Finger Mouse away. It's off unless you turn it on. It only affects Finger "
                      "Mouse — it never closes or touches any other app.")
        box = QCheckBox("Enable the hide gesture")
        box.toggled.connect(self._hide_toggled)
        self.controls["hide_gesture_enabled"] = box
        l.addWidget(box)
        self._choice(l, "hide_gesture_action", "When it's made",
                     [("tray", "Stop tracking and hide to the tray"),
                      ("minimize", "Stop tracking and minimise the window"),
                      ("quit", "Stop tracking and quit Finger Mouse")])
        self._slider(l, "hide_gesture_hold_ms", "Hold for", 600, 3000, "{} ms", step=100,
                     hint="Longer is safer against doing it by accident.")
        l.addStretch(1)
        return page

    def _advanced_tab(self) -> QWidget:
        page, l = self._page()
        self._check(l, "show_landmarks", "Show hand landmarks in the preview")
        self._check(l, "show_gesture_state", "Show the gesture state in the preview")
        self._check(l, "show_diagnostics", "Show diagnostics (frame rates, timings, stalls)")
        self._check(l, "verbose_logging", "Detailed logging",
                    "Writes more detail to the log file. Useful when reporting a problem.")
        logs = QPushButton("Open the log folder")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.main.log_dir))))
        l.addWidget(logs, 0, Qt.AlignmentFlag.AlignLeft)
        about = QLabel(f"{APP_NAME} {APP_VERSION} · {PUBLISHER} · "
                       f"<a href='{SOURCE_URL}'>source code</a>")
        about.setOpenExternalLinks(True)
        about.setObjectName("hint")
        l.addStretch(1)
        l.addWidget(about)
        return page

    # -- state ------------------------------------------------------------------
    def load(self, s: Settings) -> None:
        was = self._loading
        self._loading = True
        for key, control in self.controls.items():
            value = getattr(s, key)
            if isinstance(control, tuple):
                slider, label, fmt = control
                slider.setValue(int(value))
                label.setText(fmt.format(int(value)))
            elif isinstance(control, QCheckBox):
                control.setChecked(bool(value))
            elif isinstance(control, QComboBox):
                i = control.findData(value)
                control.setCurrentIndex(max(0, i))
        self.stream_url.setText(s.stream_url)
        self.populate_cameras()
        self.update_camera_info()
        self.update_calibration_status()
        self._loading = was

    def _set(self, key: str, value: Any) -> None:
        if self._loading:
            return
        self.main.change_settings(**{key: value})

    def populate_cameras(self) -> None:
        was = self._loading
        self._loading = True
        combo = self.camera_combo
        combo.clear()
        combo.addItem("Automatic", "auto")
        for cam in self.main.cameras:
            combo.addItem(cam.display_name, cam.id)
        combo.addItem("Phone or network stream (experimental)…", "url")
        i = combo.findData(self.main.settings.camera)
        combo.setCurrentIndex(max(0, i))
        self.stream_row.setVisible(self.main.settings.camera == "url")
        self._loading = was

    def _camera_chosen(self, _i: int) -> None:
        value = self.camera_combo.currentData()
        self.stream_row.setVisible(value == "url")
        if value == "url" and not self.main.settings.stream_url:
            self.stream_url.setFocus()
            return   # wait for an address before switching
        self._set("camera", value)

    def _stream_url_done(self) -> None:
        url = self.stream_url.text().strip()
        problem = validate_stream_url(url) if url else None
        self.stream_error.setText(problem or "")
        if not problem and url:
            self.main.change_settings(stream_url=url, camera="url")

    def update_camera_info(self) -> None:
        caps = self.main.capabilities
        tracking = self.main.is_tracking()
        self.detect_modes.setEnabled(not tracking and self.main.settings.camera.startswith("cv:"))
        self.detect_modes.setToolTip("" if not tracking else "Stop tracking first; checking modes needs the camera.")
        if caps is None:
            self.camera_info.setText("Start tracking to see what the camera delivers.")
            self.zoom.setVisible(False)
            self.zoom_label.setText("Zoom: shown once the camera is running")
            self.driver_button.setVisible(False)
            return
        fps = f" · {caps.fps:.0f} fps" if caps.fps else ""
        notes = (" " + " ".join(caps.notes)) if caps.notes else ""
        self.camera_info.setText(f"Now receiving {caps.width}×{caps.height}{fps} via {caps.backend}.{notes}")
        if caps.zoom is None:
            self.zoom.setVisible(False)
            self.zoom_label.setText("Zoom: not offered by this camera's driver")
        else:
            self.zoom.setVisible(True)
            self.zoom_label.setText("Zoom (the driver's own units)")
            if not self.zoom.hasFocus():
                self._loading, was = True, self._loading
                self.zoom.setValue(caps.zoom)
                self._loading = was
        self.driver_button.setVisible(caps.driver_settings)

    def update_calibration_status(self) -> None:
        calibrated = bool(self.main.settings.eye_calibration)
        self.calibration_status.setText("Calibrated ✓" if calibrated else "Not calibrated yet")
        ready = self.main.is_tracking() and self.main.settings.tracking_mode == "eye"
        self.calibrate_button.setEnabled(ready)
        self.calibrate_button.setToolTip(
            "" if ready else "Switch to Eye gaze mode and press Start tracking first.")

    def _detect_modes(self) -> None:
        cam_id = self.main.settings.camera
        if not cam_id.startswith("cv:") or self.main.is_tracking():
            return
        index = int(cam_id[3:])
        self.detect_modes.setEnabled(False)
        self.modes_note.setText("Checking… the camera light may flicker.")
        candidates = tuple(r for r in app_settings.RESOLUTIONS if r != "auto")
        self.main.run_in_background(lambda: probe_resolutions(OpenCVCamera(index), candidates),
                                    self._modes_found)

    def _modes_found(self, modes: Any) -> None:
        self.detect_modes.setEnabled(True)
        if isinstance(modes, Exception) or not modes:
            self.modes_note.setText("The camera didn't report any modes. It may be in use by another app.")
            return
        self.modes_note.setText("This camera delivers: " + ", ".join(m.replace("x", "×") for m in modes) +
                                ". Other choices fall back to its nearest mode.")

    def _hide_toggled(self, on: bool) -> None:
        if self._loading:
            return
        if on and not self.main.settings.hide_gesture_confirmed:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Turn on the hide gesture?")
            box.setText("Holding up only your middle finger will stop tracking and put Finger Mouse away.")
            box.setInformativeText(
                "It can happen by accident if you make that shape while working. It only affects "
                "Finger Mouse and never closes other apps. To bring Finger Mouse back, use its tray icon "
                "or open it again. You can turn this off here at any time.")
            enable = box.addButton("Turn it on", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not enable:
                control = self.controls["hide_gesture_enabled"]
                control.blockSignals(True)
                control.setChecked(False)
                control.blockSignals(False)
                return
            self.main.change_settings(hide_gesture_confirmed=True, hide_gesture_enabled=True)
            return
        self.main.change_settings(hide_gesture_enabled=on)

    def _restore_defaults(self) -> None:
        answer = QMessageBox.question(self, "Restore defaults?",
                                      "Put every setting back to how it was when Finger Mouse was installed?")
        if answer == QMessageBox.StandardButton.Yes:
            self.main.replace_settings(Settings())
            self.load(self.main.settings)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, log_dir: Path) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.settings = app_settings.load()
        self.cameras: list[CameraInfo] = []
        self.capabilities: Optional[CameraCapabilities] = None
        self.engine: Optional[TrackingEngine] = None
        self.output: Optional[PointerOutput] = None
        self.settings_dialog: Optional[SettingsDialog] = None
        self._results: "queue.Queue[tuple[Any, Any]]" = queue.Queue()
        self._output_events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._preview_seq = -1
        self._last_tick = time.monotonic()
        self._tick_times: list[float] = []
        self._fatal: Optional[str] = None
        self._quitting = False
        self._notice: Optional[tuple[str, str, float]] = None   # (chip, text, until)

        self.overlay = HaloOverlay()
        self.overlay.set_diameter(self.settings.halo_size)
        self.watchdog = Watchdog()
        self.watchdog.watch(Heartbeat("ui", self._ui_busy_since, 0.6, "window not responding"))
        self.watchdog.start()

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(640, 600)
        self.resize(820, 760)
        self._build()
        self._apply_style()
        self._build_tray()

        self.save_timer = QTimer(self, singleShot=True, interval=400)
        self.save_timer.timeout.connect(lambda: app_settings.save(self.settings))
        self.tick = QTimer(self, interval=16)      # ~60 Hz: halo, preview, events
        self.tick.timeout.connect(self._on_tick)
        self.tick.start()
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.stop_tracking)
        self.refresh_cameras()

    # -- layout -----------------------------------------------------------------
    def _build(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("subtitle")
        self.subtitle.setWordWrap(True)
        self._update_subtitle()
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self.open_settings)
        header.addWidget(settings_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        self.preview = QLabel("The camera picture appears here once tracking starts.")
        self.preview.setObjectName("preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(300)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.preview.setAccessibleName("Camera preview")
        layout.addWidget(self.preview, 1)

        state_row = QHBoxLayout()
        self.chip = QLabel("Stopped")
        self.chip.setObjectName("chip")
        self.status = QLabel("Choose a camera and start tracking to control the pointer.")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Status")
        state_row.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)
        state_row.addWidget(self.status, 1)
        layout.addLayout(state_row)

        self.diag = QLabel()
        self.diag.setObjectName("diag")
        self.diag.setWordWrap(True)
        self.diag.setVisible(self.settings.show_diagnostics)
        layout.addWidget(self.diag)

        controls = QHBoxLayout()
        camera_label = QLabel("Camera")
        self.camera_combo = QComboBox()
        self.camera_combo.setAccessibleName("Camera")
        self.camera_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.camera_combo.currentIndexChanged.connect(self._quick_camera)
        camera_label.setBuddy(self.camera_combo)
        self.start_button = QPushButton("Start tracking")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self.toggle_tracking)
        controls.addWidget(camera_label)
        controls.addWidget(self.camera_combo)
        controls.addStretch(1)
        controls.addWidget(self.start_button)
        layout.addLayout(controls)

        foot = QLabel("Esc stops tracking. Moving your real mouse pauses hand control for a moment.")
        foot.setObjectName("footnote")
        foot.setWordWrap(True)
        layout.addWidget(foot)
        self.setCentralWidget(central)

    def _update_subtitle(self) -> None:
        if self.settings.tracking_mode == "eye":
            self.subtitle.setText("Look at the screen to move the pointer. Hold your gaze still (or blink) "
                                  "to click — calibrate first in Settings → Eye tracking.")
        else:
            self.subtitle.setText("Point with your index finger. Pinch to click, pinch and hold to drag, "
                                  "two fingers up to scroll.")

    def _apply_style(self) -> None:
        check = (ASSETS / "check.png").as_posix()
        self.setStyleSheet(f"""
            QWidget {{ background: #0c1220; color: #e8edf6; font-size: 14px; }}
            QLabel#title {{ font-size: 28px; font-weight: 700; color: #f4f7fb; }}
            QLabel#subtitle, QLabel#hint {{ color: #aebbd0; }}
            QLabel#hint {{ font-size: 12px; }}
            QLabel#preview {{ background: #080d17; border: 1px solid #263248; border-radius: 12px; color: #8290a6; }}
            QLabel#value {{ color: #67e8f9; font-weight: 700; }}
            QLabel#status {{ color: #d4dbe7; }}
            QLabel#chip {{ border-radius: 10px; padding: 3px 10px; font-weight: 700; background: #1e293b; }}
            QLabel#diag {{ color: #8fb3c9; font-family: Consolas, Menlo, monospace; font-size: 12px; }}
            QLabel#error {{ color: #fca5a5; }}
            QLabel#footnote {{ color: #8290a6; font-size: 12px; }}
            QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{ background: #182338; border: 1px solid #35445c;
                border-radius: 7px; padding: 6px 9px; min-height: 22px; }}
            QComboBox QAbstractItemView {{ background: #182338; selection-background-color: #155e75; }}
            QSlider {{ min-height: 26px; }}
            QSlider::groove:horizontal {{ height: 5px; background: #344259; border-radius: 2px; }}
            QSlider::sub-page:horizontal {{ background: #22d3ee; border-radius: 2px; }}
            QSlider::handle:horizontal {{ background: #ecfeff; border: 2px solid #22d3ee; width: 16px;
                margin: -7px 0; border-radius: 9px; }}
            QSlider:focus {{ background: #1b2a44; border-radius: 6px; }}
            QSlider:focus::handle:horizontal {{ background: #fbbf24; }}
            QPushButton {{ background: #26344b; color: #e8edf6; border: 2px solid transparent; border-radius: 8px;
                padding: 9px 15px; font-weight: 700; min-height: 22px; }}
            QPushButton:hover {{ background: #31425f; }}
            QPushButton:focus, QComboBox:focus, QLineEdit:focus, QCheckBox:focus {{ border: 2px solid #fbbf24; }}
            QPushButton#startButton {{ background: #0891b2; color: white; min-width: 150px; }}
            QPushButton#startButton:hover {{ background: #06a3c7; }}
            QPushButton:disabled {{ color: #6b7a90; }}
            QCheckBox {{ spacing: 9px; min-height: 26px; }}
            QCheckBox::indicator {{ width: 18px; height: 18px; border: 2px solid #5b6b85; border-radius: 5px;
                background: #182338; }}
            QCheckBox::indicator:checked {{ background: #0891b2; border-color: #22d3ee; image: url("{check}"); }}
            QCheckBox::indicator:hover {{ border-color: #22d3ee; }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
            QScrollBar::handle:vertical {{ background: #35445c; border-radius: 4px; min-height: 30px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QTabWidget::pane {{ border: 1px solid #27344a; border-radius: 10px; background: #111a2b; }}
            QWidget#page, QScrollArea#pageScroll, QScrollArea#pageScroll > QWidget > QWidget {{ background: #111a2b; }}
            QWidget#page QLabel, QWidget#page QCheckBox, QWidget#page QSlider {{ background: transparent; }}
            QTabBar::tab {{ background: #182338; padding: 8px 14px; margin-right: 3px; border-top-left-radius: 7px;
                border-top-right-radius: 7px; color: #aebbd0; }}
            QTabBar::tab:selected {{ background: #111a2b; color: #f4f7fb; font-weight: 700; }}
            QTabBar::tab:focus {{ color: #fbbf24; }}
        """)

    def _build_tray(self) -> None:
        self.tray: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        show = QAction("Show Finger Mouse", self, triggered=self.show_window)
        self.tray_toggle = QAction("Start tracking", self, triggered=self.toggle_tracking)
        quit_action = QAction("Quit", self, triggered=self.quit)
        menu.addAction(show)
        menu.addAction(self.tray_toggle)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self.show_window()
                               if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        self.tray = tray
        self._tray_menu = menu

    # -- settings ---------------------------------------------------------------
    def change_settings(self, **changes: Any) -> None:
        self.replace_settings(self.settings.copy(**changes))

    def replace_settings(self, new: Settings) -> None:
        old = self.settings
        self.settings = new
        self.save_timer.start()
        self.overlay.set_diameter(new.halo_size)
        self.diag.setVisible(new.show_diagnostics)
        if new.verbose_logging != old.verbose_logging:
            logging.getLogger().setLevel(logging.DEBUG if new.verbose_logging else logging.INFO)
        if new.tracking_mode != old.tracking_mode:
            self._update_subtitle()
            if self.settings_dialog is not None:
                self.settings_dialog.update_calibration_status()
        if new.camera != old.camera:
            self._select_quick_camera()
        if self.engine is not None:
            self.engine.update_settings(new)
            if new.camera != old.camera or new.camera_resolution != old.camera_resolution:
                self.capabilities = None
        if self.output is not None:
            self.output.pause_on_physical_mouse = new.pause_on_physical_mouse

    def open_settings(self) -> None:
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self)
        else:
            self.settings_dialog.load(self.settings)
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def open_calibration(self) -> None:
        if self.engine is None or self.output is None or self.settings.tracking_mode != "eye":
            QMessageBox.information(self, "Start eye tracking first",
                "Switch to Eye gaze mode in Settings and press Start tracking, then come back here to "
                "calibrate.")
            return
        dialog = CalibrationDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self.settings_dialog is not None:
                self.settings_dialog.load(self.settings)
            QMessageBox.information(self, "Calibrated", "Eye tracking is calibrated. Look around to try it.")

    # -- cameras ----------------------------------------------------------------
    def refresh_cameras(self) -> None:
        tracking = self.is_tracking()

        def work() -> list[CameraInfo]:
            cams = list_cameras()
            if not cams and not tracking:
                cams = probe_camera_indices()   # the OS couldn't name them: try them
            return cams

        self.run_in_background(work, self._cameras_found)

    def _cameras_found(self, cams: Any) -> None:
        if isinstance(cams, Exception):
            log.warning("Camera listing failed: %s", cams)
            cams = []
        self.cameras = cams
        self._select_quick_camera()
        if self.settings_dialog is not None:
            self.settings_dialog.populate_cameras()

    def _select_quick_camera(self) -> None:
        combo = self.camera_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Automatic", "auto")
        for cam in self.cameras:
            combo.addItem(cam.display_name, cam.id)
        if self.settings.stream_url:
            combo.addItem("Phone or network stream", "url")
        i = combo.findData(self.settings.camera)
        if i < 0 and self.settings.camera.startswith("cv:"):
            combo.addItem(f"Camera {self.settings.camera[3:]} (not found)", self.settings.camera)
            i = combo.count() - 1
        combo.setCurrentIndex(max(0, i))
        combo.blockSignals(False)

    def _quick_camera(self, _i: int) -> None:
        value = self.camera_combo.currentData()
        if value and value != self.settings.camera:
            self.change_settings(camera=value)
            if self.settings_dialog is not None:
                self.settings_dialog.populate_cameras()

    def camera_command(self, command: str, value: Any = None) -> None:
        if self.engine is not None:
            self.engine.camera_command(command, value)

    def run_in_background(self, work, done) -> None:
        """Run ``work`` on a worker thread; ``done(result)`` runs later on this thread."""
        def target() -> None:
            try:
                result = work()
            except Exception as exc:
                log.exception("Background task failed")
                result = exc
            self._results.put((done, result))
        threading.Thread(target=target, daemon=True, name="background").start()

    # -- tracking ---------------------------------------------------------------
    def is_tracking(self) -> bool:
        return self.engine is not None

    def toggle_tracking(self) -> None:
        if self.engine is not None:
            self.stop_tracking()
        else:
            self.start_tracking()

    def start_tracking(self) -> None:
        if self.engine is not None:
            return
        if not self._request_mouse_control_access():
            return
        self._fatal = None
        try:
            backend = create_backend()
        except Exception as exc:
            self._set_status("stopped", f"Finger Mouse can't control the pointer here: {exc}")
            return
        log.info("Starting tracking: camera=%s backend=%s", self.settings.camera, backend.name)
        self.output = PointerOutput(backend, on_event=lambda kind, msg: self._output_events.put((kind, msg)),
                                    pause_on_physical_mouse=self.settings.pause_on_physical_mouse)
        self.output.start()
        self.engine = TrackingEngine(self.settings, self.output, self.cameras)
        self.engine.preview_width = self.preview.width()
        engine, output = self.engine, self.output
        self.watchdog.watch(Heartbeat("camera", lambda: engine.grabber.read_started if engine.grabber else None,
                                      1.5, "camera read blocked"))
        self.watchdog.watch(Heartbeat("inference", lambda: engine.infer_started, 1.0, "hand tracking inference"))
        self.watchdog.watch(Heartbeat("output", lambda: output.busy_since, 0.5, "pointer output call"))
        self.engine.start()
        self.start_button.setText("Stop tracking")
        if self.tray:
            self.tray_toggle.setText("Stop tracking")
        if self.settings_dialog is not None:
            self.settings_dialog.update_calibration_status()
        self._set_status("starting", "Starting the camera…")
        if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            self._set_status("starting", "Wayland desktops usually block apps from moving the pointer. If nothing "
                                         "moves, log in with an X11 session (e.g. “Ubuntu on Xorg”).")

    def stop_tracking(self) -> None:
        if self.engine is not None:
            self.engine.stop()          # finishes within a frame or two; see _on_tick
            self._set_status("stopped", "Stopping…")
        if self.output is not None:
            self.output.release_all()   # let go now, not when the engine gets round to it

    def _engine_finished(self) -> None:
        for name in ("camera", "inference", "output"):
            self.watchdog.unwatch(name)
        if self.output is not None:
            self.output.stop()
        self.engine = None
        self.output = None
        self.capabilities = None
        self.overlay.hide()
        self.start_button.setText("Start tracking")
        if self.tray:
            self.tray_toggle.setText("Start tracking")
        self._set_status("stopped", self._fatal or "Stopped. Start tracking to control the pointer again.")
        self.preview.setPixmap(QPixmap())
        self.preview.setText("The camera picture appears here once tracking starts.")
        if self.settings_dialog is not None:
            self.settings_dialog.update_camera_info()
            self.settings_dialog.update_calibration_status()
        if self._quitting:
            QApplication.quit()

    # -- the 60 Hz tick: everything the window shows ---------------------------
    def _ui_busy_since(self) -> Optional[float]:
        if not self.isVisible() or self.isMinimized():
            return None     # the OS may throttle a hidden window's timers; that's not a freeze
        now = time.monotonic()
        return self._last_tick if now - self._last_tick > 0.1 else None

    def _on_tick(self) -> None:
        now = time.monotonic()
        self._last_tick = now
        self._tick_times = [t for t in self._tick_times if t > now - 2] + [now]

        while not self._results.empty():
            done, result = self._results.get_nowait()
            done(result)
        while not self._output_events.empty():
            kind, message = self._output_events.get_nowait()
            self._on_output_event(kind, message)

        engine = self.engine
        if engine is None:
            return
        while not engine.events.empty():
            kind, payload = engine.events.get_nowait()
            self._on_engine_event(kind, payload)
        if self.engine is None:
            return
        if not engine.is_alive():
            self._engine_finished()
            return

        view = engine.view
        engine.preview_width = self.preview.width()
        if view.preview is not None and view.preview_seq != self._preview_seq:
            self._preview_seq = view.preview_seq
            h, w = view.preview.shape[:2]
            image = QImage(view.preview.data, w, h, int(view.preview.strides[0]), QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(image)
            if pixmap.width() > self.preview.width() or pixmap.height() > self.preview.height():
                pixmap = pixmap.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.FastTransformation)
            self.preview.setPixmap(pixmap)

        if (view.cursor and view.tracked and self.settings.show_halo
                and view.gesture not in ("paused", "no_hand", "no_face", "uncalibrated")):
            self.overlay.set_color(QColor(GESTURE_COLORS.get(view.gesture, "#22d3ee")))
            self.overlay.place(native_to_logical(*view.cursor))
        elif self.overlay.isVisible():
            self.overlay.hide()

        if view.camera_state == "connected" and isinstance(view.camera_detail, CameraCapabilities):
            if self.capabilities is not view.camera_detail:
                self.capabilities = view.camera_detail
                if self.settings_dialog is not None:
                    self.settings_dialog.update_camera_info()
        if self._notice and now < self._notice[2]:
            self._set_status(self._notice[0], self._notice[1])
        else:
            self._notice = None
            self._set_status(view.gesture if view.camera_state == "connected" else "starting", view.label)

        if self.settings.show_diagnostics:
            d = view.diagnostics
            ui_fps = (len(self._tick_times) - 1) / max(self._tick_times[-1] - self._tick_times[0], 1e-3)
            stall = self.watchdog.current
            self.diag.setText(
                f"camera {d.get('camera_fps', 0)} fps · frame age {d.get('frame_age_ms')} ms · "
                f"skipped {d.get('dropped', 0)} · inference {d.get('inference_ms')} ms · "
                f"tracking {d.get('tracking_fps')} fps · stale {d.get('stale', 0)} · "
                f"window {ui_fps:.0f} fps · pointer queue {d.get('output_queue', 0)}"
                + (f"\n⚠ stalled: {stall}" if stall else ""))

    def _on_engine_event(self, kind: str, payload: Any) -> None:
        if kind == "fatal":
            self._fatal = str(payload)
            log.error("Tracking stopped: %s", payload)
            self.stop_tracking()
        elif kind == "hide_requested":
            self._hide_from_gesture(str(payload))
        elif kind == "camera" and payload and payload[0] == "error":
            self._show_notice("starting", str(payload[1]))

    def _on_output_event(self, kind: str, message: str) -> None:
        if kind == "refused":
            self._fatal = message
            self.stop_tracking()
            QMessageBox.warning(self, "Pointer control blocked", message)
        elif kind == "error":
            self._show_notice("paused", f"The system refused a pointer action: {message}")
        elif kind == "override":
            self._show_notice("paused", message, 1.5)

    def _show_notice(self, chip: str, text: str, seconds: float = 4.0) -> None:
        """A message that stays up for a few seconds over the live gesture text."""
        self._notice = (chip, text, time.monotonic() + seconds)
        self._set_status(chip, text)

    def _set_status(self, gesture: str, text: str) -> None:
        names = {"ready": "Ready", "pinched": "Pinch", "dragging": "Dragging", "scrolling": "Scrolling",
                 "paused": "Paused", "hide": "Hide", "no_hand": "No hand", "open": "Tracking",
                 "pointing": "Tracking", "starting": "Starting", "stopped": "Stopped",
                 "gazing": "Tracking", "dwelling": "Click", "no_face": "No face", "uncalibrated": "Not calibrated"}
        chip = names.get(gesture, "Tracking")
        if self.chip.text() != chip:
            self.chip.setText(chip)
            color = GESTURE_COLORS.get(gesture, "#8290a6")
            self.chip.setStyleSheet(f"color: {color}; border: 1px solid {color};")
        if self.status.text() != text:
            self.status.setText(text)

    # -- hide gesture, tray, quit -------------------------------------------------
    def _hide_from_gesture(self, action: str) -> None:
        log.info("Hide gesture: %s", action)
        self.stop_tracking()
        if action == "quit":
            self.quit()
        elif action == "tray" and self.tray is not None:
            self.hide()
            self.tray.showMessage(APP_NAME, "Finger Mouse is hidden and tracking is stopped. "
                                  "Click the tray icon to bring it back.", app_icon(), 4000)
        else:
            self.showMinimized()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        if self.engine is not None:
            self.stop_tracking()
            self.engine.join(2.0)      # the loop checks for stop every 0.2 s
            self._engine_finished()
        if self.output is not None:
            self.output.stop()
        app_settings.save(self.settings)
        self.watchdog.stop()
        self.overlay.close()
        if self.tray:
            self.tray.hide()
        event.accept()
        QApplication.quit()

    # -- platform helpers ---------------------------------------------------------
    def open_system_cursor_settings(self) -> None:
        """The operating system's own pointer-size setting (the halo is only Finger Mouse's)."""
        if sys.platform == "win32":
            QDesktopServices.openUrl(QUrl("ms-settings:easeofaccess-mousepointer"))
        elif sys.platform == "darwin":
            QDesktopServices.openUrl(QUrl("x-apple.systempreferences:com.apple.Accessibility-Settings.extension?Display"))
            QMessageBox.information(self, "System pointer size",
                                    "In System Settings, open Accessibility → Display → Pointer size.")
        else:
            QMessageBox.information(self, "System pointer size",
                                    "Open your desktop's Accessibility settings and change the pointer size there.")

    def _request_mouse_control_access(self) -> bool:
        """macOS needs Accessibility permission (and permission to post events)."""
        if sys.platform != "darwin":
            return True
        trusted = posting = False
        try:
            import Quartz
            from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

            preflight = getattr(Quartz, "CGPreflightPostEventAccess", None)
            request = getattr(Quartz, "CGRequestPostEventAccess", None)
            if preflight is None or request is None:
                raise RuntimeError("Core Graphics permission checks are unavailable.")
            posting = bool(preflight())
            if not posting:
                request()
                posting = bool(preflight())
            trusted = bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}))
            if posting and trusted:
                return True
        except Exception as exc:
            self._set_status("stopped", f"Couldn't check macOS permissions: {exc}")
            return False
        executable = Path(sys.executable).resolve()
        app_path = next((str(p) for p in executable.parents if p.suffix == ".app"), str(executable))
        QDesktopServices.openUrl(QUrl("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"))
        QMessageBox.warning(
            self, "Allow Finger Mouse to control the pointer",
            f"macOS hasn't allowed this copy to control the pointer yet (Accessibility: "
            f"{'allowed' if trusted else 'not allowed'}; posting events: {'allowed' if posting else 'not allowed'}).\n\n"
            f"Running app: {app_path}\n\n"
            "Quit Finger Mouse. In System Settings → Privacy & Security → Accessibility, remove any old "
            "Finger Mouse entry, add and turn on this exact copy, then open it again. Keep it in "
            "Applications: macOS treats a moved or updated copy as a different app.")
        self._set_status("stopped", "Allow Finger Mouse under Privacy & Security → Accessibility, then reopen it.")
        return False


# ---------------------------------------------------------------------------
# Self-test and entry point
# ---------------------------------------------------------------------------

def self_test(out_path: Optional[str]) -> int:
    """Check an installed copy works, without a camera or a window."""
    results: dict[str, Any] = {"app": APP_NAME, "version": APP_VERSION, "platform": sys.platform,
                               "python": sys.version.split()[0], "checks": {}}
    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            results["checks"][name] = {"ok": True, "detail": fn()}
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - report every failure, even a library's sys.exit()
            ok = False
            results["checks"][name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    def hand_model() -> str:
        from hand_tracker import HandTracker, check_bundle, start_check
        ok, why = start_check()
        if not ok:
            # Opening the model would abort on this machine (a Mac virtual
            # machine, for instance); check everything short of that instead.
            return f"not opened here ({why}); " + check_bundle()
        tracker = HandTracker()
        started = time.perf_counter()
        tracker.process(np.zeros((360, 640, 3), np.uint8))
        ms = (time.perf_counter() - started) * 1000
        kind = tracker.kind
        tracker.close()
        return f"{kind}, first frame {ms:.0f} ms"

    def eye_model() -> str:
        from eye_tracker import EyeTracker, check_bundle as eye_check_bundle
        from hand_tracker import start_check
        ok, why = start_check()   # shared with hand_model: same native library, same probe
        if not ok:
            return f"not opened here ({why}); " + eye_check_bundle()
        tracker = EyeTracker()
        started = time.perf_counter()
        tracker.process(np.zeros((360, 640, 3), np.uint8))
        ms = (time.perf_counter() - started) * 1000
        tracker.close()
        return f"first frame {ms:.0f} ms"

    def pointer() -> str:
        backend = create_backend()
        return f"{backend.name}, desktop {backend.desktop_rect('primary')}"

    def qt() -> str:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([sys.argv[0]])
        return f"Qt platform {app.platformName()}"

    check("hand_model", hand_model)
    check("eye_model", eye_model)
    check("qt", qt)
    check("pointer_backend", pointer)
    check("camera_listing", lambda: [c.label for c in list_cameras()])
    check("settings", lambda: str(app_settings.settings_path()))
    results["ok"] = ok
    text = json.dumps(results, indent=2)
    try:
        if out_path:
            Path(out_path).write_text(text, encoding="utf-8")
        elif sys.stdout:
            print(text)
    except OSError:
        # Never let an error reach the windowed app's crash dialog: in an
        # unattended check, a dialog nobody can see is a hang.
        return 2
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--probe-hand-model":      # the child process of hand_tracker.start_check()
        from hand_tracker import probe_main
        return probe_main()
    if args and args[0] == "--self-test":
        return self_test(args[1] if len(args) > 1 else None)

    log_dir = app_settings.config_dir() / "logs"
    try:
        setup_logging(log_dir, app_settings.load().verbose_logging)
    except OSError:
        pass
    log.info("%s %s starting on %s", APP_NAME, APP_VERSION, sys.platform)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(PUBLISHER)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)   # hiding to the tray must not quit
    window = MainWindow(log_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
