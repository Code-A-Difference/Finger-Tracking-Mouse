"""Small, dependency-free gesture state machines used by Finger Mouse.

Keeping timing rules here makes gesture behaviour testable without a webcam or
desktop session.  The UI is responsible for executing the returned actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Action = Literal["pinch_started", "click", "mouse_down", "mouse_up", "armed", "lost"]


@dataclass(frozen=True)
class GestureAction:
    name: Action
    x: int | None = None
    y: int | None = None


class PinchGesture:
    """Debounced click/drag state machine with a locked pinch-start position."""

    def __init__(self, threshold: float, hold_seconds: float, debounce_frames: int = 2) -> None:
        self.threshold = threshold
        self.release_threshold = min(0.85, max(threshold + 0.12, threshold * 1.45))
        self.hold_seconds = hold_seconds
        self.debounce_frames = debounce_frames
        self.armed = False
        self.state = "open"  # open, pending, dragging
        self._closed_frames = 0
        self._open_frames = 0
        self._started_at = 0.0
        self.locked_position: tuple[int, int] | None = None

    def update(self, ratio: float | None, position: tuple[int, int] | None, now: float) -> list[GestureAction]:
        if ratio is None:
            return self.cancel(lost=True)
        actions: list[GestureAction] = []
        if self.state == "open":
            if ratio >= self.release_threshold:
                self._open_frames += 1
                if self._open_frames >= self.debounce_frames and not self.armed:
                    self.armed = True
                    actions.append(GestureAction("armed"))
            else:
                self._open_frames = 0
            if self.armed and ratio <= self.threshold and position is not None:
                self._closed_frames += 1
                if self._closed_frames >= self.debounce_frames:
                    self.state = "pending"
                    self._started_at = now
                    self.locked_position = position
                    self._closed_frames = 0
                    self.armed = False
                    actions.append(GestureAction("pinch_started", *position))
            else:
                self._closed_frames = 0
        elif self.state == "pending":
            if ratio >= self.release_threshold:
                self._open_frames += 1
                if self._open_frames >= self.debounce_frames:
                    actions.append(GestureAction("click", *(self.locked_position or position or (0, 0))))
                    self._reset_open()
            else:
                self._open_frames = 0
                if now - self._started_at >= self.hold_seconds:
                    self.state = "dragging"
                    actions.append(GestureAction("mouse_down", *(self.locked_position or position or (0, 0))))
        else:  # dragging
            if ratio >= self.release_threshold:
                self._open_frames += 1
                if self._open_frames >= self.debounce_frames:
                    actions.append(GestureAction("mouse_up"))
                    self._reset_open()
            else:
                self._open_frames = 0
        return actions

    def cancel(self, lost: bool = False) -> list[GestureAction]:
        actions = [GestureAction("mouse_up")] if self.state == "dragging" else []
        self._reset_open()
        if lost:
            actions.append(GestureAction("lost"))
        return actions

    def _reset_open(self) -> None:
        self.state = "open"
        self.armed = False
        self._closed_frames = self._open_frames = 0
        self.locked_position = None

