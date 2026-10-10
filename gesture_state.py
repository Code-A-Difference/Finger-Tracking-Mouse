"""Gesture state machines for Finger Mouse, plain Python, no camera, no Qt.

Everything that decides what a hand *means* lives here so it can be tested
frame by frame without a webcam:

* ``PointerFilter``, One Euro smoothing for the pointer, with extra
                          damping while a pinch is closing so the pinch
                          motion itself doesn't drag the pointer.
* ``PinchGesture``, quick pinch = click, pinch and hold = press and
                          drag, release = let go; the click point is locked
                          the instant the pinch crosses its threshold.
* ``PointerStabilizer``, holds the pointer still while a click is being
                          decided, and makes drags start exactly on the
                          locked point without a jump.
* ``ScrollGesture``, a separate pose that scrolls like a joystick,
                          with a dead zone, easing and a speed cap.
* ``HeldPose``, "this pose, held steadily for N seconds", used by
                          the optional hide gesture.
* ``GazeCalibration``, fits a raw gaze offset (eye_pose.py) to screen
                          coordinates from a short look-at-these-dots
                          calibration, and applies it afterwards.
* ``steady_reading``, one calibration dot's frames -> one robust reading.
* ``HeadPointer``, ``WinkClick``, ``FaceSwitch``, the head pointer: the
                          nose steers, winks click, mouth/smile are switches.
* ``DwellClick``, eye-tracking's click: hold the (calibrated, smoothed)
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
      crosses the threshold, the exact moment, not after debouncing, and
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


GAZE_FEATURES = 9   # gx, gy, right x, right y, left x, left y, openness, head x, head y


def gaze_features(offset: tuple[float, float], head: tuple[float, float] = (0.0, 0.0),
                  per_eye: Optional[tuple[tuple[float, float], tuple[float, float]]] = None,
                  openness: float = 0.0) -> tuple[float, ...]:
    """Everything the mapping looks at, from one eye_pose.EyeMeasure."""
    (rx, ry), (lx, ly) = per_eye if per_eye is not None else (offset, offset)
    return (offset[0], offset[1], rx, ry, lx, ly, openness, head[0], head[1])


def _round(values: Sequence[float]) -> list[float]:
    return [float(f"{v:.7g}") for v in values]


class GazeCalibration:
    """Maps what the eyes are doing to a normalised (0–1) screen position.

    Three generations, all still loadable:

    1. six terms: a second-degree polynomial in the averaged iris offset.
    2. eight: the same plus head turn and nod.
    3. (current) fifteen terms over nine measurements (``gaze_features``):
       the averaged offset's polynomial, the difference between the two eyes,
       how open the eyes are (the lid follows the eyeball up and down, which
       the iris barely does inside its socket), and head turn and nod with
       how they interact with gaze. Fit to hundreds of samples, a grid of
       fixations plus a followed moving dot, by weighted ridge regression,
       with the amount of ridge chosen by cross-validation so it fits what
       generalises, not the noise.

    A small ``bias`` on top is what the one-second re-centre adjusts when the
    mapping drifts (a shifted chair, a moved laptop), without recalibrating.
    """

    MIN_SAMPLES = 6
    MIN_SAMPLES_V3 = 20
    RIDGE = 1e-4                                    # v1/v2
    RIDGE_GRID = (1e-4, 1e-3, 1e-2, 3e-2, 1e-1)     # v3, chosen by cross-validation

    def __init__(self) -> None:
        self.coeffs_x: Optional[list[float]] = None
        self.coeffs_y: Optional[list[float]] = None
        self.version = 1
        self.use_head = False
        self.head_ref: tuple[float, float] = (0.0, 0.0)
        self.mean: list[float] = [0.0] * GAZE_FEATURES
        self.scale: list[float] = []
        self.bias: tuple[float, float] = (0.0, 0.0)
        self.ridge = 0.0

    @property
    def is_calibrated(self) -> bool:
        return self.coeffs_x is not None and self.coeffs_y is not None

    def reset(self) -> None:
        self.__init__()

    # -- terms ---------------------------------------------------------------
    def _terms(self, offset: tuple[float, float], head: Optional[tuple[float, float]] = None) -> list[float]:
        x, y = offset
        base = [1.0, x, y, x * y, x * x, y * y]
        if not self.use_head:
            return base
        hx, hy = head if head is not None else self.head_ref
        return base + [hx - self.head_ref[0], hy - self.head_ref[1]]

    def _raw_v3(self, f: Sequence[float]) -> list[float]:
        c = [v - m for v, m in zip(f, self.mean)]
        gx, gy = c[0], c[1]
        dx, dy = c[2] - c[4], c[3] - c[5]
        op, hx, hy = c[6], c[7], c[8]
        return [1.0, gx, gy, gx * gy, gx * gx, gy * gy, dx, dy, op, op * gy, op * gx, hx, hy, hx * gx, hy * gy]

    def _terms_v3(self, f: Sequence[float]) -> list[float]:
        raw = self._raw_v3(f)
        return [raw[0]] + [r / s for r, s in zip(raw[1:], self.scale)]

    def _as_features(self, x: Sequence[float], head: Optional[tuple[float, float]]) -> tuple[float, ...]:
        if len(x) == GAZE_FEATURES:
            return tuple(x)
        # an offset on its own: fill the rest in with the calibration's averages
        m = self.mean
        h = head if head is not None else (m[7], m[8])
        return (x[0], x[1], x[0], x[1], x[0], x[1], m[6], h[0], h[1])

    @staticmethod
    def _split(sample):
        """A sample is (offset_or_features, target) or (offset, target, head)."""
        if len(sample) == 3:
            return sample[0], sample[1], sample[2]
        return sample[0], sample[1], None

    # -- fitting -------------------------------------------------------------
    @staticmethod
    def _weighted_fit(rows: list[list[float]], targets: list[tuple[float, float]], weights: list[float],
                      ridge: float) -> Optional[tuple[list[float], list[float]]]:
        n = len(rows[0])
        ata = [[0.0] * n for _ in range(n)]
        atx = [0.0] * n
        aty = [0.0] * n
        total = 0.0
        for row, (tx, ty), w in zip(rows, targets, weights):
            total += w
            for i in range(n):
                wi = w * row[i]
                atx[i] += wi * tx
                aty[i] += wi * ty
                ai = ata[i]
                for j in range(i, n):
                    ai[j] += wi * row[j]
        for i in range(n):
            for j in range(i):
                ata[i][j] = ata[j][i]
        for i in range(1, n):                      # never shrink the constant term
            ata[i][i] += ridge * total
        cx = _solve(ata, atx)
        cy = _solve(ata, aty)
        return (cx, cy) if cx is not None and cy is not None else None

    def fit(self, samples: Sequence[tuple], weights: Optional[Sequence[float]] = None) -> bool:
        """``samples``: [(features_or_offset, (screen_x, screen_y)[, head]), ...];
        ``weights`` (optional) say how much each one counts. Returns whether it took."""
        if len(samples) < self.MIN_SAMPLES:
            return False
        weights = list(weights) if weights is not None else [1.0] * len(samples)
        parts = [self._split(s) for s in samples]
        if all(len(x) == GAZE_FEATURES for x, _, _ in parts):
            return self._fit_v3([x for x, _, _ in parts], [t for _, t, _ in parts], weights)
        return self._fit_legacy(parts, weights)

    def _fit_legacy(self, parts, weights) -> bool:
        heads = [h for _, _, h in parts]
        use_head = all(h is not None for h in heads) and len(parts) >= 9
        head_ref = ((sum(h[0] for h in heads) / len(heads), sum(h[1] for h in heads) / len(heads))
                    if use_head else (0.0, 0.0))
        prev = (self.use_head, self.head_ref)
        self.use_head, self.head_ref = use_head, head_ref
        rows = [self._terms(o, h) for o, _, h in parts]
        fit = self._weighted_fit(rows, [t for _, t, _ in parts], weights, self.RIDGE)
        if fit is None:
            self.use_head, self.head_ref = prev
            return False
        self.coeffs_x, self.coeffs_y = fit
        self.version = 2 if use_head else 1
        return True

    def _fit_v3(self, feats: list, targets: list, weights: list) -> bool:
        if len(feats) < self.MIN_SAMPLES_V3:
            return False
        total = sum(weights)
        self.mean = [sum(w * f[k] for f, w in zip(feats, weights)) / total for k in range(GAZE_FEATURES)]
        raws = [self._raw_v3(f) for f in feats]
        n = len(raws[0])
        self.scale = []
        for k in range(1, n):
            mu = sum(w * r[k] for r, w in zip(raws, weights)) / total
            var = sum(w * (r[k] - mu) ** 2 for r, w in zip(raws, weights)) / total
            self.scale.append(max(var ** 0.5, 1e-6))
        rows = [[r[0]] + [v / s for v, s in zip(r[1:], self.scale)] for r in raws]

        # Pick the ridge by 5-fold cross-validation: the setting that best
        # predicts samples it wasn't fitted on.
        folds = 5
        best = None
        for ridge in self.RIDGE_GRID:
            err = 0.0
            for k in range(folds):
                tr = [i for i in range(len(rows)) if i % folds != k]
                te = [i for i in range(len(rows)) if i % folds == k]
                fit = self._weighted_fit([rows[i] for i in tr], [targets[i] for i in tr], [weights[i] for i in tr], ridge)
                if fit is None:
                    err = float("inf")
                    break
                cx, cy = fit
                for i in te:
                    px = sum(c * t for c, t in zip(cx, rows[i]))
                    py = sum(c * t for c, t in zip(cy, rows[i]))
                    err += weights[i] * math.hypot(px - targets[i][0], py - targets[i][1])
            if best is None or err < best[0]:
                best = (err, ridge)
        fit = self._weighted_fit(rows, targets, weights, best[1])
        if fit is None:
            return False
        self.coeffs_x, self.coeffs_y = fit
        self.version, self.ridge, self.bias = 3, best[1], (0.0, 0.0)
        self.use_head, self.head_ref = False, (0.0, 0.0)
        return True

    # -- using it ------------------------------------------------------------
    def _raw_point(self, x: Sequence[float], head: Optional[tuple[float, float]] = None) -> tuple[float, float]:
        terms = self._terms_v3(self._as_features(x, head)) if self.version == 3 else self._terms(tuple(x[:2]), head)
        px = sum(c * t for c, t in zip(self.coeffs_x, terms)) + self.bias[0]
        py = sum(c * t for c, t in zip(self.coeffs_y, terms)) + self.bias[1]
        return px, py

    def apply(self, x: Sequence[float], head: Optional[tuple[float, float]] = None) -> Optional[tuple[float, float]]:
        """The screen position (clamped 0–1) for ``gaze_features(...)``, or,
        for older callers, a bare offset and head. None before calibration."""
        if not self.is_calibrated:
            return None
        px, py = self._raw_point(x, head)
        return (max(0.0, min(1.0, px)), max(0.0, min(1.0, py)))

    def recentre(self, x: Sequence[float], target: tuple[float, float] = (0.5, 0.5)) -> bool:
        """You're looking at ``target``: shift the whole mapping so that's where
        it lands. Fixes drift without recalibrating."""
        if not self.is_calibrated:
            return False
        px, py = self._raw_point(x)
        self.bias = (self.bias[0] + target[0] - px, self.bias[1] + target[1] - py)
        return True

    def error(self, samples: Sequence[tuple]) -> Optional[float]:
        """Average distance, as a fraction of the screen, between where each
        sample's target was and where the mapping puts it."""
        if not self.is_calibrated or not samples:
            return None
        total = 0.0
        for s in samples:
            x, t, h = self._split(s)
            p = self.apply(x, h)
            total += math.hypot(p[0] - t[0], p[1] - t[1])
        return total / len(samples)

    # -- storing it ----------------------------------------------------------
    def to_json(self) -> str:
        if not self.is_calibrated:
            return ""
        if self.version == 3:
            return json.dumps({"v": 3, "x": _round(self.coeffs_x), "y": _round(self.coeffs_y),
                               "mean": _round(self.mean), "scale": _round(self.scale), "bias": _round(self.bias)})
        data = {"x": self.coeffs_x, "y": self.coeffs_y}
        if self.use_head:
            data["head"] = list(self.head_ref)
        return json.dumps(data)

    @classmethod
    def from_json(cls, text: str) -> "GazeCalibration":
        cal = cls()
        if not text:
            return cal

        def nums(v, n):
            return isinstance(v, list) and len(v) == n and all(isinstance(e, (int, float)) for e in v)

        try:
            data = json.loads(text)
            x, y = data["x"], data["y"]
            if data.get("v") == 3:
                if nums(x, 15) and nums(y, 15) and nums(data["mean"], GAZE_FEATURES) and nums(data["scale"], 14) \
                        and nums(data.get("bias", [0, 0]), 2):
                    cal.version = 3
                    cal.mean = [float(v) for v in data["mean"]]
                    cal.scale = [max(float(v), 1e-6) for v in data["scale"]]
                    cal.bias = tuple(float(v) for v in data.get("bias", [0, 0]))
                    cal.coeffs_x, cal.coeffs_y = [float(v) for v in x], [float(v) for v in y]
                return cal
            head = data.get("head")
            n = 8 if head is not None else 6
            if nums(x, n) and nums(y, n):
                if head is not None:
                    if not nums(head, 2):
                        return cal
                    cal.use_head, cal.version = True, 2
                    cal.head_ref = (float(head[0]), float(head[1]))
                cal.coeffs_x, cal.coeffs_y = [float(v) for v in x], [float(v) for v in y]
        except (TypeError, ValueError, KeyError, AttributeError, json.JSONDecodeError):
            pass
        return cal


