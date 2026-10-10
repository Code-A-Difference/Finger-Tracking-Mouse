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
    from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, QUrl
    from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QGuiApplication, QIcon, QImage,
                               QKeySequence, QPainter, QPen, QPixmap, QShortcut)
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                                   QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow,
                                   QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider,
                                   QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget)

    import app_settings
    from app_settings import Settings
    from app_version import APP_NAME, APP_VERSION, PUBLISHER, SOURCE_URL
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
        # A window of its own doesn't inherit the main window's look: give it the same dark theme.
        sheet = main.styleSheet() if isinstance(main, QWidget) else ""
        self.setStyleSheet(sheet + """
            QDialog { background: #0e1626; color: #e8edf6; }
            QListWidget { background: #111a2b; color: #e8edf6; border: 1px solid #27344a; border-radius: 8px;
                          font-size: 14px; }
            QListWidget::item:selected { background: #22d3ee; color: #05070d; }
            QPushButton { background: #26344b; color: #e8edf6; border-radius: 8px; padding: 7px 12px; }
            QPushButton:hover { background: #31425f; }
            QProgressBar { background: #182338; border: none; border-radius: 4px; max-height: 8px; }
            QProgressBar::chunk { background: #22d3ee; border-radius: 4px; }
        """)
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
        self.letter.setStyleSheet("font-size: 84px; font-weight: 800; color: #e8edf6;")
        right.addWidget(self.letter)
        self.how = QLabel("")
        self.how.setWordWrap(True)
        self.how.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.how.setStyleSheet("font-size: 15px; color: #aebbd0;")
        right.addWidget(self.how)
        self.status = QLabel("Pick “Teach all” to go through every sign, or choose one on the left.")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setStyleSheet("font-size: 14px; color: #fbbf24;")
        right.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        right.addWidget(self.bar)
        self.seeing = QLabel("I see:, ")
        self.seeing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seeing.setStyleSheet("font-size: 15px; color: #34d399;")
        right.addWidget(self.seeing)
        right.addStretch(1)

        row = QHBoxLayout()
        self.teach_all = QPushButton("Teach all")
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
        if self.main.settings_dialog is not None:
            self.main.settings_dialog.update_sign_status()

    def done(self, result: int) -> None:  # noqa: D401 (Qt name)
        self._tick.stop()
        if self.main.engine is not None:
            self.main.engine.teaching = False
        super().done(result)


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
        self.twins: list[tuple[str, Any]] = []

        tabs = QTabWidget()
        for build, name in ((self._pointer_tab, "Pointer"), (self._click_tab, "Click && drag"),
                            (self._scroll_tab, "Scrolling"), (self._head_tab, "Head pointer"), (self._eye_tab, "Eye tracking"), (self._sign_tab, "Sign language"),
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
        self._register(key, (slider, value, fmt))
        return slider

    def _check(self, layout: QVBoxLayout, key: str, label: str, hint: str = "") -> QCheckBox:
        box = QCheckBox(label)
        box.toggled.connect(lambda on: self._set(key, on))
        layout.addWidget(box)
        if hint:
            self._hint(layout, hint)
        self._register(key, box)
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
        self._register(key, combo)
        return combo

    # -- tabs ---------------------------------------------------------------
    def _pointer_tab(self) -> QWidget:
        page, l = self._page()
        self._choice(l, "tracking_mode", "Tracking mode",
                     [("hand", "Hand gestures"), ("head", "Head pointer (nose + winks)"), ("eye", "Eye gaze (beta)"),
                      ("sign", "Sign language typing")],
                     "Move the pointer with a hand pinching to click, by moving your head (the nose steers, "
                     "winks click), or by looking at the screen. The settings below are for hand mode; the "
                     "head pointer and eye gaze have their own tabs.")
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
                    "When you move your real mouse, hand control steps aside for a moment, "
                    "also a quick way to reach the Stop button.")
        sys_cursor = QPushButton("System pointer size…")
        sys_cursor.clicked.connect(self.main.open_system_cursor_settings)
        l.addWidget(sys_cursor, 0, Qt.AlignmentFlag.AlignLeft)
        l.addStretch(1)
        return page

    def _click_tab(self) -> QWidget:
        page, l = self._page()
        self._hint(l, "<b>Quick pinch</b> (thumb and index): left-click where the pinch began. "
                      "<b>Pinch and hold</b>: press and hold the button, move to drag, open your fingers "
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

    def _head_tab(self) -> QWidget:
        page, l = self._page()
        self._hint(l, "Steer with your nose, the steadiest point on a face, so talking or smiling doesn't move "
                      "the pointer. Wink to click. No calibration needed: pick Head pointer under Pointer → "
                      "Tracking mode and press Start.")
        self._choice(l, "head_pointer_mode", "Movement",
                     [("relative", "Like a mouse (recommended)"), ("absolute", "Point at the spot")],
                     "Like a mouse: small, careful head movements are precise and a quick flick crosses the "
                     "screen; turning back doesn't bring the pointer back. Point at the spot is simpler, but "
                     "needs Re-centre if you shift in your seat.")
        self._slider(l, "head_speed", "Speed", 1, 100, "{}%")
        self._slider(l, "head_acceleration", "Acceleration", 0, 100, "{}%",
                     "How much further a quick movement goes than a slow one (Like a mouse only).")
        self._slider(l, "head_reach", "Turn to reach the edge", 10, 80, "{}% of face width",
                     "Point at the spot only: smaller means less turning to reach the screen edges.")
        self._slider(l, "head_dead_zone", "Steadiness", 0, 100, "{}%",
                     "Head movement slower than this is ignored, so tremor or breathing doesn't drift the pointer.")
        self._slider(l, "head_smoothing", "Smoothing", 0, 100, "{}%")
        self._check(l, "snap_enabled", "Snap, hold the pointer still once it settles",
                    "Once the pointer has stayed in one spot for a moment it locks there, perfectly still, "
                    "and only lets go when you clearly move away. Makes small buttons much easier to hit.")
        self._slider(l, "snap_strength", "Snap strength", 1, 10, "{}% of the screen",
                     "How far you have to move to pull the pointer out of a snap. Higher is steadier; lower "
                     "lets go sooner.")
        recentre = QPushButton("Re-centre now")
        recentre.setToolTip("Point at the spot: makes where your head is now the centre. Like a mouse: puts the "
                            "pointer in the middle of the screen.")
        recentre.clicked.connect(self.main.recentre_head)
        l.addWidget(recentre, 0, Qt.AlignmentFlag.AlignLeft)
        l.addSpacing(6)
        self._choice(l, "head_click", "Click with",
                     [("wink", "A wink (recommended)"), ("blink", "A long blink")],
                     "Wink: left eye left-clicks, right eye right-clicks. A long blink with both eyes is there if winking is hard.")
        self._slider(l, "head_wink_ms", "Wink hold time", 100, 600, "{} ms", step=25,
                     hint="How long to hold a wink before it clicks. Ordinary blinks close both eyes and never "
                          "click; the pointer holds still while you wink so the click lands where you aimed.")
        self._check(l, "head_swap_winks", "Swap left and right winks",
                    "Only if your camera shows an un-mirrored picture and winks come out the wrong way round.")
        self._choice(l, "head_mouth_action", "Open your mouth to",
                     [("drag", "Drag"), ("scroll", "Scroll"), ("click", "Click"), ("off", "Nothing")],
                     "Drag holds the button down while your mouth is open. Scroll: open your mouth, then nod down or up, the further, the faster.")
        self._choice(l, "head_smile_action", "A held smile",
                     [("off", "Nothing"), ("pause", "Pause / resume"), ("double_click", "Double-click"),
                      ("right_click", "Right-click")],
                     "Needs a clear smile held for about a third of a second, so a passing grin doesn't count. Pause / resume is handy for talking to someone without the pointer moving.")
        l.addStretch(1)
        return page

    def _sign_tab(self) -> QWidget:
        page, l = self._page()
        self._hint(l, "Type by fingerspelling (the ASL alphabet) into whatever app has the keyboard. First show "
                      "Finger Mouse your own signs, about two seconds each, so it recognises <i>your</i> hand. "
                      "Then hold each letter steady for a moment to type it; relax your hand for a moment between "
                      "double letters. You also teach a sign for space and one for delete.")
        self.sign_status = QLabel()
        self.sign_status.setObjectName("value")
        l.addWidget(self.sign_status)
        self.teach_button = QPushButton("Teach signs…")
        self.teach_button.clicked.connect(self.main.open_teach_signs)
        l.addWidget(self.teach_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._hint(l, "Teaching needs tracking running in sign mode: pick Sign language typing under Pointer → "
                      "Tracking mode, press Start tracking, then come back here.")
        self._slider(l, "sign_hold_ms", "Hold to type", 250, 1500, "{} ms", step=50,
                     hint="How long to hold a sign before it types. Shorter is faster; longer makes fewer mistakes.")
        self._slider(l, "sign_confidence", "Certainty", 5, 60, "{}%",
                     hint="How clearly a sign must match one you taught rather than the next closest before it "
                          "types. Raise it if similar letters (M/N, A/S/E) get mixed up.")
        self._check(l, "sign_capitals", "Type capital letters")
        l.addStretch(1)
        return page

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
        self.teach_button.setToolTip("" if ready else "Switch to Sign language typing and press Start tracking first.")

    def _eye_tab(self) -> QWidget:
        page, l = self._page()
        self._hint(l, "Look at the screen to move the pointer instead of using your hand. Needs a short "
                      "calibration first, and is happiest when your head stays roughly still and facing "
                      "the camera, a head turn can throw it off more than a hand-tracking wobble would.")
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
        self._check(l, "snap_enabled", "Snap, hold the pointer still once it settles",
                    "Once the pointer has stayed in one spot for a moment it locks there, perfectly still, "
                    "and only lets go when you clearly move away. Makes small buttons much easier to hit.")
        self._slider(l, "snap_strength", "Snap strength", 1, 10, "{}% of the screen",
                     "How far you have to move to pull the pointer out of a snap. Higher is steadier; lower "
                     "lets go sooner.")
        l.addSpacing(6)
        self.calibration_status = QLabel()
        self.calibration_status.setObjectName("value")
        l.addWidget(self.calibration_status)
        self._choice(l, "eye_calibration_detail", "Calibration",
                     [("thorough", "Thorough, 25 points, a moving dot and a check (about 70 s, recommended)"),
                      ("quick", "Quick, 13 points (about 25 s)")])
        row = QHBoxLayout()
        self.calibrate_button = QPushButton("Calibrate…")
        self.calibrate_button.clicked.connect(self.main.open_calibration)
        row.addWidget(self.calibrate_button)
        self.recentre_button = QPushButton("Re-centre (1 s)")
        self.recentre_button.setToolTip("Pointer drifted since calibrating? Look at one dot for a second to fix it.")
        self.recentre_button.clicked.connect(self.main.open_recentre)
        row.addWidget(self.recentre_button)
        row.addStretch(1)
        l.addLayout(row)
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
                       "they run. Use it on a network you trust, plain http video isn't encrypted.")
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
                      "Mouse, it never closes or touches any other app.")
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
        self._check(l, "check_for_updates", "Tell me when a new version is out",
                    "Checks GitHub once when the app opens. Nothing is downloaded or installed by itself.")
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
    def _register(self, key: str, control: Any) -> None:
        """A setting can appear on more than one tab (snap is on both the head
        and the eye tab); every copy is kept, and all of them reload."""
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
        self._loading = was

    def _set(self, key: str, value: Any) -> None:
        if self._loading:
            return
        self.main.change_settings(**{key: value})
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
        self.recentre_button.setEnabled(ready and calibrated)
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
        self.overlay.set_diameter(halo_diameter(self.settings))
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
                                  "to click, calibrate first in Settings → Eye tracking.")
        elif self.settings.tracking_mode == "sign":
            self.subtitle.setText("Fingerspell to type into any app: hold each letter steady for a moment. "
                                  "Teach your signs first in Settings → Sign language.")
        elif self.settings.tracking_mode == "head":
            mouth = {"drag": " Open your mouth to drag.", "scroll": " Open your mouth and nod to scroll.",
                     "click": " Open your mouth to click.", "off": ""}[self.settings.head_mouth_action]
            click = ("Wink to click: left eye left-click, right eye right-click."
                     if self.settings.head_click == "wink" else "Hold a long blink to click.")
            self.subtitle.setText(f"Move your head to steer the pointer with your nose. {click}{mouth}")
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
        self.overlay.set_diameter(halo_diameter(new))
        self.diag.setVisible(new.show_diagnostics)
        if new.verbose_logging != old.verbose_logging:
            logging.getLogger().setLevel(logging.DEBUG if new.verbose_logging else logging.INFO)
        if any(getattr(new, k) != getattr(old, k) for k in ("tracking_mode", "head_click", "head_mouth_action")):
            self._update_subtitle()
        if new.tracking_mode != old.tracking_mode:
            if self.settings_dialog is not None:
                self.settings_dialog.update_calibration_status()
                self.settings_dialog.update_sign_status()
            self.settings_dialog.update_sign_status()
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
        if not self._eye_ready():
            return
        dialog = CalibrationDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self.settings_dialog is not None:
                self.settings_dialog.load(self.settings)
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
                                    "If it drifts later (you moved, the laptop moved), use Re-centre in "
                                    "Settings → Eye tracking: one second, no recalibrating.")

    def open_teach_signs(self) -> None:
        if self.engine is None or self.output is None or self.settings.tracking_mode != "sign":
            QMessageBox.information(self, "Start sign mode first",
                "Pick Sign language typing under Pointer → Tracking mode and press Start tracking, then "
                "teach your signs.")
            return
        TeachSignsDialog(self).exec()
        if self.settings_dialog is not None:
            self.settings_dialog.update_sign_status()

    def recentre_head(self) -> None:
        if self.engine is None or self.settings.tracking_mode != "head":
            QMessageBox.information(self, "Start the head pointer first",
                "Pick Head pointer under Pointer → Tracking mode and press Start tracking, then re-centre.")
            return
        self.engine.recentre_head()

    def open_recentre(self) -> None:
        if not self._eye_ready():
            return
        if not GazeCalibration.from_json(self.settings.eye_calibration).is_calibrated:
            QMessageBox.information(self, "Calibrate first", "Re-centring adjusts a calibration, calibrate first.")
            return
        if CalibrationDialog(self, recentre=True).exec() == QDialog.DialogCode.Accepted and self.settings_dialog:
            self.settings_dialog.load(self.settings)

    def _eye_ready(self) -> bool:
        if self.engine is None or self.output is None or self.settings.tracking_mode != "eye":
            QMessageBox.information(self, "Start eye tracking first",
                "Switch to Eye gaze mode in Settings and press Start tracking, then come back here to "
                "calibrate.")
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
            self.settings_dialog.update_sign_status()
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
            self.settings_dialog.update_sign_status()
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
