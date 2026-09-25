"""Moving, clicking and scrolling the real system pointer.

Two layers:

* **Backends** talk to the operating system's own input APIs — SendInput on
  Windows, Quartz events on macOS, and PyAutoGUI/XTest on Linux (X11).
  Native calls matter for dragging: macOS only treats a held button plus
  movement as a drag if the movement arrives as "dragged" events, which a
  plain cursor warp is not.
* **PointerOutput** is a small thread that owns the backend. The tracking
  thread hands it commands (move, click, press, release, scroll) and never
  waits; commands run in order, and a burst of moves collapses into the
  latest one so the pointer never lags behind a backlog. Because it's its own
  thread, a busy window or a slow camera can't hold up the pointer, and it is
  the one place that guarantees the button is released on stop, error or
  exit.

Coordinates are native desktop coordinates for each OS (physical pixels on
Windows, points on macOS, X11 pixels on Linux).
"""

from __future__ import annotations

import atexit
import collections
import logging
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

Rect = tuple[int, int, int, int]  # left, top, width, height


class PointerBackend:
    """What every backend provides. Methods may raise; PointerOutput copes."""

    name = "none"

    def desktop_rect(self, mode: str) -> Rect:
        raise NotImplementedError

    def position(self) -> Optional[tuple[int, int]]:
        return None

    def move(self, x: int, y: int, button_down: bool) -> None:
        raise NotImplementedError

    def button(self, down: bool, x: int, y: int, click_count: int = 1) -> None:
        raise NotImplementedError

    def click(self, x: int, y: int) -> None:
        self.move(x, y, False)
        self.button(True, x, y)
        self.button(False, x, y)

    def wheel(self, notches: float) -> float:
        """Scroll by up to ``notches``; return how much was actually sent.
        Backends that only do whole steps send what they can and the rest
        carries over to the next call."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

class WindowsBackend(PointerBackend):
    """SendInput: the same path a real mouse driver's input takes."""

    name = "windows-sendinput"
    INPUT_MOUSE = 0
    MOVE, LEFTDOWN, LEFTUP, WHEEL = 0x0001, 0x0002, 0x0004, 0x0800
    VIRTUALDESK, ABSOLUTE = 0x4000, 0x8000
    WHEEL_DELTA = 120
    WHEEL_STEP = 30   # send quarter-notches: smooth, and every app understands it
    MARKER = 0x464D   # "FM" in dwExtraInfo, so our own input is recognisable

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32 = user32

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class _INPUTUNION(ctypes.Union):
            # KEYBDINPUT/HARDWAREINPUT are smaller than MOUSEINPUT on every
            # architecture, so this union has the size SendInput expects.
            _fields_ = [("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        self._MOUSEINPUT, self._INPUT = MOUSEINPUT, INPUT
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
        user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        self._point = wintypes.POINT
        # Physical pixels, not scaled ones. Qt normally sets this already; if
        # so the call fails harmlessly.
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except (AttributeError, OSError):
            pass
        self._wheel_carry = 0.0

    def desktop_rect(self, mode: str) -> Rect:
        m = self._user32.GetSystemMetrics
        if mode == "all":
            return (m(76), m(77), m(78), m(79))   # SM_[XY]VIRTUALSCREEN, SM_C[XY]VIRTUALSCREEN
        return (0, 0, m(0), m(1))                  # primary monitor: SM_C[XY]SCREEN

    def position(self) -> Optional[tuple[int, int]]:
        p = self._point()
        if self._user32.GetCursorPos(self._ct.byref(p)):
            return (p.x, p.y)
        return None

    def _absolute(self, x: int, y: int) -> tuple[int, int]:
        # SendInput's absolute units are 0–65535 across the virtual desktop,
        # and a unit u lands on pixel floor(u * width / 65536). The ceiling
        # below is the smallest u that lands exactly on pixel x.
        left, top, width, height = self.desktop_rect("all")
        ux = math.ceil((x - left) * 65536 / max(width, 1))
        uy = math.ceil((y - top) * 65536 / max(height, 1))
        return max(0, min(65535, ux)), max(0, min(65535, uy))

    def _mouse(self, flags: int, x: int = 0, y: int = 0, data: int = 0):
        inp = self._INPUT(type=self.INPUT_MOUSE)
        inp.u.mi = self._MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, self.MARKER)
        return inp

    def _send(self, *inputs) -> None:
        array = (self._INPUT * len(inputs))(*inputs)
        sent = self._user32.SendInput(len(inputs), array, self._ct.sizeof(self._INPUT))
        if sent != len(inputs):
            err = self._ct.get_last_error()
            # 5 = access denied: the pointer is over a window running as
            # administrator (UIPI). Windows won't let normal apps send it input.
            raise OSError(err, "Windows blocked mouse input" + (
                " (a window running as administrator is in front)" if err == 5 else ""))

    def _move_input(self, x: int, y: int):
        ax, ay = self._absolute(x, y)
        return self._mouse(self.MOVE | self.ABSOLUTE | self.VIRTUALDESK, ax, ay)

    def move(self, x: int, y: int, button_down: bool) -> None:
        self._send(self._move_input(x, y))

    def button(self, down: bool, x: int, y: int, click_count: int = 1) -> None:
        # Move and press in one SendInput call so nothing can land in between.
        self._send(self._move_input(x, y), self._mouse(self.LEFTDOWN if down else self.LEFTUP))

    def click(self, x: int, y: int) -> None:
        self._send(self._move_input(x, y), self._mouse(self.LEFTDOWN), self._mouse(self.LEFTUP))

    def wheel(self, notches: float) -> float:
        self._wheel_carry += notches * self.WHEEL_DELTA
        steps = int(self._wheel_carry / self.WHEEL_STEP)
        if not steps:
            return 0.0
        delta = steps * self.WHEEL_STEP
        self._wheel_carry -= delta
        self._send(self._mouse(self.WHEEL, data=delta))
        return delta / self.WHEEL_DELTA


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

class MacBackend(PointerBackend):
    """Quartz event services. Needs the Accessibility permission."""

    name = "macos-quartz"
    PIXELS_PER_NOTCH = 40
    DOUBLE_CLICK_SECONDS = 0.5
    DOUBLE_CLICK_DISTANCE = 6

    def __init__(self) -> None:
        import Quartz  # pyobjc-framework-Quartz

        self.Q = Quartz
        self._wheel_carry = 0.0
        self._last_click = (0.0, 0, 0)
        self._click_count = 0
        try:
            from AppKit import NSEvent  # pyobjc-framework-Cocoa
            self.DOUBLE_CLICK_SECONDS = float(NSEvent.doubleClickInterval())
        except Exception:
            pass

    def desktop_rect(self, mode: str) -> Rect:
        Q = self.Q
        if mode == "all":
            err, ids, count = Q.CGGetActiveDisplayList(16, None, None)
            if not err and count:
                rects = [Q.CGDisplayBounds(i) for i in ids[:count]]
                left = min(r.origin.x for r in rects)
                top = min(r.origin.y for r in rects)
                right = max(r.origin.x + r.size.width for r in rects)
                bottom = max(r.origin.y + r.size.height for r in rects)
                return (int(left), int(top), int(right - left), int(bottom - top))
        r = Q.CGDisplayBounds(Q.CGMainDisplayID())
        return (int(r.origin.x), int(r.origin.y), int(r.size.width), int(r.size.height))

    def position(self) -> Optional[tuple[int, int]]:
        loc = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        return (int(loc.x), int(loc.y))

    def _post(self, kind: int, x: int, y: int, click_count: int = 0) -> None:
        Q = self.Q
        event = Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft)
        if event is None:
            raise OSError("macOS refused to create a mouse event (check Accessibility permission)")
        if click_count:
            Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventClickState, click_count)
        Q.CGEventPost(Q.kCGHIDEventTap, event)

    def move(self, x: int, y: int, button_down: bool) -> None:
        Q = self.Q
        self._post(Q.kCGEventLeftMouseDragged if button_down else Q.kCGEventMouseMoved, x, y)

    def _count_for(self, x: int, y: int) -> int:
        # macOS apps recognise a double-click from the click-state field, so
        # two quick pinches in one spot must say "2", not "1" twice.
        t, lx, ly = self._last_click
        now = time.monotonic()
        near = abs(x - lx) <= self.DOUBLE_CLICK_DISTANCE and abs(y - ly) <= self.DOUBLE_CLICK_DISTANCE
        self._click_count = self._click_count + 1 if (now - t <= self.DOUBLE_CLICK_SECONDS and near) else 1
        self._last_click = (now, x, y)
        return self._click_count

    def button(self, down: bool, x: int, y: int, click_count: int = 1) -> None:
        Q = self.Q
        count = self._count_for(x, y) if down else max(1, self._click_count)
        self._post(Q.kCGEventLeftMouseDown if down else Q.kCGEventLeftMouseUp, x, y, count)

    def wheel(self, notches: float) -> float:
        Q = self.Q
        self._wheel_carry += notches * self.PIXELS_PER_NOTCH
        pixels = int(self._wheel_carry)
        if not pixels:
            return 0.0
        self._wheel_carry -= pixels
        event = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitPixel, 1, pixels)
        Q.CGEventPost(Q.kCGHIDEventTap, event)
        return pixels / self.PIXELS_PER_NOTCH


