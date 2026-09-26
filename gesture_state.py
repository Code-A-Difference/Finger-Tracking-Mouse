"""Gesture state machines for Finger Mouse — plain Python, no camera, no Qt.

Everything that decides what a hand *means* lives here so it can be tested
frame by frame without a webcam:

* ``PointerFilter``     — One Euro smoothing for the pointer, with extra
                          damping while a pinch is closing so the pinch
                          motion itself doesn't drag the pointer.
* ``PinchGesture``      — quick pinch = click, pinch and hold = press and
                          drag, release = let go; the click point is locked
                          the instant the pinch crosses its threshold.
* ``PointerStabilizer`` — holds the pointer still while a click is being
                          decided, and makes drags start exactly on the
                          locked point without a jump.
* ``ScrollGesture``     — a separate pose that scrolls like a joystick,
                          with a dead zone, easing and a speed cap.
* ``HeldPose``          — "this pose, held steadily for N seconds", used by
                          the optional hide gesture.
* ``GazeCalibration``   — fits a raw gaze offset (eye_pose.py) to screen
                          coordinates from a short look-at-these-dots
                          calibration, and applies it afterwards.
* ``DwellClick``        — eye-tracking's click: hold the (calibrated, smoothed)
                          gaze still over one spot for a moment.

The tracking thread feeds these measurements and passes the returned
actions to the pointer output thread, which does the actual clicking.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Literal, Optional, Sequence

Action = Literal[
    "armed",          # an open hand was seen: the next pinch will count
    "pinch_started",  # pinch confirmed; x, y is the locked click point
    "click",          # quick pinch released: click at x, y
    "mouse_down",     # pinch held past the hold time: press at x, y
    "mouse_up",       # release the button (end of drag, or cleanup)
    "cancelled",      # a pinch was abandoned (hand lost) without clicking
    "lost",           # hand gone long enough to reset everything
]

Point = tuple[int, int]


@dataclass(frozen=True)
class GestureAction:
    name: Action
    x: Optional[int] = None
    y: Optional[int] = None


# ---------------------------------------------------------------------------
# Pointer smoothing
# ---------------------------------------------------------------------------

class OneEuroFilter:
    """The One Euro filter (Casiez, Roussel & Vogel, CHI 2012).

    Smooths hard when the signal is nearly still (kills jitter) and eases off
    as it speeds up (keeps lag low on real movement). ``min_cutoff`` sets the
    smoothing at rest, ``beta`` how quickly speed relaxes it.
    """

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0, d_cutoff: float = 1.0) -> None:
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.reset()

    def reset(self) -> None:
        self._x: Optional[float] = None
        self._dx = 0.0
        self._t: Optional[float] = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * max(cutoff, 1e-3))
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x: float, t: float, cutoff_scale: float = 1.0) -> float:
        if self._x is None or self._t is None:
            self._x, self._t = x, t
            return x
        dt = t - self._t
        if dt <= 0:
            return self._x
        dx = (x - self._x) / dt
        self._dx += self._alpha(self.d_cutoff, dt) * (dx - self._dx)
        cutoff = (self.min_cutoff + self.beta * abs(self._dx)) * cutoff_scale
        self._x += self._alpha(cutoff, dt) * (x - self._x)
        self._t = t
        return self._x


class PointerFilter:
    """2D One Euro filter over normalised (0–1) screen coordinates.

    ``smoothing`` 0–100 maps to a resting cut-off from 4 Hz (light) down to
    0.3 Hz (heavy). ``approach`` 0–1 says how far a pinch has closed; as it
    closes, the cut-off drops by up to 90%, so the small movement of the index
    fingertip that pinching causes barely moves the pointer.
    """

    BETA = 6.0  # tuned for 0–1 coordinates; hand sweeps of ~1 screen/second

    def __init__(self, smoothing: int = 50) -> None:
        self._fx = OneEuroFilter()
        self._fy = OneEuroFilter()
        self.set_smoothing(smoothing)

    def set_smoothing(self, smoothing: int) -> None:
        s = max(0, min(100, smoothing)) / 100
        cutoff = 4.0 * (0.3 / 4.0) ** s            # 4 Hz -> 0.3 Hz, log-spaced
        for f in (self._fx, self._fy):
            f.min_cutoff = cutoff
            f.beta = self.BETA * (1 - 0.6 * s)

    def reset(self) -> None:
        self._fx.reset()
        self._fy.reset()

    def __call__(self, x: float, y: float, t: float, approach: float = 0.0) -> tuple[float, float]:
        scale = 1.0 - 0.9 * max(0.0, min(1.0, approach))
        return self._fx(x, t, scale), self._fy(y, t, scale)


# ---------------------------------------------------------------------------
# Pinch: click and drag
# ---------------------------------------------------------------------------

class PinchGesture:
    """Quick pinch clicks; pinch and hold presses (drag); release lets go.

    States::

        idle ──open hand──▶ ready ──crosses threshold──▶ closing
          ▲                   ▲                              │ held N frames
          │                   │◀── released ── pressed ◀─────┘
          │                   │                  │ held past hold time
          │                   └──── released ── dragging
          └──────────── hand lost past the grace period (from any state)

    * The click point is locked on the **first frame** the pinch ratio
      crosses the threshold — the exact moment, not after debouncing — and
      every click, press and drag start uses it.
    * ``confirm_frames`` must agree before a pinch or a release counts, and
      releasing needs the fingers to open past a wider threshold
      (hysteresis), so jitter around the line can't click or drop by itself.
    * A hand must be seen open (``ready``) before a pinch counts, so a hand
      arriving already pinched, or a fist, doesn't click.
    * Losing the hand for longer than ``loss_grace`` s cancels a pending
      click and always releases a held button.
    """

    CLOSING_TIMEOUT = 0.35  # s a pinch may take to confirm before it's dropped

    def __init__(
        self,
        threshold: float,
        hold_seconds: float,
        confirm_frames: int = 2,
        release_threshold: Optional[float] = None,
        drag_enabled: bool = True,
        loss_grace: float = 0.25,
    ) -> None:
        self.threshold = threshold
        self.release_threshold = (
            release_threshold if release_threshold is not None
            else min(0.85, max(threshold + 0.12, threshold * 1.45))
        )
        self.hold_seconds = hold_seconds
        self.confirm_frames = max(1, confirm_frames)
        self.drag_enabled = drag_enabled
        self.loss_grace = loss_grace
        self.state = "idle"
        self.locked_position: Optional[Point] = None
        self._crossed_at = 0.0
        self._count = 0            # frames agreeing with the pending change
        self._lost_since: Optional[float] = None

    # -- configuration that can change while tracking ------------------------
    def configure(self, threshold: float, release_threshold: float, hold_seconds: float,
                  confirm_frames: int, drag_enabled: bool) -> None:
        self.threshold = threshold
        self.release_threshold = max(release_threshold, threshold + 0.02)
        self.hold_seconds = hold_seconds
        self.confirm_frames = max(1, confirm_frames)
        self.drag_enabled = drag_enabled

    @property
    def armed(self) -> bool:
        return self.state == "ready"

    @property
    def active(self) -> bool:
        """A pinch is in progress (being confirmed, held or dragging)."""
        return self.state in ("closing", "pressed", "dragging")

    def approach(self, ratio: Optional[float]) -> float:
        """0 when open, rising to 1 as the fingers reach the click threshold."""
        if ratio is None or self.state not in ("ready", "closing"):
            return 0.0
        span = max(self.release_threshold - self.threshold, 1e-6)
        return max(0.0, min(1.0, (self.release_threshold - ratio) / span))

    def update(self, ratio: Optional[float], position: Optional[Point], now: float,
               hold_still: bool = False) -> list[GestureAction]:
        """Feed one frame. ``ratio`` None = no hand this frame.

        ``hold_still`` = the frame can't be judged (e.g. a fist, where the
        thumb and index are close without pinching): nothing advances.
        """
        if ratio is None or position is None:
            if self._lost_since is None:
                self._lost_since = now
            if now - self._lost_since >= self.loss_grace and self.state != "idle":
                return self.cancel(lost=True)
            return []
        self._lost_since = None
        if hold_still:
            self._count = 0
            return []

        actions: list[GestureAction] = []
        is_closed = ratio <= self.threshold
        is_open = ratio >= self.release_threshold

        if self.state == "idle":
            self._count = self._count + 1 if is_open else 0
            if self._count >= self.confirm_frames:
                self._set("ready")
                actions.append(GestureAction("armed"))

        elif self.state == "ready":
            if is_closed:
                # The exact moment of crossing: lock here, before confirming.
                self.locked_position = position
                self._crossed_at = now
                self._set("closing")
                self._count = 1
                actions += self._confirm_pinch()

        elif self.state == "closing":
            if is_open or now - self._crossed_at > self.CLOSING_TIMEOUT:
                # A flicker, or a half-pinch that never settled: not a click,
                # and the pointer must not stay frozen waiting for one.
                self._set("ready")
                self.locked_position = None
            elif is_closed:
                self._count += 1
                actions += self._confirm_pinch()
            # between the two thresholds: neither confirms nor cancels

        elif self.state == "pressed":
            if is_open:
                self._count += 1
                if self._count >= self.confirm_frames:
                    if self.drag_enabled:   # with drag off, the click already happened
                        actions.append(GestureAction("click", *self._lock()))
                    self._release()
            else:
                self._count = 0
                if self.drag_enabled and now - self._crossed_at >= self.hold_seconds:
                    self._set("dragging")
                    actions.append(GestureAction("mouse_down", *self._lock()))

        elif self.state == "dragging":
            if is_open:
                self._count += 1
                if self._count >= self.confirm_frames:
                    actions.append(GestureAction("mouse_up"))
                    self._release()
            else:
                self._count = 0
        return actions

    def cancel(self, lost: bool = False) -> list[GestureAction]:
        """Abandon whatever is in progress. Always releases a held button."""
        actions: list[GestureAction] = []
        if self.state == "dragging":
            actions.append(GestureAction("mouse_up"))
        elif self.state in ("closing", "pressed"):
            actions.append(GestureAction("cancelled"))
        if lost:
            actions.append(GestureAction("lost"))
        self._set("idle")
        self.locked_position = None
        self._lost_since = None
        return actions

    # -- internals -----------------------------------------------------------
    def _confirm_pinch(self) -> list[GestureAction]:
        if self._count < self.confirm_frames:
            return []
        self._set("pressed")
        actions = [GestureAction("pinch_started", *self._lock())]
        if not self.drag_enabled:
            # No drag to wait for, so no reason to wait for the release.
            actions.append(GestureAction("click", *self._lock()))
        return actions

    def _release(self) -> None:
        # Open fingers are already an open hand: ready for the next pinch.
        self._set("ready")
        self.locked_position = None

    def _lock(self) -> Point:
        return self.locked_position or (0, 0)

    def _set(self, state: str) -> None:
        self.state = state
        self._count = 0


class PointerStabilizer:
    """Decides where the pointer goes around a pinch.

    * While a click is being decided (``closing``/``pressed``) the pointer
      stays exactly on the locked point, whatever the hand does. ``frozen``
      does the same for other gestures (scrolling) that act under the pointer.
    * When a drag starts it moves *relative* to that point, so the drag
      begins exactly where the pinch did, with no jump.
    * After a click or a drop, any gap between where the pointer is and where
      the hand points is closed smoothly over ``settle`` seconds rather than
      in one jump.
    """

    def __init__(self, settle: float = 0.25) -> None:
        self.settle = settle
        self.reset()

    def reset(self) -> None:
        self._offset = (0.0, 0.0)
        self._offset_at = 0.0
        self._drag_anchor: Optional[tuple[float, float]] = None
        self._last: Optional[Point] = None
        self._was_holding = False

    def update(self, target: tuple[float, float], state: str, lock: Optional[Point],
               now: float) -> Point:
        if state in ("closing", "pressed", "frozen") and lock is not None:
            self._drag_anchor = None
            out = lock
        elif state == "dragging" and lock is not None:
            if self._drag_anchor is None:
                self._drag_anchor = target
            out = (round(lock[0] + target[0] - self._drag_anchor[0]),
                   round(lock[1] + target[1] - self._drag_anchor[1]))
        else:
            if self._last is not None and self._was_holding:
                # Just released: start from where the pointer actually is.
                self._offset = (self._last[0] - target[0], self._last[1] - target[1])
                self._offset_at = now
            self._drag_anchor = None
            fade = math.exp(-(now - self._offset_at) / max(self.settle / 3, 1e-3))
            out = (round(target[0] + self._offset[0] * fade),
                   round(target[1] + self._offset[1] * fade))
        self._was_holding = state in ("closing", "pressed", "dragging", "frozen")
        self._last = out
        return out


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------

class ScrollGesture:
    """Scroll by holding a pose and moving it up or down, like a joystick.

    Hold the scroll pose steadily for ``enter_frames`` to start. Where the
    hand is then becomes the centre. Move beyond the dead zone and the page
    scrolls, faster the further you go; come back to the centre to stop.
    Offsets are measured in palm sizes, so it feels the same near or far from
    the camera. Output is in wheel "notches" (1 = one click of a mouse wheel);
    the pointer output turns fractions into smooth scrolling.
    """

    MAX_NOTCHES_PER_SECOND = 40.0

    def __init__(self, sensitivity: int = 35, dead_zone: float = 0.25, reverse: bool = False,
                 enter_frames: int = 4, exit_frames: int = 4, ease_seconds: float = 0.15) -> None:
        self.configure(sensitivity, dead_zone, reverse)
        self.enter_frames = enter_frames
        self.exit_frames = exit_frames
        self.ease_seconds = ease_seconds
        self.active = False
        self._pose_frames = 0
        self._miss_frames = 0
        self._anchor = 0.0
        self._y: Optional[float] = None
        self._last_t: Optional[float] = None
        self._moving_since: Optional[float] = None

    def configure(self, sensitivity: int, dead_zone: float, reverse: bool) -> None:
        # sensitivity 5–100 -> 1.5–30 notches/second one palm beyond the dead zone
        self.gain = 0.3 * max(5, min(100, sensitivity))
        self.dead_zone = max(0.02, dead_zone)
        self.reverse = reverse

    def reset(self) -> None:
        self.active = False
        self._pose_frames = self._miss_frames = 0
        self._y = self._last_t = self._moving_since = None

    def update(self, pose: bool, y: Optional[float], scale: float, allowed: bool,
               now: float) -> float:
        """Return how many notches to scroll this frame (+ = up, − = down)."""
        if not allowed or y is None:
            self.reset()
            return 0.0

        if not self.active:
            self._pose_frames = self._pose_frames + 1 if pose else 0
            if self._pose_frames >= self.enter_frames:
                self.active = True
                self._anchor = self._y = y
                self._last_t = now
                self._miss_frames = 0
                self._moving_since = None
            return 0.0

        if not pose:
            self._miss_frames += 1
            if self._miss_frames >= self.exit_frames:
                self.reset()
            return 0.0
        self._miss_frames = 0

        # Light smoothing on the height; landmarks wobble a little every frame.
        self._y = y if self._y is None else self._y + 0.45 * (y - self._y)
        dt = min(0.1, max(0.0, now - (self._last_t or now)))
        self._last_t = now

        offset = (self._anchor - self._y) / max(scale, 1e-4)   # palm sizes; + = hand moved up
        excess = abs(offset) - self.dead_zone
        if excess <= 0:
            self._moving_since = None
            return 0.0
        if self._moving_since is None:
            self._moving_since = now
        ease = min(1.0, (now - self._moving_since) / self.ease_seconds) if self.ease_seconds else 1.0
        speed = min(self.MAX_NOTCHES_PER_SECOND, self.gain * excess ** 1.3) * ease
        notches = math.copysign(speed * dt, offset)
        return -notches if self.reverse else notches


# ---------------------------------------------------------------------------
# Held pose (the optional hide gesture)
# ---------------------------------------------------------------------------

class HeldPose:
    """Fires once when a pose has been held for ``hold_seconds``.

    Up to ``tolerance`` frames of dropout are forgiven (tracking flickers),
    and after firing the pose must be let go and ``cooldown`` must pass
    before it can fire again, so it can't repeat by accident.
    """

    def __init__(self, hold_seconds: float = 1.2, tolerance: int = 2, cooldown: float = 3.0) -> None:
        self.hold_seconds = hold_seconds
        self.tolerance = tolerance
        self.cooldown = cooldown
        self._since: Optional[float] = None
        self._misses = 0
        self._latched = False
        self._fired_at = -1e9

    def reset(self) -> None:
        self._since = None
        self._misses = 0

    def progress(self, now: float) -> float:
        if self._since is None or self._latched:
            return 0.0
        return max(0.0, min(1.0, (now - self._since) / self.hold_seconds))

    def update(self, pose: bool, now: float) -> bool:
        if not pose:
            self._misses += 1
            if self._misses > self.tolerance:
                self._since = None
                self._latched = False
            return False
        self._misses = 0
        if self._latched or now - self._fired_at < self.cooldown:
            return False
        if self._since is None:
            self._since = now
        if now - self._since >= self.hold_seconds:
            self._latched = True
            self._fired_at = now
            self._since = None
            return True
        return False


# ---------------------------------------------------------------------------
# Eye tracking: turning a raw gaze offset into a screen point, and a dwell
# into a click
# ---------------------------------------------------------------------------

def _solve(matrix: list[list[float]], vector: list[float]) -> Optional[list[float]]:
    """Solve a small linear system by Gauss-Jordan elimination with partial
    pivoting. None if it's singular (degenerate calibration points)."""
    n = len(matrix)
    aug = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < 1e-9:
            return None
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        pivot = aug[col][col]
        aug[col] = [v / pivot for v in aug[col]]
        for r in range(n):
            if r != col:
                factor = aug[r][col]
                aug[r] = [aug[r][k] - factor * aug[col][k] for k in range(n + 1)]
    return [aug[i][n] for i in range(n)]


