"""Lightweight diagnostics: where time goes, and who is stuck when it freezes.

Finger Mouse runs four things at once — camera capture, hand tracking, the
window, and pointer output — each on its own thread so none can block the
others. When something still feels frozen, the question is *which* one. A
watchdog thread checks each stage's heartbeat four times a second and logs
a single line naming the stage that stopped ("camera read blocked for
2.1 s"), then another when it recovers. The same numbers feed the optional
diagnostics line in the window.

Logs go to the per-user config folder: logs/finger-mouse.log, kept small.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("finger_mouse.diagnostics")


def setup_logging(folder: Path, verbose: bool = False) -> Path:
    """Rotating log file (1 MB x 3) plus uncaught-exception capture on every thread."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "finger-mouse.log"
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_finger_mouse", False):
            root.removeHandler(handler)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    handler._finger_mouse = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    def excepthook(kind, value, tb):
        logging.getLogger("finger_mouse").critical("Uncaught exception", exc_info=(kind, value, tb))
        sys.__excepthook__(kind, value, tb)

    def thread_excepthook(args):
        logging.getLogger("finger_mouse").critical(
            "Uncaught exception in thread %s", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    return path


class Rate:
    """Events per second over a sliding window."""

    def __init__(self, window: float = 2.0) -> None:
        self.window = window
        self._times: list[float] = []

    def tick(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self._times.append(now)
        cutoff = now - self.window
        while self._times and self._times[0] < cutoff:
            self._times.pop(0)

    def per_second(self, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        recent = [t for t in self._times if t >= now - self.window]
        if len(recent) < 2:
            return 0.0
        return (len(recent) - 1) / max(recent[-1] - recent[0], 1e-6)


class Average:
    """Exponentially weighted average of a duration, in milliseconds."""

    def __init__(self, weight: float = 0.1) -> None:
        self.weight = weight
        self.value: Optional[float] = None
        self.worst = 0.0

    def add(self, ms: float) -> None:
        self.value = ms if self.value is None else self.value + self.weight * (ms - self.value)
        self.worst = max(self.worst * 0.995, ms)


@dataclass
class Heartbeat:
    """A stage's liveness: when it last did something, and whether it's mid-call."""
    name: str
    busy_since: Callable[[], Optional[float]]     # start time of a call in progress, or None
    idle_limit: float                              # seconds a call may take before it's a stall
    describe: str


class Watchdog(threading.Thread):
    """Checks heartbeats and logs each stall once, with how long it lasted."""

    def __init__(self, interval: float = 0.25) -> None:
        super().__init__(name="watchdog", daemon=True)
        self.interval = interval
        self._beats: dict[str, Heartbeat] = {}
        self._stalled: dict[str, float] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.current: Optional[str] = None   # the stall in progress, for the UI

    def watch(self, beat: Heartbeat) -> None:
        with self._lock:
            self._beats[beat.name] = beat

    def unwatch(self, name: str) -> None:
        with self._lock:
            self._beats.pop(name, None)
            self._stalled.pop(name, None)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            self.check()

    def check(self, now: Optional[float] = None) -> Optional[str]:
        now = time.monotonic() if now is None else now
        with self._lock:
            beats = list(self._beats.values())
        messages = []
        for beat in beats:
            try:
                since = beat.busy_since()
            except Exception:
                since = None
            stuck = since is not None and now - since > beat.idle_limit
            if stuck:
                if beat.name not in self._stalled:
                    self._stalled[beat.name] = since  # type: ignore[assignment]
                    log.warning("Stall: %s for %.1f s", beat.describe, now - since)  # type: ignore[operator]
                messages.append(f"{beat.describe} ({now - since:.1f} s)")  # type: ignore[operator]
            elif beat.name in self._stalled:
                started = self._stalled.pop(beat.name)
                log.warning("Recovered: %s after %.1f s", beat.describe, now - started)
        self.current = "; ".join(messages) or None
        return self.current
