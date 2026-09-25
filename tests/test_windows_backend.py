"""The Windows SendInput backend, checked against what Windows actually receives.

A low-level mouse hook records every injected event. Moves are let through
(so button presses really happen at the pointer's new position, and the
test can check that) and the pointer is put back afterwards; every button
and wheel event is *swallowed*, so nothing on the desktop is ever clicked or
scrolled. The hook is proven live with a harmless move before any button is
sent; if it isn't, the test stops rather than click for real.
"""

import os
import sys

import pytest

# Windows only, and skipped before importing ctypes.wintypes, which fails to
# import anywhere else. CI runners have no interactive desktop to inject into.
if sys.platform != "win32":
    pytest.skip("Windows input API", allow_module_level=True)
if os.environ.get("CI"):
    pytest.skip("needs an interactive Windows desktop", allow_module_level=True)

import ctypes  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from ctypes import wintypes  # noqa: E402

WH_MOUSE_LL = 14
WM_QUIT = 0x0012
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEWHEEL = 0x0200, 0x0201, 0x0202, 0x020A
LLMHF_INJECTED = 0x01


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class SwallowingHook:
    """Records injected mouse input and swallows everything but moves."""

    def __init__(self):
        self.events = []
        self.ready = threading.Event()
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        self.user32.CallNextHookEx.restype = ctypes.c_ssize_t
        self.user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
        self.user32.SetWindowsHookExW.restype = wintypes.HHOOK
        self._proc = HOOKPROC(self._callback)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread_id = None
        self.hook = None

    def _callback(self, code, wparam, lparam):
        if code == 0:
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if info.flags & LLMHF_INJECTED:
                self.events.append((wparam, info.pt.x, info.pt.y, info.mouseData, info.dwExtraInfo))
                if wparam != WM_MOUSEMOVE:
                    return 1   # swallow: the click or scroll never reaches the desktop
        return self.user32.CallNextHookEx(None, code, wparam, lparam)

    def _loop(self):
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self.hook = self.user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
        self.ready.set()
        msg = wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        self.user32.UnhookWindowsHookEx(self.hook)

    def __enter__(self):
        self._thread.start()
        assert self.ready.wait(5), "hook thread didn't start"
        assert self.hook, f"SetWindowsHookEx failed ({ctypes.get_last_error()})"
        return self

    def __exit__(self, *exc):
        self.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(5)

    def wait_for(self, count, timeout=2.0):
        end = time.monotonic() + timeout
        while len(self.events) < count and time.monotonic() < end:
            time.sleep(0.01)
        return self.events[:count]


def test_sendinput_lands_on_exact_pixels_and_sends_real_buttons():
    from pointer_output import WindowsBackend

    backend = WindowsBackend()
    cursor_before = backend.position()
    left, top, width, height = backend.desktop_rect("primary")
    assert width > 0 and height > 0

    try:
        _run_checks(backend, width, height)
    finally:
        ctypes.windll.user32.SetCursorPos(*cursor_before)   # put the pointer back


def _run_checks(backend, width, height):
    from pointer_output import WindowsBackend

    with SwallowingHook() as hook:
        # 1. Prove the hook is catching our input before anything can click.
        backend.move(width // 2, height // 2, False)
        first = hook.wait_for(1)
        assert first and first[0][0] == WM_MOUSEMOVE, "hook not live; refusing to send clicks"
        assert first[0][4] == WindowsBackend.MARKER

        # 2. Moves land exactly, corners and edges included.
        targets = [(0, 0), (width - 1, height - 1), (width // 3, height // 5), (123, 457)]
        for x, y in targets:
            backend.move(x, y, False)
        moves = hook.wait_for(1 + len(targets))[1:]
        assert [(e[1], e[2]) for e in moves] == targets

        # 3. A click is move + down + up at the same point, in one burst. A
        # press lands wherever the pointer is at that instant, so a person
        # moving their real mouse mid-test can shift it; allow a retry or two.
        for _attempt in range(3):
            hook.events.clear()
            backend.click(200, 300)
            click = hook.wait_for(3)
            assert [e[0] for e in click] == [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP]
            if all((e[1], e[2]) == (200, 300) for e in click):
                break
        assert all((e[1], e[2]) == (200, 300) for e in click)

        # 4. Press and release for dragging.
        hook.events.clear()
        backend.button(True, 50, 60)
        backend.move(90, 100, True)
        backend.button(False, 90, 100)
        drag = hook.wait_for(5)
        assert [e[0] for e in drag] == [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_MOUSEMOVE, WM_MOUSEMOVE, WM_LBUTTONUP]
        assert (drag[1][1], drag[1][2]) == (50, 60)     # pressed where it was sent
        assert (drag[4][1], drag[4][2]) == (90, 100)    # released where it was dragged to

        # 5. Wheel: fractions carry over; whole quarter-notches are sent.
        hook.events.clear()
        assert backend.wheel(0.1) == 0.0            # 12 units: below one quarter-notch
        assert backend.wheel(0.2) == pytest.approx(0.25)   # 36 carried -> one 30-unit step
        assert backend.wheel(-1.0) == pytest.approx(-0.75)  # 6 carried - 120 = -114 -> three steps
        wheel = hook.wait_for(2)
        deltas = [ctypes.c_short(e[3] >> 16).value for e in wheel]
        assert [e[0] for e in wheel] == [WM_MOUSEWHEEL, WM_MOUSEWHEEL]
        assert deltas == [30, -90]

        # The pointer really is where the last move put it.
        assert backend.position() == (90, 100)
