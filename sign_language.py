"""Typing with fingerspelling (the ASL manual alphabet).

Every hand is different, and so is every signer's camera angle, so rather
than guessing at letters with hand-written rules, Finger Mouse learns *your*
signs: you show it each letter for a couple of seconds once, and from then on
it recognises your hand by comparing it with what you showed it. That is far
more accurate than generic rules, and it means you choose the space and
delete signs too.

Pure Python, no camera or MediaPipe, so it is tested with synthetic hands.

* ``sign_features``  — one hand (MediaPipe's 21 landmarks) -> a feature vector
                       that ignores where the hand is, how big it looks and how
                       it's tilted, but keeps the shape, and keeps which way
                       it points (K and P, G and Q differ only in that).
* ``SignBook``       — the signs you taught, and recognition by comparing with
                       them (nearest neighbours), refusing to guess when the
                       hand doesn't clearly match one sign.
* ``SignTyper``      — hold a sign steady for a moment to type it once; change
                       or relax the hand to type it again.
"""

from __future__ import annotations

import json
import math
from typing import Any, Optional, Sequence

LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
SPACE, DELETE, ENTER = "SPACE", "DELETE", "ENTER"
SIGNS = LETTERS + [SPACE, DELETE, ENTER]

# What each sign looks like, shown while you teach it. J and Z are moving
# letters; they're taught (and recognised) by the shape they end on.
HOW_TO = {
    "A": "Fist, thumb resting against the side of the index finger.",
    "B": "Flat hand, fingers up and together, thumb folded across the palm.",
    "C": "Curve the hand into a C, fingers together.",
    "D": "Index finger up; the other fingertips touch the thumb.",
    "E": "Fingertips bent down to rest on the thumb, tucked across the palm.",
    "F": "Index fingertip touches the thumb tip; the other three fingers up and apart.",
    "G": "Index and thumb point sideways, parallel, the rest curled.",
    "H": "Index and middle fingers point sideways together, thumb tucked.",
    "I": "Little finger up, the rest a fist.",
    "J": "Little finger up, then trace a J — hold the shape where the J ends.",
    "K": "Index and middle up in a V, thumb touching the middle finger between them.",
    "L": "Thumb and index make an L.",
    "M": "Thumb tucked under the first three fingers.",
    "N": "Thumb tucked under the first two fingers.",
    "O": "All fingertips curve to touch the thumb tip: an O.",
    "P": "Like K, but pointing down.",
    "Q": "Like G, but pointing down.",
    "R": "Index and middle fingers crossed.",
    "S": "Fist, thumb across the front of the fingers.",
    "T": "Fist, thumb tucked between index and middle fingers.",
    "U": "Index and middle fingers up, together.",
    "V": "Index and middle fingers up, apart (a V).",
    "W": "Index, middle and ring fingers up, apart.",
    "X": "Index finger hooked, the rest a fist.",
    "Y": "Thumb and little finger out, the rest folded.",
    "Z": "Index finger traces a Z — hold the shape where it ends, index pointing.",
    SPACE: "Your choice — an open hand with all five fingers spread works well.",
    DELETE: "Your choice — a thumbs-down works well.",
    ENTER: "Optional, your choice — for a new line or to send. A flat hand facing sideways works well.",
}

TIPS = (4, 8, 12, 16, 20)
MAX_SAMPLES = 24          # kept per sign: plenty to compare against, small to store


def sign_features(landmarks: Sequence[Any], aspect: float) -> Optional[tuple[float, ...]]:
    """Shape of one hand, independent of where it is, how big it looks and
    how it's tilted — plus which way it's pointing, kept separately."""
    if len(landmarks) < 21:
        return None
    pts = [(lm.x * aspect, lm.y, getattr(lm, "z", 0.0) * aspect) for lm in landmarks]
    wx, wy, wz = pts[0]
    ux, uy = pts[9][0] - wx, pts[9][1] - wy                 # wrist -> middle knuckle: "up" for the hand
    palm = math.hypot(ux, uy)
    width = math.hypot(pts[5][0] - pts[17][0], pts[5][1] - pts[17][1])
    size = max((palm + width) / 2, 1e-6)
    if palm < 1e-6:
        return None
    angle = math.atan2(ux, -uy)                              # 0 = fingers up
    ca, sa = math.cos(-angle), math.sin(-angle)
    out: list[float] = []
    for x, y, z in pts[1:]:
        dx, dy = x - wx, y - wy
        rx, ry = dx * ca - dy * sa, dx * sa + dy * ca
        out += [rx / size, ry / size, (z - wz) / size * 0.5]
    for i, a in enumerate(TIPS):                             # fingertip-to-fingertip gaps: crossed, touching, spread
        for b in TIPS[i + 1:]:
            pa, pb = pts[a], pts[b]
            out.append(1.5 * math.hypot(pa[0] - pb[0], pa[1] - pb[1]) / size)
    out += [1.5 * math.sin(angle), 1.5 * math.cos(angle)]   # which way the hand points
    return tuple(out)