# ---------------------------------------------------------------------------
# Linux (X11) and anything else
# ---------------------------------------------------------------------------

class PyAutoGUIBackend(PointerBackend):
    """PyAutoGUI — XTest on Linux. Wayland sessions generally refuse this."""

    name = "pyautogui"

    def __init__(self) -> None:
        import pyautogui

        pyautogui.FAILSAFE = False   # our own safety: physical-mouse override + Esc/Stop
        pyautogui.PAUSE = 0
        self.p = pyautogui
        self._wheel_carry = 0.0

    def desktop_rect(self, mode: str) -> Rect:
        w, h = self.p.size()
        return (0, 0, int(w), int(h))

    def position(self) -> Optional[tuple[int, int]]:
        pos = self.p.position()
        return (int(pos.x), int(pos.y))

    def move(self, x: int, y: int, button_down: bool) -> None:
        self.p.moveTo(x, y, _pause=False)

    def button(self, down: bool, x: int, y: int, click_count: int = 1) -> None:
        (self.p.mouseDown if down else self.p.mouseUp)(x=x, y=y, button="left", _pause=False)

    def wheel(self, notches: float) -> float:
        self._wheel_carry += notches
        clicks = int(self._wheel_carry)
        if not clicks:
            return 0.0
        self._wheel_carry -= clicks
        self.p.scroll(clicks, _pause=False)
        return float(clicks)