def steady_reading(readings: Sequence[tuple], blink_limit: float = 0.35) -> Optional[tuple]:
    """One calibration dot's worth of frames -> one reading.

    ``readings``: [(features_or_offset, head, blink), ...]. Frames taken
    mid-blink are dropped (a closing lid drags the iris landmark down), then
    each value's *median* is used rather than the mean, so a quick glance
    elsewhere or a tracking glitch doesn't pull the dot off. Returns
    (features_or_offset, head), or None if too few usable frames were left.
    """
    usable = [r for r in readings if r[2] < blink_limit]
    if len(usable) < 5:
        return None

    def med(values: list[float]) -> float:
        v = sorted(values)
        mid = len(v) // 2
        return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2

    width = len(usable[0][0])
    first = tuple(med([r[0][k] for r in usable]) for k in range(width))
    head = (med([r[1][0] for r in usable]), med([r[1][1] for r in usable]))
    return first, head


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


# ---------------------------------------------------------------------------
# Head pointer: the nose steers, winks click, the mouth drags or scrolls
# ---------------------------------------------------------------------------

class HeadPointer:
    """Turns nose movement (head_pose.HeadMeasure.nose, in face widths) into
    pointer movement, as a fraction of the screen.

    ``relative`` (the default) works like a mouse: the pointer moves by how
    far the nose moved since the last frame, more for a quick movement than a
    slow one (pointer acceleration), so small careful movements are precise
    and a flick crosses the screen. Turning your head back doesn't bring the
    pointer back with it, exactly like lifting and moving a mouse.

    ``absolute`` maps the nose position directly: straight ahead is the
    centre of the screen and turning ``reach`` face widths takes you to an
    edge. Simple to understand; needs re-centring if you shift in your seat.

    Either way the nose is One-Euro smoothed first, and movement slower than
    ``dead_zone`` (face widths per second) is ignored, so a steady head
    holds a steady pointer instead of drifting with tremor or breathing.
    """

    def __init__(self, mode: str = "relative", speed: float = 45, acceleration: float = 50,
                 dead_zone: float = 20, smoothing: float = 50, reach: float = 35) -> None:
        self.filter = PointerFilter(smoothing)
        self.configure(mode, speed, acceleration, dead_zone, smoothing, reach)
        self.reset()

    def configure(self, mode: str, speed: float, acceleration: float, dead_zone: float,
                  smoothing: float, reach: float) -> None:
        self.mode = mode if mode in ("relative", "absolute") else "relative"
        self.gain = 0.4 + 3.6 * max(1.0, min(100.0, speed)) / 100        # screen widths per face width
        self.accel = max(0.0, min(100.0, acceleration)) / 100 * 2.5
        self.dead_zone = max(0.0, min(100.0, dead_zone)) / 100 * 0.12     # face widths per second
        self.reach = max(10.0, min(80.0, reach)) / 100                     # face widths to the screen edge
        self.filter.set_smoothing(smoothing)

    def reset(self) -> None:
        """Forget the last position (face lost, or a click/drag froze the pointer)."""
        self.filter.reset()
        self._last: Optional[Point] = None
        self._last_t: Optional[float] = None

    def recentre(self) -> None:
        """Absolute mode: wherever the head is now becomes the screen centre."""
        self.neutral: Optional[Point] = None

    neutral: Optional[Point] = None

    def update(self, nose: Point, now: float, frozen: bool = False):
        """Feed one frame. Relative: returns (dx, dy) in screen fractions.
        Absolute: returns (x, y) in 0–1. ``frozen`` (a wink or a drag is
        starting) holds the pointer and forgets the movement meanwhile, so
        letting go doesn't make it jump."""
        if frozen:
            # Forget the movement entirely, the smoothing filter's lag too
            # so the pointer picks up from wherever the head settles.
            self.filter.reset()
            self._last = self._last_t = None
            return None if self.mode == "absolute" else (0.0, 0.0)
        fx, fy = self.filter(nose[0], nose[1], now)
        if self.mode == "absolute":
            if self.neutral is None:
                self.neutral = (fx, fy)
            x = 0.5 + (fx - self.neutral[0]) / (2 * self.reach)
            y = 0.5 + (fy - self.neutral[1]) / (2 * self.reach * 0.7)   # nodding has less range than turning
            return (max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))

        if self._last is None or self._last_t is None:
            self._last, self._last_t = (fx, fy), now
            return (0.0, 0.0)
        dt = max(1e-3, now - self._last_t)
        dx, dy = fx - self._last[0], fy - self._last[1]
        self._last, self._last_t = (fx, fy), now
        speed = math.hypot(dx, dy) / dt
        if speed < self.dead_zone:
            return (0.0, 0.0)
        gain = self.gain * (1 + self.accel * min(speed / 0.6, 3.0))
        return (dx * gain, dy * gain * 1.4)          # nods are smaller than turns: give them more reach


