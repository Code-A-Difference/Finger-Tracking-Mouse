"""Face + iris landmark detection for Finger Mouse's eye-tracking mode.

Same shape as hand_tracker.py, and deliberately reuses its macOS probe: the
crash that probe guards against (MediaPipe aborting instead of raising when
a GPU delegate can't be created, e.g. some macOS VMs) happens in MediaPipe's
native library itself, not in which model is loaded — if a HandTracker can
open there, a FaceLandmarker can too, so there's no need for a second
subprocess check.

The model is read into memory and handed over as bytes, for the same reason
as the hand model: MediaPipe's own file loading struggles with some Windows
paths, and installed apps live in exactly those paths.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from hand_tracker import resource_dir, start_check

log = logging.getLogger(__name__)

MODEL_NAME = "face_landmarker.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"


def model_path() -> Path:
    return resource_dir() / "models" / MODEL_NAME


def load_model_bytes(path: Optional[Path] = None) -> bytes:
    path = path or model_path()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"The eye-tracking model is missing ({path}). From the source folder, run "
            "`python tools/fetch_model.py` once, then start Finger Mouse again."
        ) from exc
    if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise RuntimeError(f"The eye-tracking model at {path} is damaged or unexpected. "
                           "Run `python tools/fetch_model.py` to replace it.")
    return data


def check_bundle() -> str:
    """Verify the model and MediaPipe's native library without opening the model."""
    model = load_model_bytes()
    try:
        from mediapipe.tasks.python.core import mediapipe_c_bindings   # MediaPipe 1.x

        mediapipe_c_bindings.load_raw_library()
    except ImportError:
        from mediapipe.tasks.python import vision  # noqa: F401  (0.10.x: bindings load on import)
    return f"model {len(model):,} bytes (SHA-256 ok), native library loads"


class EyeTracker:
    """Finds one face per frame. ``process`` returns its landmarks and
    blendshape scores, or None."""

    def __init__(self, min_confidence: float = 0.5, model: Optional[bytes] = None, probe: bool = True) -> None:
        self.min_confidence = min_confidence
        self._last_ts = 0
        self._impl: Any = None
        if probe:
            ok, why = start_check()
            if not ok:
                raise RuntimeError(why)
        try:
            self._open(model if model is not None else load_model_bytes())
        except Exception as exc:
            raise RuntimeError(f"Eye tracking couldn't start: {exc}") from exc

    def _open(self, model: bytes) -> None:
        from mediapipe.tasks.python import BaseOptions, vision

        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_buffer=model),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=self.min_confidence,
            min_face_presence_confidence=self.min_confidence,
            min_tracking_confidence=self.min_confidence,
            output_face_blendshapes=True,
        )
        self._impl = vision.FaceLandmarker.create_from_options(options)

    def process(self, rgb: np.ndarray) -> Optional[tuple[Sequence[Any], dict[str, float]]]:
        """(landmarks, {blendshape_name: score}) for the first face in an RGB frame, or None."""
        import mediapipe as mp

        # VIDEO mode needs strictly increasing timestamps.
        ts = max(int(time.monotonic() * 1000), self._last_ts + 1)
        self._last_ts = ts
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        result = self._impl.detect_for_video(image, ts)
        if not result.face_landmarks:
            return None
        shapes = {c.category_name: c.score for c in (result.face_blendshapes[0] if result.face_blendshapes else [])}
        return result.face_landmarks[0], shapes

    def close(self) -> None:
        try:
            if self._impl is not None:
                self._impl.close()
        except Exception:
            log.exception("Closing the eye tracker failed")
        self._impl = None


def draw_eye_points(image: np.ndarray, landmarks: Sequence[Any], color=(90, 220, 255)) -> None:
    """Mark the eye corners and iris centres on a BGR image, in place."""
    import cv2

    import eye_pose

    h, w = image.shape[:2]
    corners = [i for eye in (eye_pose.RIGHT_EYE, eye_pose.LEFT_EYE) for i in eye.values()]
    for i in corners:
        p = (int(landmarks[i].x * w), int(landmarks[i].y * h))
        cv2.circle(image, p, 2, (200, 200, 200), -1, cv2.LINE_AA)
    for i in (eye_pose.RIGHT_IRIS, eye_pose.LEFT_IRIS):
        p = (int(landmarks[i].x * w), int(landmarks[i].y * h))
        cv2.circle(image, p, 3, color, -1, cv2.LINE_AA)
