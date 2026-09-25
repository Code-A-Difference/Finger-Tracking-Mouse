"""Finger Mouse: control the desktop pointer with one hand and a webcam."""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

try:
    import cv2
    import mediapipe as mp
    import numpy as np
    import pyautogui
    from PySide6.QtCore import QPoint, QRect, Qt, QThread, Signal, QUrl
    from PySide6.QtGui import (
        QColor,
        QCursor,
        QDesktopServices,
        QImage,
        QPainter,
        QPen,
        QPixmap,
        QShortcut,
        QKeySequence,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSlider,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    raise SystemExit(
        "Finger Mouse is missing a dependency. Install the packages from "
        "requirements.txt, then run this file again.\n"
        f"Details: {exc}"
    ) from exc


APP_NAME = "Finger Mouse"
APP_VERSION = "2.1.0"
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.002


def settings_file() -> Path:
    """Return a per-user settings path without writing into the app bundle."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME / "settings.json"


def load_settings() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "camera": -1,
        "camera_view": "wide",
        "response": 18,
        "cursor_size": 36,
        "pinch_distance": 30,
    }
    try:
        with settings_file().open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            defaults.update(loaded)
    except (OSError, ValueError, TypeError):
        pass

    defaults["camera"] = _bounded_int(defaults.get("camera"), -1, 5, -1)
    if defaults.get("camera_view") not in ("wide", "standard"):
        defaults["camera_view"] = "wide"
    defaults["response"] = _bounded_int(defaults.get("response"), 5, 50, 18)
    defaults["cursor_size"] = _bounded_int(defaults.get("cursor_size"), 16, 80, 36)
    defaults["pinch_distance"] = _bounded_int(defaults.get("pinch_distance"), 15, 50, 30)
    return defaults


def _bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return fallback


def save_settings(settings: dict[str, Any]) -> None:
    path = settings_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        # Settings are helpful but must never prevent the mouse from starting.
        pass


class PinchClickDetector:
    """One click per intentional pinch, with stable-frame and release gates."""

    def __init__(self, sensitivity: int) -> None:
        self.enter_ratio = sensitivity / 100.0
        self.release_ratio = min(0.8, max(self.enter_ratio + 0.14, self.enter_ratio * 1.5))
        self.armed = False
        self._pinch_frames = 0
        self._release_frames = 0
        self._missing_frames = 0

    def update(self, pinch_ratio: Optional[float]) -> Optional[str]:
        if pinch_ratio is None:
            self._missing_frames += 1
            self._pinch_frames = 0
            self._release_frames = 0
            if self._missing_frames >= 15 and self.armed:
                self.armed = False
                return "disarmed"
            return None

        self._missing_frames = 0
        if self.armed:
            if pinch_ratio <= self.enter_ratio:
                self._pinch_frames += 1
                if self._pinch_frames >= 2:
                    self.armed = False
                    self._pinch_frames = 0
                    self._release_frames = 0
                    return "clicked"
            else:
                self._pinch_frames = 0
            return None

        if pinch_ratio >= self.release_ratio:
            self._release_frames += 1
            if self._release_frames >= 2:
                self.armed = True
                self._release_frames = 0
                return "armed"
        else:
            self._release_frames = 0
        return None


class TrackingWorker(QThread):
    """Owns camera and hand-tracking work so the window stays responsive."""

    status_changed = Signal(str)
    frame_ready = Signal(QImage)
    cursor_changed = Signal(int, int)
    click_requested = Signal()
    gesture_changed = Signal(str)

    def __init__(
        self,
        camera_index: int,
        camera_view: str,
        response: int,
        pinch_distance: int,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.camera_index = camera_index
        self.camera_view = camera_view
        self.response = response
        self.pinch_distance = pinch_distance
        self._stop_requested = threading.Event()

    def request_stop(self) -> None:
        self._stop_requested.set()

    @staticmethod
    def _camera_backend() -> int:
        if sys.platform == "darwin":
            return cv2.CAP_AVFOUNDATION
        if sys.platform == "win32":
            return cv2.CAP_DSHOW
        return cv2.CAP_V4L2

    def _open_camera(self) -> tuple[Any, np.ndarray, int]:
        indices = range(6) if self.camera_index < 0 else (self.camera_index,)
        backend = self._camera_backend()
        backends = (backend, cv2.CAP_ANY) if backend != cv2.CAP_ANY else (cv2.CAP_ANY,)
        for index in indices:
            if self._stop_requested.is_set():
                break
            self.status_changed.emit(f"Looking for a camera… (device {index})")
            for api in backends:
                try:
                    camera = cv2.VideoCapture(index, api)
                except cv2.error:
                    continue
                if not camera.isOpened():
                    camera.release()
                    continue

                requested_width, requested_height = (
                    (1280, 720) if self.camera_view == "wide" else (640, 480)
                )
                camera.set(cv2.CAP_PROP_FRAME_WIDTH, requested_width)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, requested_height)
                camera.set(cv2.CAP_PROP_FPS, 30)
                camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                okay, frame = camera.read()
                if okay and frame is not None and frame.size:
                    return camera, frame, index
                camera.release()

        raise RuntimeError(
            "No working camera was found. Check camera permissions, close other "
            "apps using it, or choose a different camera."
        )

    def _to_screen(self, x: float, y: float) -> tuple[int, int]:
        screen_width, screen_height = pyautogui.size()
        # Use a small edge margin so fingers at the image border still reach
        # near the screen edge without producing out-of-range coordinates or
        # accidentally activating PyAutoGUI's corner failsafe.
        margin_x = 0.10
        margin_y = 0.12
        sx = np.interp(x, (margin_x, 1.0 - margin_x), (1, screen_width - 2))
        sy = np.interp(y, (margin_y, 1.0 - margin_y), (1, screen_height - 2))
        return int(np.clip(sx, 1, screen_width - 2)), int(np.clip(sy, 1, screen_height - 2))

    @staticmethod
    def _pinch_ratio(
        thumb_tip: Any, index_tip: Any, hand_scale: float, aspect_ratio: float
    ) -> float:
        """Measure thumb-index gap relative to hand size, not camera distance."""
        gap = math.hypot(
            (thumb_tip.x - index_tip.x) * aspect_ratio,
            thumb_tip.y - index_tip.y,
        )
        return gap / max(hand_scale, 1e-4)

    def run(self) -> None:
        camera = None
        hand_tracker = None
        click_detector = PinchClickDetector(self.pinch_distance)
        smoothed: Optional[tuple[float, float]] = None
        failed_frames = 0
        try:
            camera, first_frame, camera_index = self._open_camera()
            if self._stop_requested.is_set():
                return

            hand_tracker = mp.solutions.hands.Hands(
                static_image_mode=False,
                max_num_hands=1,
                model_complexity=1,
                min_detection_confidence=0.60,
                min_tracking_confidence=0.60,
            )
            frame_height, frame_width = first_frame.shape[:2]
            self.status_changed.emit(
                f"Camera {camera_index} connected at {frame_width}×{frame_height}. "
                "Open thumb and index to arm a click."
            )
            frame_to_process: Optional[np.ndarray] = first_frame
            alpha = self.response / 100.0
            last_preview_time = 0.0

            while not self._stop_requested.is_set():
                if frame_to_process is None:
                    okay, frame = camera.read()
                    if not okay or frame is None or not frame.size:
                        failed_frames += 1
                        if failed_frames >= 75:
                            raise RuntimeError("The camera stopped sending video. Reconnect it and try again.")
                        time.sleep(0.02)
                        continue
                    failed_frames = 0
                else:
                    frame = frame_to_process
                    frame_to_process = None

                frame = cv2.flip(frame, 1)
                height, width = frame.shape[:2]
                processing_frame = frame
                if width > 640 or height > 480:
                    scale = min(640 / width, 480 / height)
                    processing_frame = cv2.resize(
                        frame,
                        (max(1, int(width * scale)), max(1, int(height * scale))),
                        interpolation=cv2.INTER_AREA,
                    )
                rgb = cv2.cvtColor(processing_frame, cv2.COLOR_BGR2RGB)
                result = hand_tracker.process(rgb)
                click_registered = False

                if result.multi_hand_landmarks:
                    hand = result.multi_hand_landmarks[0]
                    landmarks = hand.landmark
                    mp.solutions.drawing_utils.draw_landmarks(
                        frame, hand, mp.solutions.hands.HAND_CONNECTIONS
                    )

                    index_tip = landmarks[8]
                    thumb_tip = landmarks[4]
                    aspect_ratio = width / max(1, height)
                    palm_width = math.hypot(
                        (landmarks[5].x - landmarks[17].x) * aspect_ratio,
                        landmarks[5].y - landmarks[17].y,
                    )
                    palm_length = math.hypot(
                        (landmarks[0].x - landmarks[9].x) * aspect_ratio,
                        landmarks[0].y - landmarks[9].y,
                    )
                    hand_scale = max(palm_width, palm_length * 0.55)
                    pinch_ratio = self._pinch_ratio(
                        thumb_tip, index_tip, hand_scale, aspect_ratio
                    )
                    click_state = click_detector.update(pinch_ratio)
                    if click_state == "clicked":
                        self.click_requested.emit()
                        click_registered = True
                    if click_state is not None:
                        self.gesture_changed.emit(click_state)

                    target_x, target_y = self._to_screen(index_tip.x, index_tip.y)
                    if smoothed is None:
                        smoothed = (float(target_x), float(target_y))
                    else:
                        smoothed = (
                            alpha * target_x + (1.0 - alpha) * smoothed[0],
                            alpha * target_y + (1.0 - alpha) * smoothed[1],
                        )
                    screen_size = pyautogui.size()
                    cursor_x = int(np.clip(smoothed[0], 2, screen_size.width - 3))
                    cursor_y = int(np.clip(smoothed[1], 2, screen_size.height - 3))
                    self.cursor_changed.emit(cursor_x, cursor_y)

                    tip_a = (int(thumb_tip.x * width), int(thumb_tip.y * height))
                    tip_b = (int(index_tip.x * width), int(index_tip.y * height))
                    color = (50, 220, 140) if click_registered else (255, 185, 55)
                    cv2.line(frame, tip_a, tip_b, color, 2, cv2.LINE_AA)
                    cv2.circle(frame, tip_a, 10, color, 2, cv2.LINE_AA)
                    cv2.circle(frame, tip_b, 10, color, 2, cv2.LINE_AA)
                    gesture_text = (
                        "CLICK REGISTERED" if click_registered
                        else "PINCH THUMB + INDEX" if click_detector.armed
                        else "OPEN TO ARM CLICK"
                    )
                    cv2.putText(
                        frame,
                        gesture_text,
                        (18, 32),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.58,
                        color,
                        2,
                        cv2.LINE_AA,
                    )
                else:
                    smoothed = None
                    click_state = click_detector.update(None)
                    if click_state is not None:
                        self.gesture_changed.emit(click_state)
                    cv2.putText(
                        frame,
                        "Show one hand to the camera",
                        (18, 32),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.62,
                        (245, 245, 245),
                        2,
                        cv2.LINE_AA,
                    )

                now = time.monotonic()
                if now - last_preview_time >= 1 / 20:
                    preview = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    image = QImage(
                        preview.data,
                        width,
                        height,
                        int(preview.strides[0]),
                        QImage.Format.Format_RGB888,
                    ).copy()
                    self.frame_ready.emit(image)
                    last_preview_time = now

        except pyautogui.FailSafeException:
            self.status_changed.emit("Safety stop: move the pointer away from the screen corner, then start again.")
        except Exception as exc:
            self.status_changed.emit(f"Tracking error: {exc}")
        finally:
            if camera is not None:
                camera.release()
            if hand_tracker is not None:
                hand_tracker.close()
            if self._stop_requested.is_set():
                self.status_changed.emit("Tracking stopped.")


class CursorOverlay(QWidget):
    """A transparent, click-through cursor halo shown over the desktop."""

    def __init__(self) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.Tool
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setWindowTitle("Finger Mouse cursor indicator")
        self._cursor_position = QPoint(0, 0)
        self._diameter = 36
        screens = QApplication.screens()
        if screens:
            geometry = QRect(screens[0].geometry())
            for screen in screens[1:]:
                geometry = geometry.united(screen.geometry())
            self.setGeometry(geometry)

    def set_cursor_position(self, x: int, y: int) -> None:
        self._cursor_position = QPoint(x, y) - self.geometry().topLeft()
        if not self.isVisible():
            self.show()
        self.update()

    def set_diameter(self, diameter: int) -> None:
        self._diameter = diameter
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt event name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = self._diameter / 2
        x, y = self._cursor_position.x(), self._cursor_position.y()
        painter.setPen(QPen(QColor(34, 211, 238, 235), 2))
        painter.setBrush(QColor(34, 211, 238, 48))
        painter.drawEllipse(QPoint(x, y), int(radius), int(radius))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(34, 211, 238, 220))
        painter.drawEllipse(QPoint(x, y), 3, 3)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        self.worker: Optional[TrackingWorker] = None
        self._system_control_error: Optional[str] = None
        self.overlay = CursorOverlay()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setMinimumSize(760, 790)
        self.resize(860, 870)
        self._build_ui()
        self._apply_style()
        self.cursor_size.setValue(self.settings["cursor_size"])
        self.response.setValue(self.settings["response"])
        self.pinch_distance.setValue(self.settings["pinch_distance"])
        self._select_camera(self.settings["camera"])
        self._select_camera_view(self.settings["camera_view"])
        self.overlay.set_diameter(self.settings["cursor_size"])
        self.stop_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.stop_shortcut.activated.connect(self.stop_tracking)

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = QLabel("Finger Mouse")
        title.setObjectName("title")
        subtitle = QLabel("Move with your index finger. Pinch thumb and index to click once.")
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.preview = QLabel("Camera preview appears here\nafter you start tracking")
        self.preview.setObjectName("preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(330)
        self.preview.setScaledContents(False)
        layout.addWidget(self.preview, 1)

        camera_row = QHBoxLayout()
        camera_label = QLabel("Camera")
        self.camera = QComboBox()
        self.camera.addItem("Automatic", -1)
        for index in range(6):
            self.camera.addItem(f"Camera {index}", index)
        self.camera.currentIndexChanged.connect(self._camera_changed)
        camera_row.addWidget(camera_label)
        camera_row.addWidget(self.camera)
        camera_row.addSpacing(12)
        camera_row.addWidget(QLabel("Camera view"))
        self.camera_view = QComboBox()
        self.camera_view.addItem("Wide 16:9", "wide")
        self.camera_view.addItem("Standard 4:3", "standard")
        self.camera_view.currentIndexChanged.connect(self._camera_view_changed)
        camera_row.addStretch(1)
        camera_row.addWidget(self.camera_view)
        layout.addLayout(camera_row)

        settings_card = QFrame()
        settings_card.setObjectName("settingsCard")
        settings_layout = QVBoxLayout(settings_card)
        settings_layout.setContentsMargins(18, 14, 18, 14)
        settings_layout.setSpacing(8)

        cursor_row = QHBoxLayout()
        cursor_label = QLabel("Tracking halo size")
        self.cursor_value = QLabel()
        self.cursor_value.setObjectName("value")
        cursor_row.addWidget(cursor_label)
        cursor_row.addStretch(1)
        self.system_cursor_button = QPushButton("System cursor size…")
        self.system_cursor_button.clicked.connect(self._open_system_cursor_settings)
        cursor_row.addWidget(self.system_cursor_button)
        cursor_row.addWidget(self.cursor_value)
        self.cursor_size = QSlider(Qt.Orientation.Horizontal)
        self.cursor_size.setRange(16, 80)
        self.cursor_size.valueChanged.connect(self._cursor_size_changed)
        settings_layout.addLayout(cursor_row)
        settings_layout.addWidget(self.cursor_size)

        response_row = QHBoxLayout()
        response_label = QLabel("Movement response")
        self.response_value = QLabel()
        self.response_value.setObjectName("value")
        response_row.addWidget(response_label)
        response_row.addStretch(1)
        response_row.addWidget(self.response_value)
        self.response = QSlider(Qt.Orientation.Horizontal)
        self.response.setRange(5, 50)
        self.response.valueChanged.connect(self._response_changed)
        settings_layout.addLayout(response_row)
        settings_layout.addWidget(self.response)

        pinch_row = QHBoxLayout()
        pinch_label = QLabel("Pinch distance")
        self.pinch_value = QLabel()
        self.pinch_value.setObjectName("value")
        pinch_row.addWidget(pinch_label)
        pinch_row.addStretch(1)
        pinch_row.addWidget(self.pinch_value)
        self.pinch_distance = QSlider(Qt.Orientation.Horizontal)
        self.pinch_distance.setRange(15, 50)
        self.pinch_distance.valueChanged.connect(self._pinch_distance_changed)
        settings_layout.addLayout(pinch_row)
        settings_layout.addWidget(self.pinch_distance)
        layout.addWidget(settings_card)

        action_row = QHBoxLayout()
        self.status = QLabel("Ready. Start tracking to move the real system pointer and click.")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.start_button = QPushButton("Start tracking")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self.toggle_tracking)
        action_row.addWidget(self.status, 1)
        action_row.addWidget(self.start_button)
        layout.addLayout(action_row)

        footnote = QLabel(
            f"v{APP_VERSION} · Pinch once to click; open thumb and index to re-arm. "
            "Press Esc to stop."
        )
        footnote.setObjectName("footnote")
        footnote.setWordWrap(True)
        layout.addWidget(footnote)
        self.setCentralWidget(central)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget { background: #0c1220; color: #e8edf6; font-family: Arial; font-size: 14px; }
            QLabel#title { font-size: 30px; font-weight: 700; color: #f4f7fb; }
            QLabel#subtitle { color: #aebbd0; font-size: 14px; }
            QLabel#preview { background: #080d17; border: 1px solid #263248; border-radius: 12px; color: #8290a6; }
            QFrame#settingsCard { background: #151e2e; border: 1px solid #27344a; border-radius: 12px; }
            QFrame#settingsCard QLabel { background: transparent; }
            QLabel#value { color: #67e8f9; font-weight: 700; }
            QLabel#status { color: #c2ccdc; }
            QLabel#footnote { color: #8290a6; font-size: 12px; }
            QComboBox { background: #182338; border: 1px solid #35445c; border-radius: 7px; padding: 8px 10px; min-width: 125px; }
            QComboBox QAbstractItemView { background: #182338; selection-background-color: #155e75; }
            QSlider::groove:horizontal { height: 5px; background: #344259; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #22d3ee; border-radius: 2px; }
            QSlider::handle:horizontal { background: #ecfeff; border: 2px solid #22d3ee; width: 14px; margin: -6px 0; border-radius: 8px; }
            QPushButton { background: #26344b; color: #e8edf6; border: 0; border-radius: 8px; padding: 11px 16px; font-weight: 700; }
            QPushButton#startButton { background: #0891b2; color: white; min-width: 145px; }
            QPushButton#startButton:hover { background: #06a3c7; }
            QPushButton:disabled { background: #26344b; color: #8290a6; }
            """
        )

    def _select_camera(self, camera_index: int) -> None:
        for index in range(self.camera.count()):
            if self.camera.itemData(index) == camera_index:
                self.camera.setCurrentIndex(index)
                return
        self.camera.setCurrentIndex(0)

    def _select_camera_view(self, camera_view: str) -> None:
        for index in range(self.camera_view.count()):
            if self.camera_view.itemData(index) == camera_view:
                self.camera_view.setCurrentIndex(index)
                return
        self.camera_view.setCurrentIndex(0)

    def _persist_settings(self) -> None:
        save_settings(
            {
                "camera": int(self.camera.currentData()),
                "camera_view": str(self.camera_view.currentData()),
                "response": self.response.value(),
                "cursor_size": self.cursor_size.value(),
                "pinch_distance": self.pinch_distance.value(),
            }
        )

    def _camera_changed(self, _index: int) -> None:
        self._persist_settings()

    def _camera_view_changed(self, _index: int) -> None:
        self._persist_settings()

    def _cursor_size_changed(self, value: int) -> None:
        self.cursor_value.setText(f"{value} px")
        self.overlay.set_diameter(value)
        self._persist_settings()

    def _response_changed(self, value: int) -> None:
        self.response_value.setText(f"{value}%")
        self._persist_settings()
        if self.worker is not None and self.worker.isRunning():
            self.status.setText("Response changes apply the next time tracking starts.")

    def _pinch_distance_changed(self, value: int) -> None:
        self.pinch_value.setText(f"{value}% of hand size")
        self._persist_settings()
        if self.worker is not None and self.worker.isRunning():
            self.status.setText("Pinch distance changes apply the next time tracking starts.")

    def toggle_tracking(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.stop_tracking()
        else:
            self.start_tracking()

    def start_tracking(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if not self._request_mouse_control_access():
            return
        self._system_control_error = None
        self._persist_settings()
        self.preview.setText("Starting camera…")
        self.start_button.setText("Stop tracking")
        self.status.setText("Starting hand tracking…")
        self.camera.setEnabled(False)
        self.camera_view.setEnabled(False)
        self.overlay.hide()
        self.worker = TrackingWorker(
            int(self.camera.currentData()),
            str(self.camera_view.currentData()),
            self.response.value(),
            self.pinch_distance.value(),
            self,
        )
        self.worker.status_changed.connect(self.status.setText)
        self.worker.frame_ready.connect(self._show_frame)
        self.worker.cursor_changed.connect(self._move_system_pointer)
        self.worker.cursor_changed.connect(self.overlay.set_cursor_position)
        self.worker.click_requested.connect(self._click_system_pointer)
        self.worker.gesture_changed.connect(self._gesture_status)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _request_mouse_control_access(self) -> bool:
        """Ask macOS for the permission needed to post system click events."""
        if sys.platform != "darwin":
            return True
        try:
            import Quartz

            preflight = getattr(Quartz, "CGPreflightPostEventAccess", None)
            request = getattr(Quartz, "CGRequestPostEventAccess", None)
            if preflight is None or request is None:
                raise RuntimeError("This macOS build cannot check mouse-control permission.")
            if not preflight():
                request()
            if preflight():
                return True
        except Exception as exc:
            self.status.setText(f"Could not check macOS mouse permission: {exc}")
            return False

        self.status.setText("Allow Finger Mouse to control the computer, then start tracking again.")
        QDesktopServices.openUrl(
            QUrl("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
        )
        QMessageBox.warning(
            self,
            "Allow system mouse control",
            "macOS has not allowed Finger Mouse to send system clicks. In System Settings, "
            "open Privacy & Security → Accessibility and enable Finger Mouse. If it is not "
            "listed, add the Finger Mouse app, quit it, reopen it, and try again.",
        )
        return False

    def stop_tracking(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.status.setText("Stopping tracking…")
            self.worker.request_stop()

    def _worker_finished(self) -> None:
        self.overlay.hide()
        self.start_button.setText("Start tracking")
        self.camera.setEnabled(True)
        self.camera_view.setEnabled(True)
        if self._system_control_error:
            self.status.setText(self._system_control_error)
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = None

    def _gesture_status(self, state: str) -> None:
        if self._system_control_error:
            return
        if state == "armed":
            self.status.setText("Click armed. Pinch thumb and index for one left-click.")
        elif state == "clicked":
            self.status.setText("Pinch click registered. Open fingers to re-arm.")
        elif state == "disarmed":
            self.status.setText("Hand lost. Show an open hand to arm clicking again.")

    def _move_system_pointer(self, x: int, y: int) -> None:
        """Move the OS cursor on Qt's GUI thread, then verify the real position."""
        if self.worker is None or not self.worker.isRunning():
            return
        try:
            QCursor.setPos(x, y)
            actual = pyautogui.position()
            if abs(actual.x - x) > 3 or abs(actual.y - y) > 3:
                # Some desktop backends ignore Qt cursor warps. Try the native
                # PyAutoGUI backend once from the GUI thread, then fail visibly.
                pyautogui.moveTo(x, y, duration=0)
                actual = pyautogui.position()
            if abs(actual.x - x) > 3 or abs(actual.y - y) > 3:
                self._report_system_control_error(
                    "The desktop did not move its system pointer. Check mouse-control "
                    "permissions or switch Linux to an X11 session."
                )
        except pyautogui.FailSafeException:
            self._report_system_control_error(
                "Safety stop: move the pointer away from the top-left corner, then start again."
            )
        except Exception as exc:
            self._report_system_control_error(f"System pointer error: {exc}")

    def _click_system_pointer(self) -> None:
        """Send a real OS click at the system pointer's current location."""
        try:
            pyautogui.click()
        except pyautogui.FailSafeException:
            self._report_system_control_error(
                "Safety stop: move the pointer away from the top-left corner, then start again."
            )
        except Exception as exc:
            self._report_system_control_error(f"System click error: {exc}")

    def _report_system_control_error(self, message: str) -> None:
        self._system_control_error = message
        self.status.setText(message)
        if self.worker is not None:
            self.worker.request_stop()

    def _open_system_cursor_settings(self) -> None:
        """Open native accessibility settings for the actual OS cursor."""
        if sys.platform == "win32":
            QDesktopServices.openUrl(QUrl("ms-settings:easeofaccess-mousepointer"))
        elif sys.platform == "darwin":
            import subprocess

            try:
                subprocess.Popen(["open", "-b", "com.apple.systempreferences"])
                QMessageBox.information(
                    self,
                    "System cursor size",
                    "In System Settings, open Accessibility → Display → Pointer size. "
                    "That setting changes the real system cursor. The Tracking halo size "
                    "slider only changes Finger Mouse’s visual tracking indicator.",
                )
            except OSError as exc:
                QMessageBox.warning(self, "System Settings", str(exc))
        else:
            QMessageBox.information(
                self,
                "System cursor size",
                "Open your desktop environment’s Accessibility settings and adjust the "
                "mouse pointer size. The Tracking halo size slider changes only Finger "
                "Mouse’s visual tracking indicator.",
            )

    def _show_frame(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image).scaled(
            self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setPixmap(pixmap)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt event name
        super().resizeEvent(event)
        if self.worker is not None and self.worker.isRunning():
            # Re-scale the latest preview when the window size changes.
            pixmap = self.preview.pixmap()
            if pixmap is not None:
                self.preview.setPixmap(
                    pixmap.scaled(
                        self.preview.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt event name
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            if not self.worker.wait(3000):
                self.status.setText("The camera is still shutting down. Try closing again in a moment.")
                event.ignore()
                return
        self.overlay.close()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("Finger Mouse")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