class GazeCalibration:
    """Maps a raw gaze offset (eye_pose.EyeMeasure.offset) to a normalised
    (0–1) screen position.

    A classic six-term second-degree polynomial in the two offset axes
    (``1, x, y, xy, x², y²`` for each of screen-x and screen-y), the standard
    simple mapping for webcam eye tracking: it bends enough to follow how an
    eyeball's rotation maps onto a flat screen, without enough free
    parameters to overfit a short calibration. Fit by least squares (the
    normal equations, solved directly — nine calibration points and six
    terms, no need for numpy here), so a slightly misjudged dot averages out
    rather than distorting the whole mapping.
    """

    MIN_SAMPLES = 6

    def __init__(self) -> None:
        self.coeffs_x: Optional[list[float]] = None
        self.coeffs_y: Optional[list[float]] = None

    @property
    def is_calibrated(self) -> bool:
        return self.coeffs_x is not None and self.coeffs_y is not None

    def reset(self) -> None:
        self.coeffs_x = None
        self.coeffs_y = None

    @staticmethod
    def _terms(offset: tuple[float, float]) -> list[float]:
        x, y = offset
        return [1.0, x, y, x * y, x * x, y * y]

    def fit(self, samples: Sequence[tuple[tuple[float, float], tuple[float, float]]]) -> bool:
        """``samples``: [(gaze_offset, (screen_x, screen_y)), ...], both 0–1
        or -1..1 as produced by eye_pose. Returns whether it took."""
        if len(samples) < self.MIN_SAMPLES:
            return False
        terms = [self._terms(offset) for offset, _ in samples]
        n = len(terms[0])
        ata = [[sum(row[i] * row[j] for row in terms) for j in range(n)] for i in range(n)]
        atx = [sum(row[i] * target[0] for row, (_, target) in zip(terms, samples)) for i in range(n)]
        aty = [sum(row[i] * target[1] for row, (_, target) in zip(terms, samples)) for i in range(n)]
        cx = _solve(ata, atx)
        cy = _solve(ata, aty)
        if cx is None or cy is None:
            return False
        self.coeffs_x, self.coeffs_y = cx, cy
        return True

    def apply(self, offset: tuple[float, float]) -> Optional[tuple[float, float]]:
        """The screen position (clamped 0–1) this offset maps to, or None
        before calibration."""
        if not self.is_calibrated:
            return None
        terms = self._terms(offset)
        x = sum(c * t for c, t in zip(self.coeffs_x, terms))
        y = sum(c * t for c, t in zip(self.coeffs_y, terms))
        return (max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))

    def to_json(self) -> str:
        if not self.is_calibrated:
            return ""
        return json.dumps({"x": self.coeffs_x, "y": self.coeffs_y})

    @classmethod
    def from_json(cls, text: str) -> "GazeCalibration":
        cal = cls()
        if not text:
            return cal
        try:
            data = json.loads(text)
            x, y = data["x"], data["y"]
            if (isinstance(x, list) and isinstance(y, list) and len(x) == len(y) == 6
                    and all(isinstance(v, (int, float)) for v in x + y)):
                cal.coeffs_x, cal.coeffs_y = [float(v) for v in x], [float(v) for v in y]
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            pass
        return cal


