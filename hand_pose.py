"""Hand geometry for Finger Mouse: pinch distance, finger states, poses.

Input is MediaPipe's 21 hand landmarks (x and y as fractions of the frame's
width and height, z as depth on the same scale as x). Everything is measured
relative to the size of the hand, so the numbers mean the same whether the
hand is near the camera or far from it, and poses are judged from joint
angles and distances rather than "tip above knuckle", so a tilted or
sideways hand is read the same as an upright one.

Landmark numbering (MediaPipe):
    0 wrist
    thumb   1 CMC   2 MCP   3 IP    4 tip
    index   5 MCP   6 PIP   7 DIP   8 tip
    middle  9       10      11      12
    ring    13      14      15      16
    pinky   17      18      19      20
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

WRIST = 0
THUMB_TIP = 4
INDEX_MCP, INDEX_TIP = 5, 8
MIDDLE_MCP, MIDDLE_TIP = 9, 12
PINKY_MCP = 17
FINGERS = {
    "index": (5, 6, 7, 8),
    "middle": (9, 10, 11, 12),
    "ring": (13, 14, 15, 16),
    "pinky": (17, 18, 19, 20),
}

EXTENDED, PARTIAL, FOLDED = "extended", "partial", "folded"

Vec = tuple[float, float, float]


def _point(lm: Any, aspect: float) -> Vec:
    """Landmark as (x, y, z) in units of frame height, so x and y are comparable."""
    return (lm.x * aspect, lm.y, lm.z * aspect)


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _len(v: Vec) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _cos(a: Vec, b: Vec) -> float:
    la, lb = _len(a), _len(b)
    if la < 1e-6 or lb < 1e-6:
        return 1.0
    return (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (la * lb)


def _dist2d(a: Vec, b: Vec) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def hand_scale(pts: Sequence[Vec]) -> float:
    """Palm size: width across the knuckles, or 55% of wrist-to-middle-knuckle
    when the hand is turned edge-on and the knuckles overlap."""
    palm_width = _dist2d(pts[INDEX_MCP], pts[PINKY_MCP])
    palm_length = _dist2d(pts[WRIST], pts[MIDDLE_MCP])
    return max(palm_width, palm_length * 0.55, 1e-4)


def finger_state(pts: Sequence[Vec], finger: str) -> str:
    """Extended, folded, or in between — from the finger's own joint angles.

    A straight finger has its tip well beyond its middle joint (seen from the
    wrist) and its segments roughly in line. A folded one curls its tip back
    toward the palm. Using 3D angles keeps this true when the hand tilts.
    """
    mcp, pip, dip, tip = (pts[i] for i in FINGERS[finger])
    wrist = pts[WRIST]
    reach = _len(_sub(tip, wrist)) / max(_len(_sub(pip, wrist)), 1e-6)
    straight = min(_cos(_sub(pip, mcp), _sub(dip, pip)), _cos(_sub(dip, pip), _sub(tip, dip)))
    if reach > 1.18 and straight > 0.55:
        return EXTENDED
    if reach < 1.02 or straight < -0.1:
        return FOLDED
    return PARTIAL


@dataclass(frozen=True)
class HandMeasure:
    """One frame's worth of what the hand is doing."""
    scale: float                 # palm size, in frame-height units
    pinch_ratio: float           # thumb–index tip gap ÷ palm size
    fingers: dict[str, str]      # finger -> EXTENDED / PARTIAL / FOLDED
    index_tip: tuple[float, float]   # frame fractions (0–1), for the pointer
    middle_tip: tuple[float, float]
    aspect: float

    def is_pose(self, extended: set[str], folded: set[str]) -> bool:
        return all(self.fingers[f] == EXTENDED for f in extended) and all(
            self.fingers[f] == FOLDED for f in folded
        )


def measure(landmarks: Sequence[Any], aspect: float) -> HandMeasure:
    """Measure a hand from MediaPipe landmarks; aspect = frame width / height."""
    pts = [_point(lm, aspect) for lm in landmarks]
    scale = hand_scale(pts)
    pinch = _dist2d(pts[THUMB_TIP], pts[INDEX_TIP]) / scale
    return HandMeasure(
        scale=scale,
        pinch_ratio=pinch,
        fingers={name: finger_state(pts, name) for name in FINGERS},
        index_tip=(landmarks[INDEX_TIP].x, landmarks[INDEX_TIP].y),
        middle_tip=(landmarks[MIDDLE_TIP].x, landmarks[MIDDLE_TIP].y),
        aspect=aspect,
    )


# ---- poses ------------------------------------------------------------
# Deliberately unlike each other, and unlike the pointing hand people use to
# move the pointer, so one can't be mistaken for another.

def is_scroll_pose(m: HandMeasure, mode: str) -> bool:
    if mode == "index":
        # Index only. Close to how many people point, hence not the default.
        return m.is_pose({"index"}, {"middle", "ring", "pinky"})
    # Default: index and middle up together, like two fingers on a trackpad.
    return m.is_pose({"index", "middle"}, {"ring", "pinky"})


def is_hide_pose(m: HandMeasure) -> bool:
    """Middle finger alone: every other finger must be clearly folded."""
    return m.is_pose({"middle"}, {"index", "ring", "pinky"})


def scroll_anchor_y(m: HandMeasure, mode: str) -> float:
    """The height scrolling follows: the raised fingertip(s)."""
    if mode == "index":
        return m.index_tip[1]
    return (m.index_tip[1] + m.middle_tip[1]) / 2