class WinkClick:
    """A one-eyed wink, held briefly, is a click: left eye left click, right
    eye right click. An ordinary blink closes both eyes, so it never counts.
    Fires once per wink; both eyes must open again before the next."""

    CLOSED, OPEN = 0.5, 0.3

    def __init__(self, hold_seconds: float = 0.2) -> None:
        self.hold_seconds = hold_seconds
        self.reset()

    def reset(self) -> None:
        self._side: Optional[str] = None
        self._since: Optional[float] = None
        self._armed = True

    @property
    def closing(self) -> bool:
        """An eye is on its way to a wink: hold the pointer still."""
        return self._side is not None

    def update(self, left: float, right: float, now: float) -> Optional[str]:
        """Feed eye closure (0 open .. 1 shut). Returns "left" / "right" once per wink."""
        if left < self.OPEN and right < self.OPEN:
            self._armed = True
            self._side = self._since = None
            return None
        if left >= self.CLOSED and right >= self.CLOSED:      # both shut: a blink, not a wink
            self._side = self._since = None
            return None
        side = "left" if left >= self.CLOSED and right < self.OPEN else \
               "right" if right >= self.CLOSED and left < self.OPEN else None
        if side is None:
            return None
        if side != self._side:
            self._side, self._since = side, now
            return None
        if self._armed and now - self._since >= self.hold_seconds:
            self._armed = False
            return side
        return None