def _dist(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


class SignBook:
    """The signs you taught, and recognising them."""

    MARGIN = 0.85         # the best sign must be clearly closer than the runner-up
    NEIGHBOURS = 3

    def __init__(self) -> None:
        self.samples: dict[str, list[tuple[float, ...]]] = {}
        self._spread: dict[str, float] = {}

    # -- teaching --------------------------------------------------------
    def teach(self, sign: str, examples: Sequence[Sequence[float]]) -> int:
        """Replace what's known about ``sign`` with these examples (evenly
        thinned to MAX_SAMPLES). Returns how many were kept."""
        examples = [tuple(e) for e in examples if e]
        if not examples:
            return 0
        if len(examples) > MAX_SAMPLES:
            step = len(examples) / MAX_SAMPLES
            examples = [examples[int(i * step)] for i in range(MAX_SAMPLES)]
        self.samples[sign] = examples
        self._update_spread(sign)
        return len(examples)

    def forget(self, sign: Optional[str] = None) -> None:
        if sign is None:
            self.samples.clear()
            self._spread.clear()
        else:
            self.samples.pop(sign, None)
            self._spread.pop(sign, None)

    @property
    def taught(self) -> list[str]:
        return [s for s in SIGNS if s in self.samples]

    def _update_spread(self, sign: str) -> None:
        """How much your own examples of this sign vary: the yardstick for
        "close enough" when recognising it."""
        ex = self.samples[sign]
        if len(ex) < 2:
            self._spread[sign] = 0.4
            return
        nearest = sorted(min(_dist(a, b) for j, b in enumerate(ex) if j != i) for i, a in enumerate(ex))
        self._spread[sign] = max(nearest[len(nearest) // 2], 0.05)

    # -- recognising -----------------------------------------------------
    def _class_distance(self, sign: str, f: Sequence[float]) -> float:
        ds = sorted(_dist(f, e) for e in self.samples[sign])
        k = ds[: self.NEIGHBOURS]
        return sum(k) / len(k)

    def recognise(self, f: Optional[Sequence[float]]) -> tuple[Optional[str], float]:
        """(sign, confidence 0–1), or (None, 0) if the hand doesn't clearly
        match one of the taught signs."""
        if f is None or not self.samples:
            return None, 0.0
        scored = sorted((self._class_distance(s, f), s) for s in self.samples)
        best_d, best = scored[0]
        if best_d > 3.0 * self._spread.get(best, 0.4) + 0.25:
            return None, 0.0                                    # not like anything taught
        if len(scored) > 1:
            second_d = scored[1][0]
            if best_d > self.MARGIN * second_d:
                return None, 0.0                                # too close a call to type anything
            confidence = 1 - best_d / max(second_d, 1e-9)
        else:
            confidence = 1.0
        return best, max(0.0, min(1.0, confidence))

    # -- storing ---------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps({"v": 1, "signs": {s: [[round(v, 3) for v in e] for e in ex]
                                             for s, ex in self.samples.items()}})

    @classmethod
    def from_json(cls, text: str) -> "SignBook":
        book = cls()
        try:
            data = json.loads(text)
            for sign, examples in data.get("signs", {}).items():
                if sign in SIGNS and isinstance(examples, list):
                    clean = [tuple(float(v) for v in e) for e in examples
                             if isinstance(e, list) and all(isinstance(v, (int, float)) for v in e)]
                    width = len(clean[0]) if clean else 0
                    clean = [e for e in clean if len(e) == width]
                    if clean:
                        book.samples[sign] = clean[:MAX_SAMPLES]
                        book._update_spread(sign)
        except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
            return cls()
        return book


class SignTyper:
    """Turns a stream of recognised signs into typing.

    A sign types once it has been held steadily for ``hold_seconds`` with at
    least ``min_confidence``. It then won't type again until the hand has
    changed — a different sign, a relaxed hand, or no hand — for at least
    ``release_seconds``; that's how you spell a double letter ("ll"): sign
    it, relax for a moment, sign it again.
    """

    def __init__(self, hold_seconds: float = 0.6, min_confidence: float = 0.2,
                 release_seconds: float = 0.25) -> None:
        self.hold_seconds = hold_seconds
        self.min_confidence = min_confidence
        self.release_seconds = release_seconds
        self.reset()

    def reset(self) -> None:
        self.current: Optional[str] = None
        self._since = 0.0
        self._last_typed: Optional[str] = None
        self._away_since: Optional[float] = None

    def progress(self, now: float) -> float:
        if self.current is None or self.current == self._last_typed:
            return 0.0
        return max(0.0, min(1.0, (now - self._since) / self.hold_seconds))

    def update(self, sign: Optional[str], confidence: float, now: float) -> Optional[str]:
        """Feed one frame's recognition. Returns the sign to type, once."""
        if sign is not None and confidence < self.min_confidence:
            sign = None
        if self._last_typed is not None:
            if sign == self._last_typed:
                self._away_since = None
            else:
                if self._away_since is None:
                    self._away_since = now
                if now - self._away_since >= self.release_seconds:
                    self._last_typed = None              # released: the same sign may type again
        if sign != self.current:
            self.current, self._since = sign, now
            return None
        if sign is None or sign == self._last_typed:
            return None
        if now - self._since >= self.hold_seconds:
            self._last_typed = sign
            self._away_since = None
            return sign
        return None
