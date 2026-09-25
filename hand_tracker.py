"""Hand landmark detection, wrapped so the rest of the app doesn't care which
MediaPipe it's running on.

MediaPipe 1.0 removed the old ``mp.solutions.hands`` API that Finger Mouse
2.1 used; the supported way is the Tasks API with a ``hand_landmarker.task``
model file. This uses Tasks everywhere (it exists in 0.10 too) and falls
back to the old API only if Tasks can't load.

The model is read into memory and handed over as bytes: MediaPipe's own file
loading has trouble with some Windows paths (non-ASCII user names, for one),
and installed apps live in exactly those paths.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "hand_landmarker.task"
# Pinned: tools/fetch_model.py downloads exactly this file, and a changed
# file is refused rather than silently used.
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/1/hand_landmarker.task")
MODEL_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"

# The 21-point hand skeleton, for drawing.
HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11),
    (11, 12), (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
)


def _allow_missing_audio() -> None:
    """MediaPipe imports ``sounddevice`` (for its audio tasks) as soon as it
    loads, and on Linux that raises if the PortAudio system library isn't
    installed. Finger Mouse never uses audio, so if it can't load, a stand-in
    module lets MediaPipe import anyway."""
    if "sounddevice" in sys.modules:
        return
    try:
        import sounddevice  # noqa: F401
    except (OSError, ImportError):
        import types
        sys.modules["sounddevice"] = types.ModuleType("sounddevice")
        log.info("PortAudio not found; MediaPipe's audio support disabled (not needed here)")


_allow_missing_audio()


# ---------------------------------------------------------------------------
# Starting safely
# ---------------------------------------------------------------------------
# MediaPipe's macOS build sets up GPU resources while opening the hand model,
# even for CPU inference, and if that fails it doesn't raise — it aborts the
# whole process. Real Macs are fine; some virtual machines (GitHub's macOS
# runners, for one) are not. So on macOS the model is first opened once in a
# short-lived child process: if that child dies, the app shows why and keeps
# running instead of vanishing.

PROBE_ARG = "--probe-hand-model"
_probe: Optional[tuple[bool, str]] = None


def needs_probe() -> bool:
    return sys.platform == "darwin"


def _probe_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, PROBE_ARG]            # the app itself (see Finger_tracker.main)
    return [sys.executable, str(Path(__file__).resolve()), PROBE_ARG]


def probe_main() -> int:
    """Run in the child: open the model, run one frame, report."""
    tracker = HandTracker(probe=False)
    tracker.process(np.zeros((240, 320, 3), np.uint8))
    tracker.close()
    print("hand model ok")
    return 0


def start_check(timeout: float = 90.0) -> tuple[bool, str]:
    """(True, "") if MediaPipe can open the hand model here, else (False, why).

    Cached for the life of the process. Only macOS actually probes.
    """
    global _probe
    if _probe is None:
        if not needs_probe():
            _probe = (True, "")
        else:
            try:
                done = subprocess.run(_probe_command(), capture_output=True, text=True, timeout=timeout,
                                      errors="replace")
                if done.returncode == 0:
                    _probe = (True, "")
                else:
                    detail = [l for l in (done.stderr or "").splitlines() if "Check failed" in l or "Error" in l]
                    why = detail[-1].strip() if detail else f"exit code {done.returncode}"
                    vm = " This Mac appears to be a virtual machine." if _is_virtual_machine() else ""
                    _probe = (False, f"MediaPipe couldn't open the hand model on this Mac ({why}).{vm}")
                    log.error("Hand model probe failed: %s\n%s", why, (done.stderr or "")[-4000:])
            except (OSError, subprocess.SubprocessError) as exc:
                _probe = (False, f"Couldn't check the hand model: {exc}")
    return _probe


def _is_virtual_machine() -> bool:
    try:
        out = subprocess.run(["sysctl", "-n", "kern.hv_vmm_present"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def check_bundle() -> str:
    """Verify the model and MediaPipe's native library without opening the model."""
    model = load_model_bytes()
    try:
        from mediapipe.tasks.python.core import mediapipe_c_bindings   # MediaPipe 1.x

        mediapipe_c_bindings.load_raw_library()
    except ImportError:
        from mediapipe.tasks.python import vision  # noqa: F401  (0.10.x: bindings load on import)
    return f"model {len(model):,} bytes (SHA-256 ok), native library loads"