class DwellClick:
    """Click by looking, and holding still: the eye-tracking equivalent of a
    pinch. A real gaze never sits at one exact pixel, so "still" means
    within ``radius`` of a rolling anchor, not motionless; drifting past the
    radius re-anchors instead of cancelling, so an unsteady gaze still gets
    there. Firing needs looking away and back (past ``radius``) before it can
    fire again, so it can't repeat by staring.
    """

    def __init__(self, hold_seconds: float = 0.7, radius: float = 0.035) -> None:
        self.hold_seconds = hold_seconds
        self.radius = radius
        self.reset()

    def configure(self, hold_seconds: float, radius: float) -> None:
        self.hold_seconds = hold_seconds
        self.radius = radius

    def reset(self) -> None:
        self._anchor: Optional[Point] = None
        self._since: Optional[float] = None
        self._armed = True   # must move away from the last click point before it can fire again

    def progress(self, now: float) -> float:
        if self._since is None or not self._armed:
            return 0.0
        return max(0.0, min(1.0, (now - self._since) / self.hold_seconds))

    def update(self, position: Optional[Point], now: float) -> bool:
        """Feed one frame's (smoothed, calibrated) gaze point. True = click now."""
        if position is None:
            self.reset()
            return False
        if self._anchor is None:
            self._anchor = position
            self._since = now
            return False
        moved = math.hypot(position[0] - self._anchor[0], position[1] - self._anchor[1])
        if moved > self.radius:
            self._anchor = position
            self._since = now
            self._armed = True
            return False
        if not self._armed:
            return False
        if self._since is not None and now - self._since >= self.hold_seconds:
            self._armed = False
            self._since = now   # so progress() doesn't jump back to 100% mid-cooldown
            return True
        return False
