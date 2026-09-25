"""The real app, end to end, with no webcam and no real mouse.

The camera is an MJPEG stream served locally — the same kind of stream a
phone camera app publishes — showing a real photo of a pointing hand. Hand
tracking is the real MediaPipe model. Only the operating-system mouse is
swapped for a recorder, so nothing on the test machine moves. The window
runs offscreen.
"""

import http.server
import os
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
cv2 = pytest.importorskip("cv2")

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "models" / "hand_landmarker.task"
DATA = Path(__file__).parent / "data"

pytestmark = pytest.mark.skipif(not MODEL.exists(), reason="model not downloaded (tools/fetch_model.py)")


def jpeg_frames():
    import numpy as np

    hand = cv2.imread(str(DATA / "pointing_up.jpg"))
    canvas = np.full((480, 640, 3), 190, np.uint8)
    h, w = hand.shape[:2]
    y, x = (480 - h) // 2, (640 - w) // 2
    canvas[y:y + h, x:x + w] = hand
    return [cv2.imencode(".jpg", canvas)[1].tobytes()]


class Stream(http.server.BaseHTTPRequestHandler):
    frames: list = []
    stop = threading.Event()

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        i = 0
        try:
            while not self.stop.is_set():
                jpg = self.frames[i % len(self.frames)]
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                self.wfile.write(jpg + b"\r\n")
                i += 1
                time.sleep(1 / 20)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def stream_url():
    Stream.frames = jpeg_frames()
    Stream.stop.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/video"
    Stream.stop.set()
    server.shutdown()


def pump(app, seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.01)
    return bool(until and until())


def test_app_tracks_a_hand_from_a_network_stream(stream_url, tmp_path, monkeypatch):
    for var in ("APPDATA", "XDG_CONFIG_HOME", "HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path))       # keep the real settings untouched
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication, QTabWidget

    import Finger_tracker as ft
    import app_settings
    from test_pointer_output import Recorder

    recorder = Recorder()
    monkeypatch.setattr(ft, "create_backend", lambda: recorder)
    app = QApplication.instance() or QApplication([])

    window = ft.MainWindow(tmp_path / "logs")
    window.show()
    window.change_settings(camera="url", stream_url=stream_url, show_diagnostics=True)
    window.start_tracking()
    try:
        assert pump(app, 30, lambda: window.engine and window.engine.view.hand), \
            f"no hand seen: {window.status.text()}"
        assert pump(app, 5, lambda: any(c[0] == "move" for c in recorder.calls)), "pointer never moved"
        assert window.capabilities is not None and window.capabilities.width == 640
        assert pump(app, 3, lambda: not window.preview.pixmap().isNull()), "no preview shown"
        assert window.chip.text() != "Stopped"
        assert "inference" in window.diag.text()

        # The pointer follows the fingertip, mapped onto the (recorded) 1000×800 desktop.
        x, y = next(c for c in reversed(recorder.calls) if c[0] == "move")[1:3]
        assert 0 <= x < 1000 and 0 <= y < 800

        # Settings: every tab builds, a change applies live and is saved.
        window.open_settings()
        dialog = window.settings_dialog
        tabs = dialog.findChild(QTabWidget)
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            pump(app, 0.05)
        slider = dialog.controls["smoothing"][0]
        slider.setValue(80)
        assert window.engine.settings.smoothing == 80 or pump(app, 1, lambda: window.engine.settings.smoothing == 80)
        pump(app, 0.8)
        saved = app_settings.load(app_settings.settings_path())
        assert saved.smoothing == 80 and saved.camera == "url"
        assert "via network stream" in dialog.camera_info.text()
        dialog.close()
    finally:
        window.stop_tracking()
        assert pump(app, 10, lambda: window.engine is None), "tracking didn't stop"
    assert window.output is None
    assert not any(c[0] == "down" for c in recorder.calls)   # a pointing hand never clicks
    window.close()