def resource_dir() -> Path:
    """Where bundled files live: next to the source, or inside the PyInstaller bundle."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def model_path() -> Path:
    return resource_dir() / "models" / MODEL_NAME


def load_model_bytes(path: Optional[Path] = None) -> bytes:
    path = path or model_path()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"The hand-tracking model is missing ({path}). From the source folder, run "
            "`python tools/fetch_model.py` once, then start Finger Mouse again."
        ) from exc
    if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise RuntimeError(f"The hand-tracking model at {path} is damaged or unexpected. "
                           "Run `python tools/fetch_model.py` to replace it.")
    return data


class HandTracker:
    """Finds one hand per frame. ``process`` returns its 21 landmarks or None."""

    def __init__(self, min_confidence: float = 0.6, model: Optional[bytes] = None, probe: bool = True) -> None:
        self.min_confidence = min_confidence
        self.kind = ""
        self._last_ts = 0
        self._impl: Any = None
        self._legacy: Any = None
        if probe:
            ok, why = start_check()
            if not ok:
                raise RuntimeError(why)
        try:
            self._open_tasks(model if model is not None else load_model_bytes())
        except Exception as exc:
            if not self._open_legacy():
                raise RuntimeError(f"Hand tracking couldn't start: {exc}") from exc
            log.warning("Using legacy MediaPipe hands API (%s)", exc)

    def _open_tasks(self, model: bytes) -> None:
        from mediapipe.tasks.python import BaseOptions, vision

        options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_buffer=model),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=self.min_confidence,
            min_hand_presence_confidence=self.min_confidence,
            min_tracking_confidence=self.min_confidence,
        )
        self._impl = vision.HandLandmarker.create_from_options(options)
        self.kind = "tasks"

    def _open_legacy(self) -> bool:
        try:
            import mediapipe as mp

            hands = mp.solutions.hands  # absent in MediaPipe 1.0+
        except (ImportError, AttributeError):
            return False
        self._legacy = hands.Hands(static_image_mode=False, max_num_hands=1, model_complexity=1,
                                   min_detection_confidence=self.min_confidence,
                                   min_tracking_confidence=self.min_confidence)
        self.kind = "legacy"
        return True

    def process(self, rgb: np.ndarray) -> Optional[Sequence[Any]]:
        """Landmarks for the first hand in an RGB frame, or None."""
        if self._legacy is not None:
            result = self._legacy.process(rgb)
            hands = result.multi_hand_landmarks
            return hands[0].landmark if hands else None

        import mediapipe as mp

        # VIDEO mode needs strictly increasing timestamps.
        ts = max(int(time.monotonic() * 1000), self._last_ts + 1)
        self._last_ts = ts
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        result = self._impl.detect_for_video(image, ts)
        return result.hand_landmarks[0] if result.hand_landmarks else None

    def close(self) -> None:
        for impl in (self._impl, self._legacy):
            try:
                if impl is not None:
                    impl.close()
            except Exception:
                log.exception("Closing the hand tracker failed")
        self._impl = self._legacy = None


def draw_landmarks(image: np.ndarray, landmarks: Sequence[Any], color=(90, 220, 255)) -> None:
    """Draw the hand skeleton onto a BGR image in place."""
    import cv2

    h, w = image.shape[:2]
    pts = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]
    for a, b in HAND_CONNECTIONS:
        cv2.line(image, pts[a], pts[b], (200, 200, 200), 1, cv2.LINE_AA)
    for p in pts:
        cv2.circle(image, p, 3, color, -1, cv2.LINE_AA)


if __name__ == "__main__" and PROBE_ARG in sys.argv:
    raise SystemExit(probe_main())
