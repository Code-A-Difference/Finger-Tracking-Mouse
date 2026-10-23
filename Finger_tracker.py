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
import math
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
    from PySide6.QtCore import QPoint, QRect, QRegularExpression, QSize, Qt, QTimer, QUrl
    from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QGuiApplication, QIcon, QImage,
                               QKeySequence, QPainter, QPen, QPixmap, QRegularExpressionValidator, QShortcut)
    from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                                   QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                                   QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QRadioButton,
                                   QScrollArea, QSizePolicy, QSlider, QStackedWidget, QSystemTrayIcon, QVBoxLayout,
                                   QWidget)

    import app_settings
    from app_settings import Settings
    from app_version import APP_NAME, APP_VERSION, PUBLISHER, SOURCE_URL
    import ui_theme
    import updates
    from camera import CameraCapabilities, CameraInfo, OpenCVCamera, list_cameras, probe_camera_indices, \
        probe_resolutions, validate_stream_url
    from diagnostics import Heartbeat, Watchdog, setup_logging
    from gesture_state import GazeCalibration, steady_reading
    import sign_language
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
    "tracking": "#cbd5e1",
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


def halo_diameter(settings: Settings) -> int:
    """The halo's size. In eye mode it is a third of the hand-mode size:
    a wide ring around a gaze point hides where it actually is, and the gaze
    jitter makes a big ring swim, so the point looks less accurate than it is."""
    if settings.tracking_mode == "eye":
        return max(12, settings.halo_size // 3)
    return settings.halo_size


class CalibrationTarget(QWidget):
    """The thing to look at while calibrating.

    A big ring that shrinks onto a tiny centre point. A plain dot gives the
    eye a 28-pixel area to rest anywhere in, and every pixel of that slop
    became calibration error. A shrinking ring pulls the eye to one exact
    point, and sampling only starts once it has closed.
    """

    START, END = 64, 10

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedSize(self.START + 8, self.START + 8)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._t = 0.0          # 0 = ring at full size, 1 = closed
        self.sampling = False

    def set_progress(self, t: float) -> None:
        self._t = max(0.0, min(1.0, t))
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 (Qt name)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QPoint(self.width() // 2, self.height() // 2)
        ease = 1 - (1 - self._t) ** 3
        r = (self.START + (self.END - self.START) * ease) / 2
        ring = QColor(34, 211, 238) if not self.sampling else QColor(74, 222, 128)
        ring.setAlpha(230)
        p.setPen(QPen(ring, 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, int(r), int(r))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(c, 2, 2)          # the exact point to look at: 4 px


class CalibrationDialog(QDialog):
    """Teach the eye tracker where you're looking.

    Thorough (the default, about 70 seconds):
      1. a 5x5 grid of points, edge to edge, each a ring that closes onto a
         4-pixel centre, sampled once it has (blinks dropped, median kept);
      2. a dot that glides around the screen for 20 seconds while you follow
         it, hundreds of samples, at every place in between the grid points;
      3. five check points it hasn't trained on: the error there is what's
         reported, so the number is honest, and they're then added in too.
    Quick: thirteen points, no moving dot, no check (the report is then the
    fit's own error).

    ``recentre=True`` is the one-second drift fix: one point in the middle,
    shifting the existing calibration instead of replacing it.

    Needs eye tracking running (MainWindow checks before opening this): it
    reads the engine's last gaze values on a timer, the same way the main
    window reads ``engine.view``, nothing here touches the tracking thread.
    """

    GRID = [(x, y) for y in (0.04, 0.27, 0.5, 0.73, 0.96) for x in (0.04, 0.27, 0.5, 0.73, 0.96)]
    QUICK = [(0.5, 0.5), (0.05, 0.05), (0.5, 0.05), (0.95, 0.05), (0.95, 0.5), (0.95, 0.95), (0.5, 0.95),
             (0.05, 0.95), (0.05, 0.5), (0.275, 0.275), (0.725, 0.275), (0.725, 0.725), (0.275, 0.725)]
    CHECK = [(0.17, 0.38), (0.62, 0.16), (0.84, 0.62), (0.4, 0.84), (0.6, 0.45)]
    SETTLE_MS = 900       # the ring closes over this long; the eye gets there
    SAMPLE_MS = 800       # then readings are collected for this long
    PURSUIT_MS = 20000    # the moving dot
    PURSUIT_LAG = 0.15    # eyes trail a moving target by about this much (seconds)
    FIX_WEIGHT = 8.0      # a fixation (a median of ~25 frames) counts as much as 8 moving-dot frames
    RETRIES = 1

    def __init__(self, main: "MainWindow", recentre: bool = False) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.main = main
        self.recentre = recentre
        self.setModal(True)
        left, top, width, height = main.output.backend.desktop_rect(main.settings.screen)
        self.setGeometry(left, top, width, height)
        self.setStyleSheet("background: #05070d;")
        thorough = main.settings.eye_calibration_detail == "thorough" and not recentre
        if recentre:
            self.plan = [("fix", [(0.5, 0.5)])]
        elif thorough:
            # snake through the grid so the eye never jumps across the whole screen
            rows = [self.GRID[i:i + 5] for i in range(0, 25, 5)]
            snake = [p for i, r in enumerate(rows) for p in (r if i % 2 == 0 else r[::-1])]
            self.plan = [("fix", snake), ("pursuit", None), ("check", self.CHECK)]
        else:
            self.plan = [("fix", self.QUICK)]
        self.samples: list[tuple] = []
        self.weights: list[float] = []
        self.check_samples: list[tuple] = []
        self.accuracy: Optional[float] = None    # mean error, fraction of the screen
        self.accuracy_is_held_out = False
        self._stage = 0
        self._index = 0
        self._retried = 0
        self._readings: list[tuple] = []
        self._trace: list[tuple] = []            # pursuit: (t, features, blink)
        self._path: list[tuple] = []             # pursuit: (t, x, y)
        self._phase_start = 0.0
        self._started = False

        self.hint = QLabel(self)
        self.hint.setStyleSheet("color: #aebbd0; font-size: 16px; background: transparent;")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.hint.setWordWrap(True)
        self.hint.setGeometry(width // 8, height // 2 + 80, width * 3 // 4, 90)

        self.target = CalibrationTarget(self)
        self.target.hide()
        self._tick = QTimer(self, interval=16)
        self._tick.timeout.connect(self._step)

        if recentre:
            self._begin()
        else:
            self.hint.move(width // 8, height // 2 - 45)
            self.hint.setText(
                ("Thorough calibration, about a minute. Sit as you normally will, face the screen, and keep "
                 "your head still. Look at the centre of each ring until it moves on, then follow the moving "
                 "dot with your eyes. " if thorough else
                 "Quick calibration, 13 points. Keep your head still and look at the centre of each ring. ")
                + "Press Space to start. Esc cancels.")

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        elif event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self._started:
            self._begin()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        if not self._started:
            self._begin()

    # -- reading the engine ------------------------------------------------
    def _reading(self) -> Optional[tuple]:
        engine = self.main.engine
        if engine is None or engine.last_gaze_features is None:
            return None
        return engine.last_gaze_features, engine.last_gaze_head, engine.last_gaze_blink

    # -- flow --------------------------------------------------------------
    def _begin(self) -> None:
        self._started = True
        self._stage, self._index = 0, 0
        self._enter_stage()

    def _enter_stage(self) -> None:
        if self._stage >= len(self.plan):
            self._finish()
            return
        kind, _points = self.plan[self._stage]
        self._index = 0
        if kind == "pursuit":
            self._trace, self._path = [], []
            self.target.sampling = True
            self.target.set_progress(1)
            self.target.show()
            self._place_hint(0.5)
            self.hint.setText("Now follow the moving dot with your eyes, just your eyes, head still.")
            self._phase_start = time.monotonic()
            self._tick.start()
        else:
            self._show_point()

    def _place_hint(self, fy: float) -> None:
        self.hint.move(self.width() // 8, self.height() // 2 - 150 if fy > 0.6 else self.height() // 2 + 80)

    def _show_point(self) -> None:
        kind, points = self.plan[self._stage]
        fx, fy = points[self._index]
        self._move_target(fx, fy)
        self.target.sampling = False
        self.target.set_progress(0)
        self.target.show()
        self._place_hint(fy)
        label = {"fix": "Calibrating", "check": "Checking"}[kind] if not self.recentre else "Re-centring"
        again = " Keep your eyes open and on the centre." if self._retried else ""
        self.hint.setText(f"{label}: look at the centre of the ring, {self._index + 1} of {len(points)}. "
                          f"Esc cancels.{again}")
        self._readings = []
        self._phase_start = time.monotonic()
        self._tick.start()

    def _move_target(self, fx: float, fy: float) -> None:
        self.target.move(int(fx * self.width()) - self.target.width() // 2,
                         int(fy * self.height()) - self.target.height() // 2)

    @staticmethod
    def pursuit_point(t: float) -> tuple[float, float]:
        """Where the moving dot is ``t`` seconds in: a slow Lissajous sweep that
        covers the whole screen, edge to edge, without sudden jumps."""
        return (0.5 + 0.45 * math.sin(t * 0.55 + 0.3), 0.5 + 0.44 * math.sin(t * 0.77))

    def _step(self) -> None:
        kind, _ = self.plan[self._stage]
        elapsed = (time.monotonic() - self._phase_start) * 1000
        if kind == "pursuit":
            t = elapsed / 1000
            x, y = self.pursuit_point(t)
            self._move_target(x, y)
            self._path.append((t, x, y))
            r = self._reading()
            if r is not None:
                self._trace.append((t, r[0], r[2]))
            if elapsed >= self.PURSUIT_MS:
                self._tick.stop()
                self._end_pursuit()
            return
        if elapsed < self.SETTLE_MS:
            self.target.set_progress(elapsed / self.SETTLE_MS)
            return
        if not self.target.sampling:
            self.target.sampling = True
            self.target.set_progress(1)
        r = self._reading()
        if r is not None:
            self._readings.append(r)
        # a re-centre rests on one point, so it gets twice the readings
        if elapsed >= self.SETTLE_MS + self.SAMPLE_MS * (2 if self.recentre else 1):
            self._tick.stop()
            self._next_point()

    def _end_pursuit(self) -> None:
        # Pair each frame with where the dot was PURSUIT_LAG earlier (the eye
        # trails a moving target), skip the first second while the eye catches
        # up, and drop blinks.
        for t, features, blink in self._trace:
            if t < 1.0 or blink >= 0.35:
                continue
            x, y = self.pursuit_point(t - self.PURSUIT_LAG)
            self.samples.append((features, (x, y)))
            self.weights.append(1.0)
        self._stage += 1
        self._enter_stage()

    def _next_point(self) -> None:
        kind, points = self.plan[self._stage]
        reading = steady_reading(self._readings)
        if reading is None and self._retried < self.RETRIES:
            self._retried += 1
            self._show_point()
            return
        if reading is not None:
            features, _head = reading
            target = points[self._index]
            if kind == "check":
                self.check_samples.append((features, target))
            else:
                self.samples.append((features, target))
                self.weights.append(self.FIX_WEIGHT)
        self._retried = 0
        self._index += 1
        if self._index >= len(points):
            self._stage += 1
            self._enter_stage()
        else:
            self._show_point()

    def _finish(self) -> None:
        self.target.hide()
        if self.recentre:
            cal = GazeCalibration.from_json(self.main.settings.eye_calibration)
            if not self.samples or not cal.recentre(self.samples[0][0], (0.5, 0.5)):
                QMessageBox.warning(self, "Couldn't re-centre",
                    "Finger Mouse couldn't see your eyes clearly enough. Try again, or recalibrate.")
                self.reject()
                return
            self.main.change_settings(eye_calibration=cal.to_json())
            self.accept()
            return
        fixations = sum(1 for w in self.weights if w == self.FIX_WEIGHT)
        if fixations < 9:
            QMessageBox.warning(self, "Calibration incomplete",
                "Finger Mouse couldn't see your eyes clearly for enough of the points. Make sure your face "
                "is well lit and centred in the camera, then try again.")
            self.reject()
            return
        cal = GazeCalibration()
        if not cal.fit(self.samples, self.weights):
            QMessageBox.warning(self, "Calibration didn't take",
                "That didn't produce a usable mapping. Try again, keeping your head still and looking "
                "only at the centre of each ring.")
            self.reject()
            return
        if self.check_samples:
            # the honest number: points it never trained on. Then use them too.
            self.accuracy = cal.error(self.check_samples)
            self.accuracy_is_held_out = True
            refit = GazeCalibration()
            if refit.fit(self.samples + self.check_samples,
                         self.weights + [self.FIX_WEIGHT] * len(self.check_samples)):
                cal = refit
        else:
            self.accuracy = cal.error([s for s, w in zip(self.samples, self.weights) if w == self.FIX_WEIGHT])
        self.main.change_settings(eye_calibration=cal.to_json())
        self.accept()


class TeachSignsDialog(QDialog):
    """Show Finger Mouse your fingerspelling, one sign at a time.

    For each sign: the letter and how it's made are shown, there's a moment
    to get your hand into shape, then two seconds of your hand are recorded
    (move it a little, slightly different angles and distances make the
    recognition sturdier). A sign that couldn't be seen clearly is tried again.
    While this window is open, signs are recognised (shown live under "I see")
    but never typed.
    """

    READY_MS = 1500
    CAPTURE_MS = 2000
    MIN_FRAMES = 12

    def __init__(self, main: "MainWindow") -> None:
        # Its own window, on top: you'll be looking at the camera preview and this, not the main window.
        super().__init__(None, Qt.WindowType.WindowStaysOnTopHint)
        self.main = main
        self.setModal(True)
        self.setWindowTitle("Teach your signs")
        self.setMinimumSize(720, 540)
        # A window of its own doesn't inherit the main window's look: give it the same theme.
        self.setStyleSheet(ui_theme.stylesheet())
        self.book = sign_language.SignBook.from_json(app_settings.load_signs_text())
        self.queue: list[str] = []
        self.current: Optional[str] = None
        self.samples: list[tuple[float, ...]] = []
        self._phase = "idle"
        self._phase_start = 0.0
        self._last_seq = -1
        self._retried = False

        outer = QHBoxLayout(self)
        self.list = QListWidget()
        self.list.setFixedWidth(190)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.itemSelectionChanged.connect(self._show_selected)
        outer.addWidget(self.list)

        right = QVBoxLayout()
        self.letter = QLabel("")
        self.letter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.letter.setObjectName("signLetter")
        right.addWidget(self.letter)
        self.how = QLabel("")
        self.how.setWordWrap(True)
        self.how.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.how.setObjectName("secondary")
        right.addWidget(self.how)
        self.status = QLabel("Pick “Teach all” to go through every sign, or choose one on the left.")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setObjectName("warnText")
        right.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        right.addWidget(self.bar)
        self.seeing = QLabel("I see:, ")
        self.seeing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seeing.setObjectName("okText")
        right.addWidget(self.seeing)
        right.addStretch(1)

        row = QHBoxLayout()
        self.teach_all = QPushButton("Teach all")
        self.teach_all.setObjectName("accent")
        self.teach_all.setToolTip("Every sign you haven't taught yet, in order. Already taught all? Then every sign again.")
        self.teach_all.clicked.connect(self._teach_all)
        self.teach_one = QPushButton("Teach this one")
        self.teach_one.clicked.connect(self._teach_selected)
        self.skip = QPushButton("Skip")
        self.skip.clicked.connect(self._skip)
        self.forget = QPushButton("Forget this one")
        self.forget.clicked.connect(self._forget_selected)
        for b in (self.teach_all, self.teach_one, self.skip, self.forget):
            row.addWidget(b)
        right.addLayout(row)
        close = QPushButton("Done")
        close.clicked.connect(self.accept)
        right.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        outer.addLayout(right, 1)

        self._fill_list()
        self.list.setCurrentRow(0)
        self._tick = QTimer(self, interval=33)
        self._tick.timeout.connect(self._step)
        self._tick.start()
        if self.main.engine is not None:
            self.main.engine.teaching = True

    # -- list ------------------------------------------------------------
    def _label(self, sign: str) -> str:
        name = {"SPACE": "Space", "DELETE": "Delete", "ENTER": "Enter (optional)"}.get(sign, sign)
        return f"{'✓' if sign in self.book.samples else '  '}  {name}"

    def _fill_list(self) -> None:
        row = self.list.currentRow()
        self.list.clear()
        for sign in sign_language.SIGNS:
            self.list.addItem(self._label(sign))
        if row >= 0:
            self.list.setCurrentRow(row)

    def _selected(self) -> Optional[str]:
        row = self.list.currentRow()
        return sign_language.SIGNS[row] if 0 <= row < len(sign_language.SIGNS) else None

    def _show_sign(self, sign: Optional[str]) -> None:
        if sign is None:
            self.letter.setText("")
            self.how.setText("")
            return
        self.letter.setText({"SPACE": "␣", "DELETE": "⌫", "ENTER": "↵"}.get(sign, sign))
        name = {"SPACE": "Space", "DELETE": "Delete", "ENTER": "Enter"}.get(sign)
        self.how.setText((f"{name}: " if name else "") + sign_language.HOW_TO[sign])

    def _show_selected(self) -> None:
        if self._phase == "idle":
            self._show_sign(self._selected())

    # -- teaching --------------------------------------------------------
    def _teach_all(self) -> None:
        todo = [s for s in sign_language.SIGNS if s not in self.book.samples and s != sign_language.ENTER]
        self.queue = todo or [s for s in sign_language.SIGNS if s != sign_language.ENTER]
        self._next()

    def _teach_selected(self) -> None:
        sign = self._selected()
        if sign:
            self.queue = [sign]
            self._next()

    def _skip(self) -> None:
        if self._phase != "idle":
            self._next()

    def _forget_selected(self) -> None:
        sign = self._selected()
        if sign and sign in self.book.samples:
            self.book.forget(sign)
            self._save()

    def _next(self) -> None:
        self._retried = False
        if not self.queue:
            self.current = None
            self._phase = "idle"
            self.bar.setValue(0)
            taught = len(self.book.taught)
            self.status.setText(f"{taught} of {len(sign_language.SIGNS)} signs taught. Close this window and sign "
                                "into any app, hold each letter steady for a moment to type it.")
            self._show_sign(self._selected())
            return
        self.current = self.queue.pop(0)
        self.list.setCurrentRow(sign_language.SIGNS.index(self.current))
        self._start_ready()

    def _start_ready(self) -> None:
        self._show_sign(self.current)
        self._phase, self._phase_start = "ready", time.monotonic()
        self.samples = []
        self.status.setText("Get ready, make this sign with your signing hand.")

    def _step(self) -> None:
        engine = self.main.engine
        if engine is not None:
            seen, conf = engine.sign_seen
            name = {"SPACE": "space", "DELETE": "delete", "ENTER": "enter"}.get(seen or "", seen)
            self.seeing.setText(f"I see: {name}  ({int(conf * 100)}% sure)" if seen else
                                ("I see: your hand, but no sign I know yet" if engine.last_sign_features else
                                 "I see: no hand"))
        if self._phase == "idle":
            return
        elapsed = (time.monotonic() - self._phase_start) * 1000
        if self._phase == "ready":
            self.bar.setValue(int(100 * elapsed / self.READY_MS))
            if elapsed >= self.READY_MS:
                self._phase, self._phase_start = "capture", time.monotonic()
                self.status.setText("Hold it… move your hand a little, closer, further, a slight turn.")
                self._last_seq = engine.sign_frame_seq if engine else -1
            return
        # capture
        self.bar.setValue(int(100 * elapsed / self.CAPTURE_MS))
        if engine is not None and engine.sign_frame_seq != self._last_seq and engine.last_sign_features is not None:
            self._last_seq = engine.sign_frame_seq
            self.samples.append(engine.last_sign_features)
        if elapsed >= self.CAPTURE_MS:
            if len(self.samples) < self.MIN_FRAMES:
                if not self._retried:
                    self._retried = True
                    self.status.setText("Couldn't see your hand clearly, once more. Keep it inside the camera view.")
                    self._phase, self._phase_start = "ready", time.monotonic()
                    self.samples = []
                    return
                self.status.setText(f"Skipped {self.current}: the hand wasn't visible enough.")
                self._next()
                return
            self.book.teach(self.current, self.samples)
            self._save()
            self._next()

    def _save(self) -> None:
        try:
            app_settings.save_signs_text(self.book.to_json())
        except OSError as exc:
            QMessageBox.warning(self, "Couldn't save your signs", str(exc))
            return
        if self.main.engine is not None:
            self.main.engine.reload_signs()
        self._fill_list()
        if self.main.settings_panel is not None:
            self.main.settings_panel.update_sign_status()

    def done(self, result: int) -> None:  # noqa: D401 (Qt name)
        self._tick.stop()
        if self.main.engine is not None:
            self.main.engine.teaching = False
        super().done(result)


# ---------------------------------------------------------------------------
# Settings pages
# ---------------------------------------------------------------------------

class Group:
    """A titled block of settings rows on one card, the system Settings way."""

    def __init__(self, page_layout: QVBoxLayout, title: str = "") -> None:
        if title:
            label = QLabel(title)
            label.setObjectName("groupTitle")
            page_layout.addSpacing(10)
            page_layout.addWidget(label)
        self.frame = QFrame()
        self.frame.setObjectName("group")
        self.layout = QVBoxLayout(self.frame)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        page_layout.addWidget(self.frame)
        self.rows = 0

    def add(self, widget: QWidget) -> QWidget:
        widget.sep_line = None           # type: ignore[attr-defined]
        if self.rows:
            line = QFrame()
            line.setObjectName("rowLine")
            self.layout.addWidget(line)
            widget.sep_line = line       # type: ignore[attr-defined]
        self.layout.addWidget(widget)
        self.rows += 1
        return widget

    def row(self, title: str, hint: str = "", *controls: QWidget, stack: bool = False) -> QWidget:
        """Title and hint on the left, controls on the right (or underneath with ``stack``)."""
        row = QWidget()
        row.setObjectName("page")
        outer = QVBoxLayout(row) if stack else QHBoxLayout(row)
        outer.setContentsMargins(18, 12, 18, 12)
        outer.setSpacing(10 if stack else 18)
        text = QVBoxLayout()
        text.setSpacing(2)
        label = QLabel(title)
        label.setObjectName("rowTitle")
        label.setWordWrap(True)
        text.addWidget(label)
        h = None
        if hint:
            h = QLabel(hint)
            h.setObjectName("rowHint")
            h.setWordWrap(True)
            h.setTextFormat(Qt.TextFormat.RichText)
            h.setOpenExternalLinks(True)
            text.addWidget(h)
        outer.addLayout(text, 1)
        if controls:
            box = QHBoxLayout()
            box.setSpacing(10)
            if stack:
                box.setAlignment(Qt.AlignmentFlag.AlignLeft)
            for c in controls:
                box.addWidget(c)
            if stack:
                box.addStretch(1)
            outer.addLayout(box)
        if controls and not title:
            label.hide()
        row.title_label = label          # type: ignore[attr-defined]
        row.hint_label = h               # type: ignore[attr-defined]
        return self.add(row)


MODE_NAMES = {"head": "Head pointer", "sign": "Sign language typing", "hand": "Hand gestures", "eye": "Eye gaze"}


class SettingsPanel(QWidget):
    """Every setting, as pages for the main window's sidebar. Changes apply to
    tracking at once and are saved. The panel itself is never shown; the main
    window places each of its ``pages`` in its own page stack."""

    PAGES = (("head", "head", "Head pointer"), ("sign", "sign", "Sign language"), ("hand", "hand", "Hand gestures"),
             ("eye", "eye", "Eye gaze"), ("pointer", "pointer", "Pointer"), ("camera", "camera", "Camera"),
             ("caregiver", "caregiver", "Caregiver"), ("advanced", "advanced", "Advanced"))

    def __init__(self, main: "MainWindow") -> None:
        super().__init__(main)
        self.main = main
        self.hide()
        self._loading = True
        self.controls: dict[str, Any] = {}
        self.twins: list[tuple[str, Any]] = []
        self.mode_rows: dict[str, tuple[QLabel, QPushButton]] = {}
        self.pages: dict[str, QScrollArea] = {}
        builders = {"head": self._head_page, "sign": self._sign_page, "hand": self._hand_page, "eye": self._eye_page,
                    "pointer": self._pointer_page, "camera": self._camera_page, "caregiver": self._caregiver_page,
                    "advanced": self._advanced_page}
        for key, _icon, _name in self.PAGES:
            scroll = QScrollArea()
            scroll.setObjectName("pageScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(builders[key]())
            self.pages[key] = scroll
        self.load(main.settings)
        self._loading = False

    # -- building blocks ------------------------------------------------------
    def _page(self, title: str, intro: str = "") -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("page")
        outer = QHBoxLayout(page)
        outer.setContentsMargins(36, 28, 36, 36)
        column = QWidget()
        column.setObjectName("page")
        column.setMaximumWidth(880)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        if intro:
            text = QLabel(intro)
            text.setObjectName("pageIntro")
            text.setWordWrap(True)
            text.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(text)
        layout.addSpacing(6)
        outer.addWidget(column, 1)
        outer.addStretch(0)
        page.column_layout = layout        # type: ignore[attr-defined]
        return page, layout

    def _done(self, page: QWidget, layout: QVBoxLayout) -> QWidget:
        layout.addStretch(1)
        return page

    def _mode_row(self, l: QVBoxLayout, mode: str) -> None:
        """The top of a mode's page: is it the way of steering in use? If not, one button switches to it."""
        g = Group(l)
        label = QLabel()
        label.setObjectName("rowTitle")
        use = QPushButton(f"Use {MODE_NAMES[mode].lower()}")
        use.setObjectName("accent")
        use.clicked.connect(lambda: self.main.change_settings(tracking_mode=mode))
        row = QWidget()
        row.setObjectName("page")
        h = QHBoxLayout(row)
        h.setContentsMargins(18, 12, 18, 12)
        h.addWidget(label, 1)
        h.addWidget(use)
        g.add(row)
        self.mode_rows[mode] = (label, use)

    def _slider(self, g: Group, key: str, label: str, lo: int, hi: int,
                fmt: str = "{}", hint: str = "", step: int = 1) -> QSlider:
        value = QLabel()
        value.setObjectName("rowValue")
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value.setMinimumWidth(96)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setSingleStep(step)
        slider.setPageStep(max(step, (hi - lo) // 10))
        slider.setAccessibleName(label)
        slider.setFixedWidth(240)
        slider.setCursor(Qt.CursorShape.PointingHandCursor)

        def changed(v: int) -> None:
            value.setText(fmt.format(v))
            self._set(key, v)

        slider.valueChanged.connect(changed)
        g.row(label, hint, value, slider)
        self._register(key, (slider, value, fmt))
        return slider

    def _check(self, g: Group, key: str, label: str, hint: str = "") -> QCheckBox:
        box = ui_theme.ToggleSwitch()
        box.setAccessibleName(label)
        box.toggled.connect(lambda on: self._set(key, on))
        g.row(label, hint, box)
        self._register(key, box)
        return box

    def _choice(self, g: Group, key: str, label: str, options: list[tuple[str, str]],
                hint: str = "") -> QComboBox:
        combo = QComboBox()
        for value, text in options:
            combo.addItem(text, value)
        combo.setAccessibleName(label)
        combo.setCursor(Qt.CursorShape.PointingHandCursor)
        combo.currentIndexChanged.connect(lambda _i: self._set(key, combo.currentData()))
        g.row(label, hint, combo)
        self._register(key, combo)
        return combo

    @staticmethod
    def _button(text: str, slot, icon_name: str = "", accent: bool = False) -> QPushButton:
        b = QPushButton(("  " + text) if icon_name else text)   # a little room between icon and words
        if icon_name:
            b.setIcon(ui_theme.icon(icon_name, ui_theme.colors()["on_accent" if accent else "text"], 16))
            b.icon_name = icon_name          # type: ignore[attr-defined]
        if accent:
            b.setObjectName("accent")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    # -- pages ------------------------------------------------------------------
    def _head_page(self) -> QWidget:
        page, l = self._page("Head pointer", "The nose steers, so talking or smiling doesn't move the pointer. "
                                             "Winks click. No calibration needed.")
        self._mode_row(l, "head")
        g = Group(l, "Moving")
        self._choice(g, "head_pointer_mode", "Movement",
                     [("relative", "Like a mouse (recommended)"), ("absolute", "Point at the spot")],
                     "Like a mouse: small movements are precise, a quick flick crosses the screen. "
                     "Point at the spot is simpler but needs Re-centre after shifting in the seat.")
        self._slider(g, "head_speed", "Speed", 1, 100, "{}%")
        self.accel_row = self._slider(g, "head_acceleration", "Acceleration", 0, 100, "{}%",
                                      "How much further a quick movement goes than a slow one.").parentWidget()
        self.reach_row = self._slider(g, "head_reach", "Turn to reach the edge", 10, 80, "{}% of face",
                                      "Smaller means less turning.").parentWidget()
        self._slider(g, "head_dead_zone", "Steadiness", 0, 100, "{}%",
                     "Slower movement than this is ignored, so tremor or breathing doesn't drift the pointer.")
        self._slider(g, "head_smoothing", "Smoothing", 0, 100, "{}%")
        g.row("Re-centre", "Puts the pointer back in the middle (Like a mouse), or makes where the head is now "
                           "the centre (Point at the spot).",
              self._button("Re-centre now", self.main.recentre_head, "refresh"))
        g = Group(l, "Clicking")
        self._choice(g, "head_click", "Click with",
                     [("wink", "A wink (recommended)"), ("blink", "A long blink")],
                     "Wink: left eye left-clicks, right eye right-clicks. A long blink is there if winking is hard.")
        self._slider(g, "head_wink_ms", "Wink hold time", 100, 600, "{} ms", step=25,
                     hint="Ordinary blinks close both eyes and never click. The pointer holds still while winking.")
        self._check(g, "head_swap_winks", "Swap left and right winks",
                    "Only if the camera picture isn't mirrored and winks come out the wrong way round.")
        g = Group(l, "Face actions")
        self._choice(g, "head_mouth_action", "Open the mouth to",
                     [("drag", "Drag"), ("scroll", "Scroll"), ("click", "Click"), ("off", "Nothing")],
                     "Drag holds the button down while the mouth is open. Scroll: open, then nod up or down.")
        self._choice(g, "head_smile_action", "A held smile",
                     [("off", "Nothing"), ("pause", "Pause / resume"), ("double_click", "Double-click"),
                      ("right_click", "Right-click")],
                     "Needs a clear smile held for about a third of a second, so a laugh doesn't count.")
        self._snap_group(l)
        return self._done(page, l)

    def _snap_group(self, l: QVBoxLayout) -> None:
        g = Group(l, "Snap")
        self._check(g, "snap_enabled", "Snap the pointer still",
                    "Once the pointer settles it locks there and only lets go on a clear move. "
                    "Makes small buttons easier to hit.")
        self._slider(g, "snap_strength", "Snap strength", 1, 10, "{}% of screen",
                     "How far to move to pull out of a snap. Higher is steadier.")

    def _sign_page(self) -> QWidget:
        page, l = self._page("Sign language typing", "Types fingerspelling (the ASL alphabet) into whatever app "
                                                     "has the keyboard. It learns this person's own signs.")
        self._mode_row(l, "sign")
        g = Group(l, "Signs")
        self.teach_button = self._button("Teach signs…", self.main.open_teach_signs, "hand", accent=True)
        self.sign_status = g.row("No signs taught yet", "About two seconds per sign, about a minute for all. "
                                 "Needs sign language typing running.", self.teach_button).title_label
        g = Group(l, "Typing")
        self._slider(g, "sign_hold_ms", "Hold to type", 250, 1500, "{} ms", step=50,
                     hint="Shorter is faster; longer makes fewer mistakes.")
        self._slider(g, "sign_confidence", "Certainty", 5, 60, "{}%",
                     hint="Raise it if similar letters (M and N, A and S) get mixed up.")
        self._check(g, "sign_capitals", "Type capital letters")
        return self._done(page, l)

    def update_sign_status(self) -> None:
        book = sign_language.SignBook.from_json(app_settings.load_signs_text())
        n = len(book.taught)
        missing = [x for x in sign_language.LETTERS if x not in book.samples]
        text = f"{n} of {len(sign_language.SIGNS)} signs taught"
        if n and missing:
            text += f", not yet: {', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}"
        for extra, name in ((sign_language.SPACE, "space"), (sign_language.DELETE, "delete")):
            if n and extra not in book.samples:
                text += f"; no {name} sign yet"
        self.sign_status.setText(text if n else "No signs taught yet")
        ready = self.main.is_tracking() and self.main.settings.tracking_mode == "sign"
        self.teach_button.setEnabled(ready)
        self.teach_button.setToolTip("" if ready else "Switch to sign language typing and press Start first.")

    def _hand_page(self) -> QWidget:
        page, l = self._page("Hand gestures", "Point with the index finger to move. Pinch to click, pinch and "
                                              "hold to drag, two fingers up to scroll.")
        self._mode_row(l, "hand")
        g = Group(l, "Moving")
        self._slider(g, "smoothing", "Smoothing", 0, 100, "{}%",
                     "Higher holds the pointer steadier; lower follows faster. Small tremors are smoothed more.")
        self._slider(g, "reach", "Hand reach", 50, 100, "{}% of view",
                     "How much of the camera picture the fingertip sweeps to cover the screen. Lower means less "
                     "arm movement.")
        g = Group(l, "Clicking and dragging")
        self._check(g, "click_enabled", "Pinch to click", "Thumb and index: left-click where the pinch began.")
        self._slider(g, "pinch_threshold", "Pinch closes at", 10, 60, "{}% of hand",
                     "Raise it if pinches are missed; lower it if clicks happen too early.")
        self._slider(g, "pinch_release_gap", "Opens again at", 5, 40, "+{}%",
                     "A wider gap ignores more wobble.")
        self._slider(g, "click_stability", "Click steadiness", 1, 5, "{} frames",
                     "How many camera frames must agree before a pinch or release counts.")
        self._check(g, "drag_enabled", "Pinch and hold to drag")
        self._slider(g, "pinch_hold_ms", "Hold for", 150, 1500, "{} ms", step=10,
                     hint="How long a pinch is held before it becomes a press-and-hold.")
        g = Group(l, "Scrolling")
        self._check(g, "scroll_enabled", "Scroll with a hand pose")
        self._choice(g, "scroll_pose", "Pose",
                     [("two_fingers", "Index and middle up (recommended)"), ("index", "Index finger only")],
                     "Hold still to start, then move up or down. Further is faster. Index only scrolls by "
                     "accident more often.")
        self._slider(g, "scroll_sensitivity", "Speed", 5, 100, "{}%")
        self._slider(g, "scroll_dead_zone", "Dead zone", 5, 60, "{}% of hand",
                     "How far to move before scrolling starts.")
        self._check(g, "scroll_reverse", "Reverse direction")
        g = Group(l, "Hide gesture")
        box = ui_theme.ToggleSwitch()
        box.setAccessibleName("Hide gesture")
        box.toggled.connect(self._hide_toggled)
        self.controls["hide_gesture_enabled"] = box
        g.row("Hide gesture", "Hold up only the middle finger to stop tracking and put Finger Mouse away. "
                              "Never touches other apps.", box)
        self._choice(g, "hide_gesture_action", "When it's made",
                     [("tray", "Stop and hide to the tray"), ("minimize", "Stop and minimise"),
                      ("quit", "Stop and quit Finger Mouse")])
        self._slider(g, "hide_gesture_hold_ms", "Hold for", 600, 3000, "{} ms", step=100,
                     hint="Longer is safer against doing it by accident.")
        return self._done(page, l)

    def _eye_page(self) -> QWidget:
        page, l = self._page("Eye gaze (beta)", "Look where the pointer should go. Needs calibrating, and works "
                                                "best with the head fairly still and facing the camera.")
        self._mode_row(l, "eye")
        g = Group(l, "Calibration")
        self.calibrate_button = self._button("Calibrate…", self.main.open_calibration, "eye", accent=True)
        self.recentre_button = self._button("Re-centre", self.main.open_recentre, "refresh")
        self.recentre_button.setToolTip("Drifted since calibrating? Look at one dot for a second to fix it.")
        self.calibration_status = g.row("Not calibrated yet", "Needs eye gaze running. Re-centre fixes drift in "
                                        "one second.", self.calibrate_button, self.recentre_button).title_label
        self._choice(g, "eye_calibration_detail", "Calibration",
                     [("thorough", "Thorough, about 70 s (recommended)"), ("quick", "Quick, about 25 s")])
        g = Group(l, "Clicking")
        self._choice(g, "eye_click_mode", "Click by",
                     [("dwell", "Holding the gaze still (recommended)"), ("blink", "A deliberate blink"),
                      ("both", "Either one")])
        self._slider(g, "eye_dwell_ms", "Dwell time", 300, 2500, "{} ms", step=50,
                     hint="How long a steady gaze takes to click.")
        self._slider(g, "eye_dwell_radius", "Dwell steadiness", 1, 15, "{}% of screen",
                     hint="How far the gaze may drift and still count as still.")
        self._slider(g, "eye_blink_ms", "Blink hold time", 100, 800, "{} ms", step=25,
                     hint="How long an eye must stay shut to count as a deliberate blink.")
        g = Group(l, "Moving")
        self._slider(g, "eye_smoothing", "Smoothing", 0, 100, "{}%",
                     hint="Gaze is noisier than a hand, so this usually sits higher.")
        self._snap_group(l)
        return self._done(page, l)

    def _pointer_page(self) -> QWidget:
        page, l = self._page("Pointer", "How the pointer looks and which screen it covers. These apply to every "
                                        "way of steering.")
        g = Group(l)
        self._choice(g, "screen", "Screen", [("primary", "Main screen"), ("all", "All screens together")])
        self._check(g, "show_halo", "Show the halo", "A ring around the pointer so it's easy to find.")
        self._slider(g, "halo_size", "Halo size", 16, 80, "{} px")
        self._check(g, "pause_on_physical_mouse", "Pause while the real mouse moves",
                    "Someone helping can take over with the mouse or trackpad at any time.")
        g.row("System pointer size", "The pointer itself is sized in the system's own settings.",
              self._button("Open system settings", self.main.open_system_cursor_settings, "monitor"))
        return self._done(page, l)

    def _camera_page(self) -> QWidget:
        page, l = self._page("Camera", "Phone apps such as DroidCam or Iriun, and Continuity Camera on a Mac, "
                                       "appear here as ordinary cameras.")
        g = Group(l)
        self.camera_combo = QComboBox()
        self.camera_combo.setAccessibleName("Camera")
        self.camera_combo.currentIndexChanged.connect(self._camera_chosen)
        g.row("Camera", "Automatic tries real cameras before virtual ones.", self.camera_combo,
              self._button("Refresh", self.main.refresh_cameras, "refresh"), stack=True)

        self.stream_url = QLineEdit()
        self.stream_url.setPlaceholderText("http://192.168.1.20:8080/video")
        self.stream_url.setAccessibleName("Stream address")
        self.stream_url.setMinimumWidth(300)
        self.stream_url.editingFinished.connect(self._stream_url_done)
        self.stream_row = g.row("Stream address", "Experimental. Apps such as IP Webcam show an address like "
                                "this. Use it on a network you trust: plain http video isn't encrypted.",
                                self.stream_url, stack=True)
        self.stream_error = QLabel()
        self.stream_error.setObjectName("error")
        self.stream_row.layout().addWidget(self.stream_error)

        self.camera_info = QLabel("Start tracking to see what the camera delivers.")
        self.camera_info.setObjectName("rowHint")
        self.camera_info.setWordWrap(True)
        info_row = g.row("What it delivers", "")
        info_row.layout().itemAt(0).layout().addWidget(self.camera_info)

        g = Group(l, "Picture")
        self.resolution = self._choice(g, "camera_resolution", "Resolution",
                                       [(r, "Automatic (1280×720 if offered)" if r == "auto" else r.replace("x", "×"))
                                        for r in app_settings.RESOLUTIONS],
                                       "Some cameras show a wider area in 16:9 modes such as 1280×720.")
        self.detect_modes = self._button("Check what it supports", self._detect_modes)
        self.modes_note = QLabel()
        self.modes_note.setObjectName("rowHint")
        self.modes_note.setWordWrap(True)
        modes_row = g.row("Camera modes", " ", self.detect_modes)
        modes_row.layout().itemAt(0).layout().addWidget(self.modes_note)
        self.modes_reason = modes_row.hint_label

        self.zoom = QDoubleSpinBox()
        self.zoom.setRange(0, 1000)
        self.zoom.setDecimals(0)
        self.zoom.setAccessibleName("Camera zoom")
        self.zoom.valueChanged.connect(lambda v: None if self._loading else self._set("camera_zoom", v))
        self.driver_button = self._button("Driver settings…", lambda: self.main.camera_command("driver_settings"))
        zoom_row = g.row("Zoom", "", self.zoom, self.driver_button)
        self.zoom_label = zoom_row.title_label
        self._check(g, "mirror", "Mirror the picture", "Move right and the pointer goes right.")
        return self._done(page, l)

    def _caregiver_page(self) -> QWidget:
        page, l = self._page("Caregiver", "For whoever sets Finger Mouse up. Lock it so the settings can't be "
                                          "changed by accident, and leave a note for the next person who helps.")
        g = Group(l, "Lock")
        self.lock_switch = ui_theme.ToggleSwitch()
        self.lock_switch.setAccessibleName("Caregiver lock")
        self.lock_switch.toggled.connect(self._lock_toggled)
        g.row("Caregiver lock", "Only Home shows. To get back to setup, press and hold Caregiver at the bottom "
                                "of the sidebar for 3 seconds.", self.lock_switch)
        self.pin_button = self._button("Set a PIN…", self._set_pin)
        self.pin_remove = self._button("Remove PIN", self._remove_pin)
        self.pin_row = g.row("PIN (optional)", "Also asks for a PIN after the hold. Pick one the person using "
                             "Finger Mouse doesn't know.", self.pin_button, self.pin_remove)
        g = Group(l, "Handover note")
        self.summary = QLabel()
        self.summary.setObjectName("summary")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy = self._button("Copy note", self._copy_summary, "copy")
        row = g.row("How this person uses Finger Mouse", "Paste it into a care plan, an email or a sticky note.",
                    stack=True)
        row.layout().addWidget(self.summary)
        row.layout().addWidget(copy, 0, Qt.AlignmentFlag.AlignLeft)
        return self._done(page, l)

    def update_caregiver(self) -> None:
        s = self.main.settings
        self._loading, was = True, self._loading
        self.lock_switch.setChecked(s.caregiver_lock)
        self._loading = was
        self.pin_button.setText("Change PIN…" if s.caregiver_pin else "Set a PIN…")
        self.pin_remove.setVisible(bool(s.caregiver_pin))
        self.summary.setText(self.main.setup_summary())

    def _lock_toggled(self, on: bool) -> None:
        if self._loading:
            return
        self.main.set_caregiver_lock(on)

    def _set_pin(self) -> None:
        pin = PinDialog.ask_new(self.main)
        if pin:
            self.main.change_settings(caregiver_pin=app_settings.hash_pin(pin))
            self.update_caregiver()

    def _remove_pin(self) -> None:
        self.main.change_settings(caregiver_pin="")
        self.update_caregiver()

    def _copy_summary(self) -> None:
        QGuiApplication.clipboard().setText(self.main.setup_summary())
        self.main.flash_footnote("Handover note copied.")

    def _advanced_page(self) -> QWidget:
        page, l = self._page("Advanced", "For checking and reporting problems.")
        g = Group(l, "Preview")
        self._check(g, "show_landmarks", "Show tracking points in the preview")
        self._check(g, "show_gesture_state", "Show the gesture name in the preview")
        self._check(g, "show_diagnostics", "Show diagnostics", "Frame rates, timings and stalls under the preview.")
        g = Group(l, "Help and updates")
        self._check(g, "verbose_logging", "Detailed logging", "Useful when reporting a problem.")
        g.row("Log files", "", self._button("Open the log folder", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.main.log_dir)))))
        self._check(g, "check_for_updates", "Tell me when a new version is out",
                    "Checks GitHub once when the app opens. Nothing installs by itself.")
        g = Group(l, "Reset")
        g.row("Restore defaults", "Every setting goes back to how it was when installed. Taught signs are kept.",
              self._button("Restore defaults…", self._restore_defaults))
        about = QLabel(f"{APP_NAME} {APP_VERSION} · {PUBLISHER} · <a href='{SOURCE_URL}'>source code</a>")
        about.setOpenExternalLinks(True)
        about.setObjectName("footnote")
        l.addSpacing(12)
        l.addWidget(about)
        return self._done(page, l)

    # -- state ------------------------------------------------------------------
    def _register(self, key: str, control: Any) -> None:
        """A setting can appear on more than one page (snap is on both the head
        and the eye page); every copy is kept, and all of them reload."""
        if key in self.controls:
            self.twins.append((key, control))
        else:
            self.controls[key] = control

    def load(self, s: Settings) -> None:
        was = self._loading
        self._loading = True
        for key, control in list(self.controls.items()) + self.twins:
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
        self.update_sign_status()
        self.update_modes()
        self.update_caregiver()
        self._update_dependents()
        self._loading = was

    def _update_dependents(self) -> None:
        """Show only the settings that apply to the choices made above them."""
        relative = self.main.settings.head_pointer_mode == "relative"
        for row, show in ((self.accel_row, relative), (self.reach_row, not relative)):
            row.setVisible(show)
            if row.sep_line is not None:
                row.sep_line.setVisible(show)

    def update_modes(self) -> None:
        current = self.main.settings.tracking_mode
        for mode, (label, use) in self.mode_rows.items():
            in_use = mode == current
            label.setText(f"{MODE_NAMES[mode]} is the way of steering in use." if in_use
                          else f"In use now: {MODE_NAMES[current].lower()}.")
            use.setVisible(not in_use)

    def _set(self, key: str, value: Any) -> None:
        if self._loading:
            return
        self.main.change_settings(**{key: value})
        if key == "head_pointer_mode":
            self._update_dependents()
        if any(k == key for k, _ in self.twins):
            self.load(self.main.settings)       # bring the setting's other copy into line

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
        specific = self.main.settings.camera.startswith("cv:")
        self.detect_modes.setEnabled(not tracking and specific)
        reason = ("Press Stop first: checking needs the camera to itself." if tracking else
                  "" if specific else "Choose a specific camera above first, not Automatic.")
        self.modes_reason.setText(reason or "Asks the camera which picture sizes it can deliver.")
        self.detect_modes.setToolTip(reason)
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
            self.zoom_label.setText("Zoom: not offered by this camera")
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
        self.calibration_status.setText("Calibrated" if calibrated else "Not calibrated yet")
        ready = self.main.is_tracking() and self.main.settings.tracking_mode == "eye"
        self.calibrate_button.setEnabled(ready)
        self.recentre_button.setEnabled(ready and calibrated)
        self.calibrate_button.setToolTip("" if ready else "Switch to eye gaze and press Start first.")

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
            box = QMessageBox(self.main)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Turn on the hide gesture?")
            box.setText("Holding up only the middle finger will stop tracking and put Finger Mouse away.")
            box.setInformativeText(
                "It can happen by accident if that shape is made while working. It only affects "
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
        answer = QMessageBox.question(self.main, "Restore defaults?",
                                      "Put every setting back to how it was when Finger Mouse was installed? "
                                      "The caregiver lock and PIN are kept.")
        if answer == QMessageBox.StandardButton.Yes:
            s = self.main.settings
            self.main.replace_settings(Settings().copy(caregiver_lock=s.caregiver_lock, caregiver_pin=s.caregiver_pin))
            self.load(self.main.settings)

    def refresh_icons(self) -> None:
        c = ui_theme.colors()
        for b in self.findChildren(QPushButton):
            name = getattr(b, "icon_name", "")
            if name:
                b.setIcon(ui_theme.icon(name, c["on_accent"] if b.objectName() == "accent" else c["text"], 16))


class PinDialog(QDialog):
    """Ask for the caregiver PIN (4 to 8 digits)."""

    def __init__(self, parent: QWidget, title: str, prompt: str, confirm: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        text = QLabel(prompt)
        text.setWordWrap(True)
        layout.addWidget(text)
        self.fields = []
        for label in (("PIN", "Type it again") if confirm else ("PIN",)):
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setMaxLength(8)
            field.setPlaceholderText(label)
            field.setAccessibleName(label)
            field.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,8}"), field))
            layout.addWidget(field)
            self.fields.append(field)
        self.error = QLabel()
        self.error.setObjectName("error")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("accent")
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _check(self) -> None:
        pin = self.fields[0].text()
        if len(pin) < 4:
            self.error.setText("Use 4 to 8 digits.")
        elif len(self.fields) == 2 and self.fields[1].text() != pin:
            self.error.setText("The two PINs don't match.")
        else:
            self.accept()

    def pin(self) -> str:
        return self.fields[0].text()

    @classmethod
    def ask_new(cls, parent: QWidget) -> str:
        d = cls(parent, "Set a caregiver PIN", "4 to 8 digits. It's asked for after holding Caregiver.", confirm=True)
        return d.pin() if d.exec() == QDialog.DialogCode.Accepted else ""

    @classmethod
    def ask(cls, parent: QWidget) -> Optional[str]:
        d = cls(parent, "Caregiver PIN", "Type the caregiver PIN to open setup.")
        return d.pin() if d.exec() == QDialog.DialogCode.Accepted else None


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ModeTile(QFrame):
    """One way of steering on the Set up page: a big card that's a radio button, Grid 3 sized.
    A click anywhere on it picks it."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(84)
        self.radio: Optional[QRadioButton] = None

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        if self.radio is not None and self.rect().contains(event.position().toPoint()):
            self.radio.setChecked(True)
            self.radio.setFocus()
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, log_dir: Path) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.settings = app_settings.load()
        self.cameras: list[CameraInfo] = []
        self.capabilities: Optional[CameraCapabilities] = None
        self.engine: Optional[TrackingEngine] = None
        self.output: Optional[PointerOutput] = None
        self.settings_panel: Optional[SettingsPanel] = None
        self._unlocked = False             # a caregiver opened setup while the lock is on
        self._state = ("stopped", "Press Start to control the pointer.")   # what _set_status last showed
        self._results: "queue.Queue[tuple[Any, Any]]" = queue.Queue()
        self._output_events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._preview_seq = -1
        self._last_tick = time.monotonic()
        self._tick_times: list[float] = []
        self._fatal: Optional[str] = None
        self._quitting = False
        self._notice: Optional[tuple[str, str, float]] = None   # (chip, text, until)

        self.overlay = HaloOverlay()
        self.overlay.set_diameter(halo_diameter(self.settings))
        self.watchdog = Watchdog()
        self.watchdog.watch(Heartbeat("ui", self._ui_busy_since, 0.6, "window not responding"))
        self.watchdog.start()

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(900, 640)
        self.resize(1080, 780)
        self._build()
        self._apply_style()
        self._build_tray()
        try:   # follow the system's light or dark mode while open
            QGuiApplication.styleHints().colorSchemeChanged.connect(lambda _s: self._apply_style())
        except AttributeError:
            pass

        self.save_timer = QTimer(self, singleShot=True, interval=400)
        self.save_timer.timeout.connect(lambda: app_settings.save(self.settings))
        self.tick = QTimer(self, interval=16)      # ~60 Hz: halo, preview, events
        self.tick.timeout.connect(self._on_tick)
        self.tick.start()
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.stop_tracking)
        self.refresh_cameras()
        # a few seconds after opening: is there a newer version? (updates.py)
        self._update_result: Optional[dict] = None
        if self.settings.check_for_updates and not os.environ.get("FINGERMOUSE_NO_UPDATE_CHECK"):
            QTimer.singleShot(4000, self._start_update_check)

    # -- updates ----------------------------------------------------------------

    def _start_update_check(self) -> None:
        def work() -> None:
            rel = updates.fetch_latest()
            self._update_result = updates.offer(rel, APP_VERSION, self.settings.skipped_update) if rel else None
        threading.Thread(target=work, name="update-check", daemon=True).start()
        self._update_polls = 0
        self._update_timer = QTimer(self, interval=500)
        self._update_timer.timeout.connect(self._poll_update_check)
        self._update_timer.start()

    def _poll_update_check(self) -> None:
        self._update_polls += 1
        if self._update_result is None and self._update_polls < 40:   # 20 s, then give up quietly
            return
        self._update_timer.stop()
        if self._update_result and not self._quitting:
            self._offer_update(self._update_result)

    def _offer_update(self, o: dict) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Update available")
        box.setTextFormat(Qt.TextFormat.MarkdownText)
        box.setText(f"**{APP_NAME} {o['version']} is out**, you have {APP_VERSION}.\n\n"
                    f"**What's new**\n\n{o['notes']}")
        download = box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
        later = box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        skip = box.addButton("Skip this version", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(download)
        box.setEscapeButton(later)
        box.exec()
        if box.clickedButton() is download:
            QDesktopServices.openUrl(QUrl(o["url"]))
        elif box.clickedButton() is skip:
            self.replace_settings(self.settings.copy(skipped_update=o["version"]))

    # -- layout -----------------------------------------------------------------
    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("ground")
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- sidebar ------------------------------------------------------------
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(256)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 18, 12, 14)
        side.setSpacing(2)
        brand = QHBoxLayout()
        brand.setContentsMargins(10, 0, 0, 14)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(28, 28))
        name = QLabel(APP_NAME)
        name.setObjectName("appName")
        brand.addWidget(logo)
        brand.addSpacing(8)
        brand.addWidget(name, 1)
        side.addLayout(brand)

        self.stack = QStackedWidget()
        self.nav: dict[str, ui_theme.NavButton] = {}
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        def add_nav(key: str, icon_name: str, text: str, page: QWidget) -> None:
            button = ui_theme.NavButton(icon_name, text)
            button.clicked.connect(lambda: self.open_page(key))
            self._nav_group.addButton(button)
            self.nav[key] = button
            side.addWidget(button)
            self.stack.addWidget(page)
            page.setProperty("page_key", key)

        add_nav("home", "home", "Home", self._build_home())
        add_nav("setup", "setup", "Set up", self._build_setup())
        self.settings_label = QLabel("Settings")
        self.settings_label.setObjectName("secondary")
        self.settings_label.setContentsMargins(14, 14, 0, 4)
        side.addWidget(self.settings_label)
        self.settings_panel = SettingsPanel(self)
        for key, icon_name, text in SettingsPanel.PAGES:
            add_nav(key, icon_name, text, self.settings_panel.pages[key])
        side.addStretch(1)

        self.unlock_button = ui_theme.HoldButton("lock", "Caregiver: hold to open")
        self.unlock_button.setToolTip("Press and hold for 3 seconds to open setup.")
        self.unlock_button.held.connect(self._unlock_held)
        side.addWidget(self.unlock_button)
        self.relock_button = QPushButton("  Lock setup again")
        self.relock_button.setIcon(ui_theme.icon("lock", ui_theme.colors()["text"], 16))
        self.relock_button.icon_name = "lock"     # type: ignore[attr-defined]
        self.relock_button.clicked.connect(self._relock)
        side.addWidget(self.relock_button)
        version = QLabel(f"Version {APP_VERSION}")
        version.setObjectName("footnote")
        version.setContentsMargins(14, 8, 0, 0)
        side.addWidget(version)

        outer.addWidget(sidebar)
        outer.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav[self.settings.tracking_mode].set_badge("In use")
        self._apply_lock()
        self.open_page("home")

    def _build_home(self) -> QWidget:
        page = QWidget()
        page.setObjectName("ground")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 22)
        layout.setSpacing(14)

        card = QFrame()
        card.setObjectName("statusCard")
        grid = QGridLayout(card)
        grid.setContentsMargins(22, 20, 22, 18)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(8)
        self.state_disc = ui_theme.StateDisc()
        grid.addWidget(self.state_disc, 0, 0, 2, 1, Qt.AlignmentFlag.AlignTop)
        self.headline = QLabel("Not running")
        self.headline.setObjectName("headline")
        self.headline.setWordWrap(True)
        grid.addWidget(self.headline, 0, 1)
        line = QHBoxLayout()
        line.setSpacing(10)
        self.chip = QLabel("Stopped")
        self.chip.setObjectName("chip")
        self.status = QLabel("Press Start to control the pointer.")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Status")
        line.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)
        line.addWidget(self.status, 1)
        grid.addLayout(line, 1, 1)
        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("bigToggle")
        self.start_button.setProperty("running", False)
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(self.toggle_tracking)
        grid.addWidget(self.start_button, 0, 2, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        rule = QFrame()
        rule.setObjectName("rowLine")
        grid.addWidget(rule, 2, 0, 1, 3)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("howTo")
        self.subtitle.setWordWrap(True)
        self._update_subtitle()
        grid.addWidget(self.subtitle, 3, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        layout.addWidget(card)

        self.preview = QLabel()
        self.preview.setObjectName("preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(240)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.preview.setAccessibleName("Camera preview")
        # While stopped: what the picture area is for, and the fix when there's no camera.
        self.preview_empty = QWidget()
        self.preview_empty.setObjectName("previewEmpty")
        self.preview_empty.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        empty = QVBoxLayout(self.preview_empty)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setSpacing(14)
        self.preview_icon = QLabel()
        self.preview_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_text = QLabel()
        self.preview_text.setObjectName("previewText")
        self.preview_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_text.setWordWrap(True)
        self.preview_fix = QPushButton("  Look for cameras again")
        self.preview_fix.icon_name = "refresh"     # type: ignore[attr-defined]
        self.preview_fix.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_fix.clicked.connect(self.refresh_cameras)
        empty.addWidget(self.preview_icon)
        empty.addWidget(self.preview_text)
        empty.addWidget(self.preview_fix, 0, Qt.AlignmentFlag.AlignCenter)
        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.preview_empty)
        self.preview_stack.addWidget(self.preview)
        layout.addWidget(self.preview_stack, 1)

        self.diag = QLabel()
        self.diag.setObjectName("diag")
        self.diag.setWordWrap(True)
        self.diag.setVisible(self.settings.show_diagnostics)
        layout.addWidget(self.diag)

        self.footnote = QLabel()
        self.footnote.setObjectName("footnote")
        self.footnote.setWordWrap(True)
        self._footnote_text = "Esc stops Finger Mouse. Moving the real mouse pauses it for a moment."
        self.footnote.setText(self._footnote_text)
        layout.addWidget(self.footnote)
        return page

    def _build_setup(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        page.setObjectName("page")
        outer = QHBoxLayout(page)
        outer.setContentsMargins(36, 28, 36, 36)
        column = QWidget()
        column.setObjectName("page")
        column.setMaximumWidth(880)
        l = QVBoxLayout(column)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(8)
        outer.addWidget(column, 1)
        title = QLabel("Set up")
        title.setObjectName("pageTitle")
        l.addWidget(title)
        intro = QLabel("Four steps for whoever is setting Finger Mouse up. Everything saves by itself.")
        intro.setObjectName("pageIntro")
        intro.setWordWrap(True)
        l.addWidget(intro)
        l.addSpacing(10)
        self.steps: dict[int, tuple[QLabel, QLabel]] = {}

        def step(n: int, name: str, hint: str) -> QVBoxLayout:
            frame = QFrame()
            frame.setObjectName("group")
            box = QVBoxLayout(frame)
            box.setContentsMargins(20, 16, 20, 18)
            box.setSpacing(10)
            head = QHBoxLayout()
            head.setSpacing(10)
            number = QLabel(str(n))
            number.setObjectName("stepNumber")
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            number.setFixedSize(28, 28)
            heading = QLabel(name)
            heading.setObjectName("stepTitle")
            state = QLabel()
            state.setObjectName("stepState")
            head.addWidget(number)
            head.addWidget(heading, 1)
            head.addWidget(state)
            box.addLayout(head)
            if hint:
                h = QLabel(hint)
                h.setObjectName("rowHint")
                h.setWordWrap(True)
                h.setContentsMargins(38, 0, 0, 0)
                box.addWidget(h)
            body = QVBoxLayout()
            body.setContentsMargins(38, 4, 0, 0)
            body.setSpacing(8)
            box.addLayout(body)
            l.addWidget(frame)
            l.addSpacing(6)
            self.steps[n] = (number, state)
            return body

        body = step(1, "Choose the camera", "Pick the webcam that faces the person. Automatic tries real "
                                            "cameras first.")
        row = QHBoxLayout()
        self.camera_combo = QComboBox()
        self.camera_combo.setAccessibleName("Camera")
        self.camera_combo.setMinimumWidth(240)
        self.camera_combo.currentIndexChanged.connect(self._quick_camera)
        refresh = QPushButton("  Refresh")
        refresh.setIcon(ui_theme.icon("refresh", ui_theme.colors()["text"], 16))
        refresh.icon_name = "refresh"            # type: ignore[attr-defined]
        refresh.clicked.connect(self.refresh_cameras)
        row.addWidget(self.camera_combo)
        row.addWidget(refresh)
        row.addStretch(1)
        body.addLayout(row)

        body = step(2, "Choose the way to steer", "")
        self.mode_group = QButtonGroup(self)
        self.mode_radios: dict[str, QRadioButton] = {}
        descriptions = {
            "head": "Move the head to steer, wink or blink to click. For limited hand movement. No calibration.",
            "sign": "Fingerspell letters (ASL) to type into any app. Teach this person's signs first.",
            "hand": "Point with a finger, pinch to click and drag, two fingers up to scroll. Needs a steady hand.",
            "eye": "Look where the pointer should go, hold the gaze or blink to click. Needs calibrating. Beta.",
        }
        self.mode_tiles: dict[str, QFrame] = {}
        tiles = QGridLayout()
        tiles.setSpacing(10)
        for i, mode in enumerate(("head", "sign", "hand", "eye")):
            tile = ModeTile()
            box = QHBoxLayout(tile)
            box.setContentsMargins(14, 14, 16, 14)
            box.setSpacing(12)
            radio = QRadioButton()
            radio.setAccessibleName(MODE_NAMES[mode])
            radio.setCursor(Qt.CursorShape.PointingHandCursor)
            radio.toggled.connect(lambda on, m=mode: on and self._mode_picked(m))
            pic = QLabel()
            pic.setPixmap(ui_theme.icon_pixmap(ui_theme.MODE_ICONS[mode], ui_theme.colors()["text"], 32))
            pic.setProperty("icon_name", ui_theme.MODE_ICONS[mode])
            pic.setProperty("icon_size", 32)
            texts = QVBoxLayout()
            texts.setSpacing(4)
            name = QLabel(MODE_NAMES[mode] + (" (beta)" if mode == "eye" else ""))
            name.setObjectName("tileTitle")
            desc = QLabel(descriptions[mode])
            desc.setObjectName("rowHint")
            desc.setWordWrap(True)
            texts.addWidget(name)
            texts.addWidget(desc)
            texts.addStretch(1)
            box.addWidget(radio, 0, Qt.AlignmentFlag.AlignTop)
            box.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
            box.addLayout(texts, 1)
            tile.radio = radio
            tiles.addWidget(tile, i, 0)
            self.mode_group.addButton(radio)
            self.mode_radios[mode] = radio
            self.mode_tiles[mode] = tile
        body.addLayout(tiles)
        self._tiles_layout = tiles
        self._tile_columns = 1

        body = step(3, "Check it works", "")
        row = QHBoxLayout()
        row.setSpacing(14)
        self.setup_toggle = QPushButton("Start")
        self.setup_toggle.setObjectName("bigToggle")
        self.setup_toggle.setProperty("running", False)
        self.setup_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setup_toggle.clicked.connect(self.toggle_tracking)
        self.setup_live = QLabel("Not running.")
        self.setup_live.setObjectName("status")
        self.setup_live.setWordWrap(True)
        row.addWidget(self.setup_toggle)
        row.addWidget(self.setup_live, 1)
        body.addLayout(row)
        self.setup_try = QLabel()
        self.setup_try.setWordWrap(True)
        body.addWidget(self.setup_try)
        self.setup_action = QPushButton()
        self.setup_action.setObjectName("accent")
        self.setup_action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setup_action.clicked.connect(self._setup_action_clicked)
        body.addWidget(self.setup_action, 0, Qt.AlignmentFlag.AlignLeft)

        body = step(4, "Fine-tune and hand over", "Adjust speed, clicking and steadiness for this person. Then "
                                                   "turn on the lock if you like, and copy a note for the next "
                                                   "person who helps.")
        row = QHBoxLayout()
        self.tune_button = QPushButton()
        self.tune_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tune_button.clicked.connect(lambda: self.open_page(self.settings.tracking_mode))
        handover = QPushButton("Caregiver lock and note")
        handover.setCursor(Qt.CursorShape.PointingHandCursor)
        handover.clicked.connect(lambda: self.open_page("caregiver"))
        row.addWidget(self.tune_button)
        row.addWidget(handover)
        row.addStretch(1)
        body.addLayout(row)
        l.addStretch(1)
        scroll.setWidget(page)
        self._update_setup()
        return scroll

    # -- pages, lock -----------------------------------------------------------
    def open_page(self, key: str) -> None:
        if key not in self.nav or not self.nav[key].isVisible() and self.nav[key].parent() is not None \
                and self._locked_out():
            key = "home"
        self.nav[key].setChecked(True)
        for i in range(self.stack.count()):
            if self.stack.widget(i).property("page_key") == key:
                self.stack.setCurrentIndex(i)
        if key == "caregiver" and self.settings_panel is not None:
            self.settings_panel.update_caregiver()

    def current_page(self) -> str:
        return str(self.stack.currentWidget().property("page_key"))

    def _locked_out(self) -> bool:
        return self.settings.caregiver_lock and not self._unlocked

    def _apply_lock(self) -> None:
        locked_out = self._locked_out()
        for key, button in self.nav.items():
            button.setVisible(key == "home" or not locked_out)
        self.settings_label.setVisible(not locked_out)
        self.unlock_button.setVisible(locked_out)
        self.relock_button.setVisible(self.settings.caregiver_lock and self._unlocked)
        if locked_out and self.current_page() != "home":
            self.open_page("home")

    def set_caregiver_lock(self, on: bool) -> None:
        if on:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("Turn on the caregiver lock?")
            box.setText("Only Home will show. To open setup again, press and hold Caregiver at the bottom of "
                        "the sidebar for 3 seconds" + (", then type the PIN." if self.settings.caregiver_pin else "."))
            lock = box.addButton("Turn on the lock", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not lock:
                self.settings_panel.update_caregiver()
                return
        self.change_settings(caregiver_lock=on)
        self._unlocked = False if on else self._unlocked
        self._apply_lock()
        if on:
            self.open_page("home")

    def _unlock_held(self) -> None:
        if self.settings.caregiver_pin:
            pin = PinDialog.ask(self)
            if pin is None:
                return
            if not app_settings.pin_matches(self.settings.caregiver_pin, pin):
                QMessageBox.warning(self, "Wrong PIN", "That PIN isn't right. Setup stays locked.")
                return
        self._unlocked = True
        self._apply_lock()
        self.open_page("setup")

    def _relock(self) -> None:
        self._unlocked = False
        self._apply_lock()

    def flash_footnote(self, text: str) -> None:
        self.footnote.setText(text)
        QTimer.singleShot(3000, lambda: self.footnote.setText(self._footnote_text))

    def setup_summary(self) -> str:
        """A plain note on how this person uses Finger Mouse, for the next person who helps."""
        s = self.settings
        mode = MODE_NAMES[s.tracking_mode]
        lines = [f"Finger Mouse: {mode}"]
        if s.tracking_mode == "head":
            lines.append("Steer: move the head (" + ("like a mouse" if s.head_pointer_mode == "relative"
                                                       else "point at the spot") + ").")
            lines.append("Click: " + ("wink (left eye left-click, right eye right-click)." if s.head_click == "wink"
                                      else "a long blink."))
            mouth = {"drag": "drag", "scroll": "scroll (nod up or down)", "click": "click", "off": ""}[s.head_mouth_action]
            if mouth:
                lines.append(f"Open the mouth: {mouth}.")
            smile = {"pause": "pause or resume", "double_click": "double-click", "right_click": "right-click",
                     "off": ""}[s.head_smile_action]
            if smile:
                lines.append(f"Held smile: {smile}.")
            if s.snap_enabled:
                lines.append("Snap is on: the pointer locks still once it settles.")
        elif s.tracking_mode == "sign":
            lines.append("Hold each letter steady for a moment to type it. Relax the hand between double letters.")
        elif s.tracking_mode == "eye":
            lines.append("Click: " + {"dwell": "hold the gaze still.", "blink": "a deliberate blink.",
                                      "both": "hold the gaze still or blink."}[s.eye_click_mode])
            lines.append("If the pointer drifts: Settings, Eye gaze, Re-centre.")
        else:
            lines.append("Point with the index finger. Pinch to click, pinch and hold to drag, two fingers up to scroll.")
        camera = next((c.display_name for c in self.cameras if c.id == s.camera), None)
        lines.append("Camera: " + (camera or {"auto": "Automatic", "url": "phone or network stream"}.get(s.camera, s.camera)) + ".")
        lines.append("Start and stop: the big button on Home. Esc always stops it.")
        if s.caregiver_lock:
            lines.append("Setup is locked: hold Caregiver at the bottom of the sidebar for 3 seconds"
                         + (" and type the PIN." if s.caregiver_pin else "."))
        return "\n".join(lines)

    def _mode_picked(self, mode: str) -> None:
        if mode != self.settings.tracking_mode:
            self.change_settings(tracking_mode=mode)

    def _setup_action_clicked(self) -> None:
        mode = self.settings.tracking_mode
        if mode == "head":
            self.recentre_head()
        elif mode == "eye":
            self.open_calibration()
        elif mode == "sign":
            self.open_teach_signs()

    def _update_setup(self) -> None:
        """Keep the Set up page's steps in line with what's actually happening."""
        if not hasattr(self, "steps"):
            return
        s = self.settings
        mode = s.tracking_mode
        radio = self.mode_radios[mode]
        if not radio.isChecked():
            radio.blockSignals(True)
            radio.setChecked(True)
            radio.blockSignals(False)
        for m, tile in self.mode_tiles.items():
            if tile.property("checked") != (m == mode):
                tile.setProperty("checked", m == mode)
                tile.style().unpolish(tile)
                tile.style().polish(tile)
        tracking = self.is_tracking()
        seen = bool(tracking and self.engine and self.engine.view.tracked)
        n_cams = len(self.cameras)
        receiving = self.capabilities is not None
        self._step_state(1, receiving or n_cams > 0 or s.camera == "url",
                         "Receiving" if receiving else (f"{n_cams} found" if n_cams else
                                                        ("Phone stream" if s.camera == "url" else "None found yet")))
        self._step_state(2, True, MODE_NAMES[mode])
        ready = seen and (mode != "eye" or bool(s.eye_calibration))
        self._step_state(3, ready, "Working" if ready else ("Running" if tracking else "Not checked yet"))
        self._step_state(4, False, "")
        what = {"head": "Ask them to move their head: the pointer should follow. Then a wink: it should click. "
                        "If the pointer is off to one side, press Re-centre.",
                "sign": "Press Teach signs and go through the letters together, about a minute. Then open any app "
                        "and sign a letter.",
                "hand": "Ask them to point: the pointer should follow the fingertip. A quick pinch should click.",
                "eye": "Press Calibrate and have them look at each dot as it appears. Then look around: the "
                       "pointer should follow."}[mode]
        self.setup_try.setText(what)
        action = {"head": "Re-centre", "eye": "Calibrate…", "sign": "Teach signs…"}.get(mode)
        self.setup_action.setVisible(action is not None)
        if action:
            self.setup_action.setText(action)
            self.setup_action.setEnabled(tracking)
            self.setup_action.setToolTip("" if tracking else "Press Start first.")
        self.tune_button.setText(f"{MODE_NAMES[mode]} settings")

    def _step_state(self, n: int, done: bool, text: str) -> None:
        number, state = self.steps[n]
        if state.text() != text:
            state.setText(text)
        for w in (number, state):
            if w.property("done") != done:
                w.setProperty("done", done)
                w.style().unpolish(w)
                w.style().polish(w)
        if done:   # a drawn check mark in the number's place, and the words beside it
            number.setPixmap(ui_theme.icon_pixmap("check", ui_theme.colors()["on_accent"], 16, 3))
            number.setAccessibleName(f"Step {n} done")
        else:
            number.setText(str(n))
            number.setAccessibleName(f"Step {n}")

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._flow_tiles()

    def _flow_tiles(self) -> None:
        """Two columns of steering tiles when there's room, one when the window is narrow."""
        if not hasattr(self, "_tiles_layout"):
            return
        columns = 2 if self.stack.width() >= 860 else 1
        if columns == self._tile_columns:
            return
        self._tile_columns = columns
        for i, mode in enumerate(("head", "sign", "hand", "eye")):
            tile = self.mode_tiles[mode]
            self._tiles_layout.removeWidget(tile)
            self._tiles_layout.addWidget(tile, i // columns, i % columns)
        for c in range(2):
            self._tiles_layout.setColumnStretch(c, 1 if c < columns else 0)

    def _update_preview_empty(self) -> None:
        if not hasattr(self, "preview_empty"):
            return
        c = ui_theme.colors()
        no_camera = not self.cameras and self.settings.camera != "url"
        self.preview_icon.setPixmap(ui_theme.icon_pixmap("camera_off" if no_camera else "camera",
                                                         c["preview_text"], 48, 1.6))
        self.preview_text.setText("No camera found. Plug one in, or allow camera access, then look again."
                                  if no_camera else "The camera picture shows here while Finger Mouse is running.")
        self.preview_fix.setVisible(no_camera)

    def _update_subtitle(self) -> None:
        s = self.settings
        if s.tracking_mode == "eye":
            text = "Look where the pointer should go. Hold the gaze still (or blink) to click."
            if not s.eye_calibration:
                text += " Calibrate first: Set up, step 3."
        elif s.tracking_mode == "sign":
            text = "Fingerspell to type into any app: hold each letter steady for a moment."
        elif s.tracking_mode == "head":
            mouth = {"drag": " Open the mouth to drag.", "scroll": " Open the mouth and nod to scroll.",
                     "click": " Open the mouth to click.", "off": ""}[s.head_mouth_action]
            click = ("Wink to click: left eye left-click, right eye right-click."
                     if s.head_click == "wink" else "Hold a long blink to click.")
            text = f"Move the head to steer. {click}{mouth}"
        else:
            text = "Point with the index finger. Pinch to click, pinch and hold to drag, two fingers up to scroll."
        self.subtitle.setText(text)

    def _apply_style(self) -> None:
        sheet = ui_theme.stylesheet()
        self.setStyleSheet(sheet)
        c = ui_theme.colors()
        for b in self.findChildren(QPushButton):
            name = getattr(b, "icon_name", "")
            if name:
                b.setIcon(ui_theme.icon(name, c["on_accent"] if b.objectName() == "accent" else c["text"], 16))
        for pic in self.findChildren(QLabel):
            name = pic.property("icon_name")
            if name:
                pic.setPixmap(ui_theme.icon_pixmap(str(name), c["text"], int(pic.property("icon_size") or 22)))
        self._style_toggles()
        self._update_preview_empty()
        self._set_status(*self._state, force=True)
        for w in self.findChildren(QWidget):
            if isinstance(w, (ui_theme.NavButton, ui_theme.ToggleSwitch, ui_theme.HoldButton, ui_theme.StateDisc)):
                w.update()

    def _style_toggles(self) -> None:
        running = self.is_tracking()
        c = ui_theme.colors()
        for b in (self.start_button, self.setup_toggle):
            b.setText("  Stop" if running else "  Start")
            b.setIcon(ui_theme.icon("stop" if running else "play", c["text"] if running else c["on_accent"], 18))
            if b.property("running") != running:
                b.setProperty("running", running)
                b.style().unpolish(b)
                b.style().polish(b)
        self.start_button.setToolTip("Stop (Esc)" if running else "Start controlling the pointer")

    def _build_tray(self) -> None:
        self.tray: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        show = QAction("Show Finger Mouse", self, triggered=self.show_window)
        self.tray_toggle = QAction("Start", self, triggered=self.toggle_tracking)
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
        self.overlay.set_diameter(halo_diameter(new))
        self.diag.setVisible(new.show_diagnostics)
        if new.verbose_logging != old.verbose_logging:
            logging.getLogger().setLevel(logging.DEBUG if new.verbose_logging else logging.INFO)
        if any(getattr(new, k) != getattr(old, k)
               for k in ("tracking_mode", "head_click", "head_mouth_action", "eye_calibration")):
            self._update_subtitle()
        if new.tracking_mode != old.tracking_mode:
            if self.settings_panel is not None:
                self.settings_panel.update_calibration_status()
                self.settings_panel.update_sign_status()
                self.settings_panel.update_modes()
            for key, button in self.nav.items():
                button.set_badge("In use" if key == new.tracking_mode else "")
            self._set_status(*self._state, force=True)
        if new.caregiver_lock != old.caregiver_lock:
            self._apply_lock()
        if self.settings_panel is not None and self.current_page() == "caregiver":
            self.settings_panel.update_caregiver()
        self._update_setup()
        if new.camera != old.camera:
            self._select_quick_camera()
            if self.settings_panel is not None:
                self.settings_panel.update_camera_info()
        if self.engine is not None:
            self.engine.update_settings(new)
            if new.camera != old.camera or new.camera_resolution != old.camera_resolution:
                self.capabilities = None
        if self.output is not None:
            self.output.pause_on_physical_mouse = new.pause_on_physical_mouse

    def open_settings(self, page: Optional[str] = None) -> None:
        """Show a settings page; by default the one for the way of steering in use."""
        self.settings_panel.load(self.settings)
        self.show_window()
        self.open_page(page or self.settings.tracking_mode)

    def open_calibration(self) -> None:
        if not self._eye_ready():
            return
        dialog = CalibrationDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self.settings_panel is not None:
                self.settings_panel.load(self.settings)
            err = dialog.accuracy
            if err is None:
                note = "Look around to try it."
            else:
                screen = QGuiApplication.primaryScreen().size()
                px = int(err * (screen.width() ** 2 + screen.height() ** 2) ** 0.5 / 1.414)
                quality = ("Great" if err < 0.035 else "Good" if err < 0.06 else "Rough, calibrating again in "
                           "even light, with your head still, usually helps")
                where = "on check points it didn't train on" if dialog.accuracy_is_held_out else "on the calibration points"
                note = f"Average error {where}: about {err * 100:.1f}% of the screen (~{px} px). {quality}."
            QMessageBox.information(self, "Calibrated", f"Eye tracking is calibrated. {note}" + chr(10) * 2 +
                                    "If it drifts later (someone moved, the laptop moved), use Re-centre on "
                                    "the Eye gaze page: one second, no recalibrating.")

    def open_teach_signs(self) -> None:
        if self.engine is None or self.output is None or self.settings.tracking_mode != "sign":
            QMessageBox.information(self, "Start sign language typing first",
                "In Set up, choose Sign language typing and press Start, then teach the signs.")
            return
        TeachSignsDialog(self).exec()
        if self.settings_panel is not None:
            self.settings_panel.update_sign_status()

    def recentre_head(self) -> None:
        if self.engine is None or self.settings.tracking_mode != "head":
            QMessageBox.information(self, "Start the head pointer first",
                "In Set up, choose Head pointer and press Start, then re-centre.")
            return
        self.engine.recentre_head()

    def open_recentre(self) -> None:
        if not self._eye_ready():
            return
        if not GazeCalibration.from_json(self.settings.eye_calibration).is_calibrated:
            QMessageBox.information(self, "Calibrate first", "Re-centring adjusts a calibration, calibrate first.")
            return
        if CalibrationDialog(self, recentre=True).exec() == QDialog.DialogCode.Accepted and self.settings_panel:
            self.settings_panel.load(self.settings)

    def _eye_ready(self) -> bool:
        if self.engine is None or self.output is None or self.settings.tracking_mode != "eye":
            QMessageBox.information(self, "Start eye tracking first",
                "In Set up, choose Eye gaze and press Start, then calibrate.")
            return False
        return True

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
        if self.settings_panel is not None:
            self.settings_panel.populate_cameras()
        self._update_setup()
        self._update_preview_empty()

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
            if self.settings_panel is not None:
                self.settings_panel.populate_cameras()

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
        self._style_toggles()
        if self.tray:
            self.tray_toggle.setText("Stop")
        if self.settings_panel is not None:
            self.settings_panel.update_calibration_status()
            self.settings_panel.update_sign_status()
            self.settings_panel.update_camera_info()
        self.preview_stack.setCurrentWidget(self.preview)
        self._set_status("starting", "Starting the camera…")
        self._update_setup()
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
        self._style_toggles()
        if self.tray:
            self.tray_toggle.setText("Start")
        self._set_status("error" if self._fatal else "stopped",
                         self._fatal or "Press Start to control the pointer again.")
        self.preview.setPixmap(QPixmap())
        self.preview_stack.setCurrentWidget(self.preview_empty)
        self._update_preview_empty()
        self._update_setup()
        if self.settings_panel is not None:
            self.settings_panel.update_camera_info()
            self.settings_panel.update_calibration_status()
            self.settings_panel.update_sign_status()
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
                if self.settings_panel is not None:
                    self.settings_panel.update_camera_info()
                self._update_setup()
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

    def _set_status(self, gesture: str, text: str, force: bool = False) -> None:
        """Show the state on Home (disc, headline, chip, detail) and on Set up.
        Each state has its own words and icon; colour only backs them up."""
        if (gesture, text) == self._state and not force:
            return
        changed = gesture != self._state[0] or force
        self._state = (gesture, text)
        names = {"ready": "Ready", "pinched": "Pinch", "dragging": "Dragging", "scrolling": "Scrolling",
                 "paused": "Paused", "hide": "Hide", "no_hand": "No hand", "open": "Tracking",
                 "pointing": "Tracking", "starting": "Starting", "stopped": "Stopped", "error": "Stopped",
                 "gazing": "Tracking", "dwelling": "Click", "no_face": "No face", "uncalibrated": "Not calibrated"}
        mode = self.settings.tracking_mode
        what = ui_theme.tone(gesture)
        headline, icon_name = {
            "stopped": ("Not running", "power"),
            "error": ("Stopped by a problem", "alert"),
            "starting": ("Starting…", "loader"),
            "no_hand": ("Can't see a hand", "scan"),
            "no_face": ("Can't see a face", "scan"),
            "paused": ("Paused", "pause"),
            "hide": ("Hiding…", "pause"),
            "uncalibrated": ("Eye gaze needs calibrating", "alert"),
        }.get(gesture, (f"{MODE_NAMES[mode]} is on", ui_theme.MODE_ICONS[mode]))
        if self.status.text() != text:
            self.status.setText(text)
        if self.setup_live.text() != (text or headline):
            self.setup_live.setText(text or headline)
        if not changed:
            return
        c = ui_theme.colors()
        self.headline.setText(headline)
        self.state_disc.set_state(what, icon_name)
        self.chip.setText(names.get(gesture, "Tracking"))
        self.chip.setStyleSheet(f"background: {c[what + '_bg']}; color: {c[what]};")
        self._update_setup()

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
    ui_theme.apply_app_font(app)
    app.setQuitOnLastWindowClosed(False)   # hiding to the tray must not quit
    window = MainWindow(log_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
