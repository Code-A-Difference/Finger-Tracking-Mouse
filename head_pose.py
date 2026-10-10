"""Head-pointer geometry for Finger Mouse: where the nose is, and what the
eyes, mouth and cheeks are doing.

Input is the same as eye_pose.py, MediaPipe's face landmarks (x, y as
fractions of the frame) and its face blendshapes, so the head pointer runs
on the same face model as eye tracking. Nothing here touches MediaPipe or a
camera, so it is tested with plain landmark data.

Why the nose: it's the most rigid point on a face. Eyes move on their own,
lips move when you talk or smile, but the nose tip only moves when the head
does. Its position is measured in *face widths* (the distance between the
eye centres) rather than pixels, so the same head movement moves the pointer
the same amount whether you sit close to the camera or far from it.

The picture is mirrored before this runs (Finger Mouse shows you a mirror),
so "left" here means the left of the screen, which is your own left.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

NOSE_TIP = 1
RIGHT_EYE_CORNERS = (33, 133)
LEFT_EYE_CORNERS = (263, 362)
UPPER_LIP, LOWER_LIP = 13, 14
MOUTH_CORNERS = (61, 291)

# ARKit-style blendshape names MediaPipe's face model reports.
BLINK_LEFT, BLINK_RIGHT = "eyeBlinkLeft", "eyeBlinkRight"
JAW_OPEN = "jawOpen"
SMILE_LEFT, SMILE_RIGHT = "mouthSmileLeft", "mouthSmileRight"


@dataclass(frozen=True)
class HeadMeasure:
    """One frame of the face, for the head pointer."""
    nose: tuple[float, float]   # nose tip, in face widths from the frame's top-left corner
    scale: float                # face width (eye centre to eye centre), in frame-height units
    wink_left: float            # 0 (open) .. 1 (closed), the eye on YOUR left
    wink_right: float           # the eye on your right
    mouth_open: float           # 0 .. 1
    smile: float                # 0 .. 1


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def measure(landmarks: Sequence[Any], blendshapes: dict[str, float], aspect: float,
            swap_eyes: bool = False) -> Optional[HeadMeasure]:
    """Measure the head from MediaPipe face landmarks + blendshape scores.

    ``aspect`` = frame width / height, so a sideways nod and an up-down nod
    count the same. ``swap_eyes`` flips which eye is which, for cameras or
    drivers that hand over an un-mirrored picture. None if the face can't be
    read (too small, too far turned).
    """
    if len(landmarks) <= max(NOSE_TIP, *LEFT_EYE_CORNERS, LOWER_LIP, *MOUTH_CORNERS):
        return None

    def xy(i: int) -> tuple[float, float]:
        return (landmarks[i].x * aspect, landmarks[i].y)

    r = [xy(i) for i in RIGHT_EYE_CORNERS]
    l = [xy(i) for i in LEFT_EYE_CORNERS]
    rc = ((r[0][0] + r[1][0]) / 2, (r[0][1] + r[1][1]) / 2)
    lc = ((l[0][0] + l[1][0]) / 2, (l[0][1] + l[1][1]) / 2)
    width = ((rc[0] - lc[0]) ** 2 + (rc[1] - lc[1]) ** 2) ** 0.5
    if width < 0.02:            # a face this small (or this side-on) is too noisy to steer with
        return None
    nx, ny = xy(NOSE_TIP)

    # MediaPipe names blendshapes from the face's point of view; in a mirrored
    # picture the face's left eye is on the left of the screen, which is the
    # person's own right. Swap so wink_left is the eye on the person's left.
    face_left = _clamp(blendshapes.get(BLINK_LEFT, 0.0))
    face_right = _clamp(blendshapes.get(BLINK_RIGHT, 0.0))
    wink_left, wink_right = (face_right, face_left) if not swap_eyes else (face_left, face_right)

    # Mouth: the blendshape when there is one, else lip gap over mouth width.
    if JAW_OPEN in blendshapes:
        mouth = _clamp(blendshapes[JAW_OPEN])
    else:
        up, lo = xy(UPPER_LIP), xy(LOWER_LIP)
        a, b = xy(MOUTH_CORNERS[0]), xy(MOUTH_CORNERS[1])
        mw = max(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5, 1e-6)
        mouth = _clamp((abs(lo[1] - up[1]) / mw - 0.05) / 0.6)
    smile = _clamp((blendshapes.get(SMILE_LEFT, 0.0) + blendshapes.get(SMILE_RIGHT, 0.0)) / 2)

    return HeadMeasure(nose=(nx / width, ny / width), scale=width, wink_left=wink_left,
                       wink_right=wink_right, mouth_open=mouth, smile=smile)
