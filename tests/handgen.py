"""Synthetic MediaPipe-style hands for tests: any pose, any rotation, any place.

Coordinates are built in a hand-local frame (palm width ≈ 1, fingers pointing
"up" = −y), then rotated, scaled and moved into normalised frame
coordinates, the way MediaPipe reports them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class LM:
    x: float
    y: float
    z: float = 0.0


_MCP = {"index": (-0.45, -1.0), "middle": (-0.15, -1.08), "ring": (0.15, -1.04), "pinky": (0.42, -0.95)}
_ORDER = ("index", "middle", "ring", "pinky")


def make_hand(extended=("index",), pinch: float | None = None, rotate: float = 0.0,
              center=(0.5, 0.55), size: float = 0.12, aspect: float = 16 / 9):
    """21 landmarks.

    extended: fingers held straight; the rest are curled into the palm.
    pinch:    thumb-to-index tip gap as a fraction of palm width (None = thumb out).
    rotate:   degrees, in the image plane.
    size:     palm width as a fraction of frame height.
    """
    pts = [(0.0, 0.0, 0.0)] * 21
    pts[0] = (0.0, 0.0, 0.0)
    for f, base in zip(_ORDER, (5, 9, 13, 17)):
        mx, my = _MCP[f]
        if f in extended:
            joints = [(mx, my), (mx, my - 0.45), (mx, my - 0.73), (mx, my - 0.95)]
            zs = [0, 0, 0, 0]
        else:
            joints = [(mx, my), (mx, my - 0.35), (mx, my - 0.2), (mx, my - 0.05)]
            zs = [0, -0.2, -0.35, -0.3]
        for k, ((x, y), z) in enumerate(zip(joints, zs)):
            pts[base + k] = (x, y, z)
    index_tip = pts[8]
    if pinch is None:
        thumb = [(-0.35, -0.25), (-0.6, -0.45), (-0.8, -0.62), (-0.95, -0.78)]
    else:
        # Put the thumb tip `pinch` palm-widths from the index tip, off to its side.
        tx, ty = index_tip[0] - pinch, index_tip[1]
        thumb = [(-0.35, -0.25), (-0.55, -0.45), ((tx - 0.55) / 2 - 0.1, (ty - 0.45) / 2), (tx, ty)]
    for k, (x, y) in enumerate(thumb):
        pts[1 + k] = (x, y, 0.0)

    a = math.radians(rotate)
    ca, sa = math.cos(a), math.sin(a)
    out = []
    for x, y, z in pts:
        rx, ry = x * ca - y * sa, x * sa + y * ca
        # size is in frame-height units; x is normalised by width
        out.append(LM(center[0] + rx * size / aspect, center[1] + ry * size, z * size / aspect))
    return out


def index_tip_at(landmarks, x: float, y: float):
    """Translate a hand so its index fingertip sits at (x, y)."""
    dx, dy = x - landmarks[8].x, y - landmarks[8].y
    return [LM(p.x + dx, p.y + dy, p.z) for p in landmarks]
