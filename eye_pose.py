"""Eye geometry for Finger Mouse: where in its socket each iris sits, and
whether an eye is closed.

Input is MediaPipe's 478 face landmarks (468 face mesh points plus 5 iris
points per eye, x and y as fractions of the frame's width and height) and
the matching face blendshapes. Like hand_pose.py, nothing here touches
MediaPipe or a camera, so it's tested with plain landmark data.

Landmark numbering (MediaPipe FaceMesh, with iris refinement):
    right eye   outer corner 33   inner corner 133   top 159   bottom 145
    left eye    outer corner 263  inner corner 362   top 386   bottom 374
    iris centres: right 468, left 473
("right"/"left" are the subject's own; Finger Mouse mirrors the picture
before this runs, so on screen they appear the way the subject expects.)

Gaze itself is read as *where the iris sits inside its own eye socket* —
how far from centre, as a fraction of the socket's width and height. That
number moves with the eyeball, not with the head: turning your head while
holding your gaze still barely changes it, which is what makes a short,
per-user calibration (gaze.py) enough to turn it into a screen position.
What it can't see is a head *rotation* that points the eyes at a different
part of the screen without the eyeball itself turning much — keeping your
head roughly facing the camera, the way the calibration was done, is what
keeps it accurate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

RIGHT_EYE = {"outer": 33, "inner": 133, "top": 159, "bottom": 145}
LEFT_EYE = {"outer": 263, "inner": 362, "top": 386, "bottom": 374}
RIGHT_IRIS = 468
LEFT_IRIS = 473

# ARKit-compatible blendshape names MediaPipe's face blendshapes use.
BLINK_LEFT = "eyeBlinkLeft"
BLINK_RIGHT = "eyeBlinkRight"

Point = tuple[float, float]


def _xy(lm: Any) -> Point:
    return (lm.x, lm.y)


def _one_eye(pts: Sequence[Point], corners: dict[str, int], iris: Point) -> Optional[tuple[Point, float]]:
    """(offset, socket_scale) for one eye, or None if the socket reads as too small to trust."""
    outer, inner, top, bottom = (pts[corners[k]] for k in ("outer", "inner", "top", "bottom"))
    width = abs(inner[0] - outer[0])
    height = abs(bottom[1] - top[1])
    if width < 1e-4 or height < 1e-4:
        return None
    left_x = min(outer[0], inner[0])
    top_y = min(top[1], bottom[1])
    # -1..1: 0 is centred in the socket, negative/positive is toward each edge.
    dx = ((iris[0] - left_x) / width - 0.5) * 2
    dy = ((iris[1] - top_y) / height - 0.5) * 2
    return (dx, dy), (width + height) / 2


@dataclass(frozen=True)
class EyeMeasure:
    """One frame's worth of what the eyes are doing."""
    offset: tuple[float, float]   # iris position within its socket, -1..1 per axis, both eyes averaged
    blink_left: float             # 0 (open) .. 1 (closed)
    blink_right: float
    blink: float                  # max of the two — either eye closing counts as a blink
    scale: float                  # inter-ocular distance, in frame-height units


def measure(landmarks: Sequence[Any], blendshapes: dict[str, float], aspect: float) -> Optional[EyeMeasure]:
    """Measure both eyes from MediaPipe face landmarks + blendshape scores.

    ``aspect`` = frame width / height (kept for symmetry with hand_pose.measure;
    the offset itself is already socket-relative and needs no frame scaling).
    None if the face is too small, at too sharp an angle, or an eye can't be read.
    """
    pts = [_xy(lm) for lm in landmarks]
    right = _one_eye(pts, RIGHT_EYE, pts[RIGHT_IRIS])
    left = _one_eye(pts, LEFT_EYE, pts[LEFT_IRIS])
    if right is None and left is None:
        return None
    both = [eye for eye in (right, left) if eye is not None]
    offsets = [o for o, _ in both]
    scales = [s for _, s in both]
    ox = sum(o[0] for o in offsets) / len(offsets)
    oy = sum(o[1] for o in offsets) / len(offsets)

    eye_l = (pts[RIGHT_EYE["outer"]][0] + pts[RIGHT_EYE["inner"]][0]) / 2, \
            (pts[RIGHT_EYE["outer"]][1] + pts[RIGHT_EYE["inner"]][1]) / 2
    eye_r = (pts[LEFT_EYE["outer"]][0] + pts[LEFT_EYE["inner"]][0]) / 2, \
            (pts[LEFT_EYE["outer"]][1] + pts[LEFT_EYE["inner"]][1]) / 2
    inter_ocular = ((eye_l[0] - eye_r[0]) * aspect) ** 2 + (eye_l[1] - eye_r[1]) ** 2
    scale = max(inter_ocular ** 0.5, sum(scales) / len(scales), 1e-4)

    bl = max(0.0, min(1.0, blendshapes.get(BLINK_LEFT, 0.0)))
    br = max(0.0, min(1.0, blendshapes.get(BLINK_RIGHT, 0.0)))
    return EyeMeasure(offset=(ox, oy), blink_left=bl, blink_right=br, blink=max(bl, br), scale=scale)