class FaceSwitch:
    """A face expression (mouth open, smile) used as a switch, with
    hysteresis: on above ``on``, off below ``off``, and it has to stay on for
    ``hold_seconds`` before it counts, so a word or a passing grin doesn't."""

    def __init__(self, on: float, off: float, hold_seconds: float) -> None:
        self.on_level, self.off_level, self.hold_seconds = on, off, hold_seconds
        self.reset()

    def reset(self) -> None:
        self.active = False
        self._since: Optional[float] = None

    @property
    def pending(self) -> bool:
        """Above the threshold but not yet held long enough."""
        return self._since is not None and not self.active

    def update(self, value: float, now: float) -> Optional[str]:
        """Returns "start" when it switches on, "end" when it switches off."""
        if self.active:
            if value < self.off_level:
                self.active = False
                self._since = None
                return "end"
            return None
        if value >= self.on_level:
            if self._since is None:
                self._since = now
            elif now - self._since >= self.hold_seconds:
                self.active = True
                return "start"
        elif value < self.off_level:
            self._since = None
        return None


class SnapLock:
    """Snap: hold the pointer perfectly still once it settles, and let it go
    only on a deliberate move.

    Eye and head tracking are never perfectly steady, even smoothed, the
    pointer shivers around where you're looking, which makes small targets
    hard to hit. With snap on, once the pointer has stayed within ``radius``
    for ``settle_seconds`` it locks to the middle of where it has been, and
    stays locked, not a pixel of movement, until it is pulled more than
    ``radius`` away; then it follows freely again until it settles somewhere
    new. Positions are screen fractions (0–1).
    """

    def __init__(self, radius: float = 0.03, settle_seconds: float = 0.18) -> None:
        self.radius = radius
        self.settle_seconds = settle_seconds
        self.reset()

    def configure(self, radius: float, settle_seconds: Optional[float] = None) -> None:
        self.radius = radius
        if settle_seconds is not None:
            self.settle_seconds = settle_seconds

    def reset(self) -> None:
        self.anchor: Optional[tuple[float, float]] = None   # where it's locked, if it is
        self._start: Optional[tuple[float, float]] = None   # free: where the current settle began
        self._since = 0.0
        self._sum = [0.0, 0.0]
        self._n = 0

    @property
    def locked(self) -> bool:
        return self.anchor is not None

    def update(self, pos: tuple[float, float], now: float) -> tuple[float, float]:
        if self.anchor is not None:
            if math.hypot(pos[0] - self.anchor[0], pos[1] - self.anchor[1]) <= self.radius:
                return self.anchor
            self.anchor = None                  # a deliberate move: let go and follow
            self._start = None
        if self._start is None or math.hypot(pos[0] - self._start[0], pos[1] - self._start[1]) > self.radius / 2:
            self._start, self._since, self._sum, self._n = pos, now, [0.0, 0.0], 0
        self._sum[0] += pos[0]
        self._sum[1] += pos[1]
        self._n += 1
        if now - self._since >= self.settle_seconds and self._n >= 3:
            self.anchor = (self._sum[0] / self._n, self._sum[1] / self._n)
            return self.anchor
        return pos