def create_backend() -> PointerBackend:
    """The best backend for this OS, falling back to PyAutoGUI."""
    try:
        if sys.platform == "win32":
            return WindowsBackend()
        if sys.platform == "darwin":
            return MacBackend()
    except Exception:
        log.exception("Native pointer backend unavailable; using PyAutoGUI")
    return PyAutoGUIBackend()


# ---------------------------------------------------------------------------
# The output thread
# ---------------------------------------------------------------------------

@dataclass
class _Command:
    kind: str                # move, click, press, release, scroll, release_all
    x: int = 0
    y: int = 0
    amount: float = 0.0


class PointerOutput(threading.Thread):
    """Performs pointer commands in order on its own thread.

    ``on_event(kind, message)`` is called (from this thread) for things the
    UI should know: "error" (one command failed, e.g. Windows refusing input
    to an administrator window), "refused" (the desktop ignores us entirely:
    no Accessibility permission on macOS, or a Wayland session on Linux),
    "override" (the user moved the real mouse), "resumed", and "released" (a
    held button was let go by cleanup).
    """

    OVERRIDE_DISTANCE = 40     # px the real pointer must be off ours to count as the user
    OVERRIDE_PAUSE = 1.5       # s hand control stays paused after that

    def __init__(self, backend: PointerBackend, on_event: Optional[Callable[[str, str], None]] = None,
                 pause_on_physical_mouse: bool = True) -> None:
        super().__init__(name="pointer-output", daemon=True)
        self.backend = backend
        self.on_event = on_event or (lambda kind, message: None)
        self.pause_on_physical_mouse = pause_on_physical_mouse
        self._queue: collections.deque[_Command] = collections.deque()
        self._cond = threading.Condition()
        self._halt = False
        self._button_down = False
        self._last_set: Optional[tuple[int, int]] = None
        self._paused_until = 0.0
        self.last_command_at = time.monotonic()
        self.busy_since: Optional[float] = None   # for the freeze watchdog
        self.errors = 0
        self._verified = False     # has the pointer been seen to go where we sent it?
        self._verify_misses = 0
        atexit.register(self._atexit_release)

    # -- called from any thread ---------------------------------------------
    @property
    def button_down(self) -> bool:
        return self._button_down

    @property
    def paused(self) -> bool:
        return time.monotonic() < self._paused_until

    def queue_depth(self) -> int:
        return len(self._queue)

    def move(self, x: int, y: int) -> None:
        self._put(_Command("move", x, y), coalesce=True)

    def click(self, x: int, y: int) -> None:
        self._put(_Command("click", x, y))

    def press(self, x: int, y: int) -> None:
        self._put(_Command("press", x, y))

    def release(self) -> None:
        self._put(_Command("release"))

    def scroll(self, notches: float) -> None:
        if notches:
            self._put(_Command("scroll", amount=notches))

    def release_all(self) -> None:
        """Drop anything queued and make sure the button is up."""
        with self._cond:
            self._queue.clear()
            self._queue.append(_Command("release_all"))
            self._cond.notify()

    def stop(self, timeout: float = 2.0) -> None:
        self.release_all()
        with self._cond:
            self._halt = True
            self._cond.notify()
        if self.is_alive():
            self.join(timeout)
        self._atexit_release()

    # -- internals ----------------------------------------------------------
    def _put(self, command: _Command, coalesce: bool = False) -> None:
        with self._cond:
            if coalesce and self._queue and self._queue[-1].kind == "move":
                self._queue[-1] = command   # only the newest position matters
            else:
                self._queue.append(command)
            self._cond.notify()

    def run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._halt:
                    self._cond.wait(0.5)
                if self._halt and not self._queue:
                    break
                command = self._queue.popleft()
            self.busy_since = time.monotonic()
            try:
                self._perform(command)
            except Exception as exc:
                self.errors += 1
                log.exception("Pointer command %s failed", command.kind)
                self.on_event("error", str(exc))
                # Whatever failed, don't leave the button held.
                self._force_release()
            finally:
                self.busy_since = None
                self.last_command_at = time.monotonic()
        self._force_release()

    def _user_moved_mouse(self) -> bool:
        """True if the real pointer isn't where we last put it: someone is
        using a physical mouse or trackpad, so hand control steps aside."""
        if not self.pause_on_physical_mouse or self._last_set is None:
            return False
        try:
            pos = self.backend.position()
        except Exception:
            return False
        if pos is None:
            return False
        return max(abs(pos[0] - self._last_set[0]), abs(pos[1] - self._last_set[1])) > self.OVERRIDE_DISTANCE

    def _perform(self, c: _Command) -> None:
        b = self.backend
        if c.kind == "release_all":
            self._force_release()
            return
        if c.kind == "release":
            if self._button_down:
                pos = self._last_set or (b.position() or (0, 0))
                b.button(False, *pos)
                self._button_down = False
            return

        now = time.monotonic()
        if not self._button_down and self._user_moved_mouse():
            was_paused = self.paused
            self._paused_until = now + self.OVERRIDE_PAUSE
            self._last_set = None
            if not was_paused:
                self.on_event("override", "You moved the mouse — hand control paused for a moment.")
        if self.paused and not self._button_down:
            return   # the person's own mouse wins; clicks and moves are dropped
        if self._paused_until and not self.paused:
            self._paused_until = 0.0
            self.on_event("resumed", "")

        if c.kind == "move":
            b.move(c.x, c.y, self._button_down)
            self._last_set = (c.x, c.y)
            if not self._verified:
                self._verify(c.x, c.y)
        elif c.kind == "click":
            b.click(c.x, c.y)
            self._last_set = (c.x, c.y)
        elif c.kind == "press":
            b.move(c.x, c.y, False)
            b.button(True, c.x, c.y)
            self._button_down = True
            self._last_set = (c.x, c.y)
        elif c.kind == "scroll":
            b.wheel(c.amount)

    def _verify(self, x: int, y: int) -> None:
        """The first moves are checked: some desktops accept the call and
        silently ignore it, which would otherwise look like a frozen pointer."""
        try:
            pos = self.backend.position()
        except Exception:
            pos = None
        if pos is None or (abs(pos[0] - x) <= 3 and abs(pos[1] - y) <= 3):
            self._verified = True
            return
        self._verify_misses += 1
        self._last_set = None          # don't mistake this for the user's own mouse
        if self._verify_misses >= 3:
            self._verified = True
            if sys.platform == "darwin":
                why = ("macOS isn't letting Finger Mouse move the pointer. Allow it under System Settings → "
                       "Privacy & Security → Accessibility, then quit and reopen it.")
            elif sys.platform.startswith("linux"):
                why = ("This desktop isn't letting apps move the pointer. Wayland sessions block it; "
                       "log in with an X11 session (e.g. “Ubuntu on Xorg”).")
            else:
                why = "The desktop didn't move the pointer where Finger Mouse sent it."
            self.on_event("refused", why)

    def _force_release(self) -> None:
        if not self._button_down:
            return
        try:
            pos = self._last_set or self.backend.position() or (0, 0)
            self.backend.button(False, *pos)
            self.on_event("released", "")
        except Exception:
            log.exception("Could not release the mouse button")
        finally:
            self._button_down = False

    def _atexit_release(self) -> None:
        # Last line of defence if the app is closing with the button held.
        if self._button_down:
            self._force_release()
