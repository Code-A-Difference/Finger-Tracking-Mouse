"""The pointer output thread: order, coalescing, and never leaving a button held."""

import threading
import time

import pytest

from pointer_output import PointerBackend, PointerOutput


class Recorder(PointerBackend):
    name = "recorder"

    def __init__(self, fail_on=None, slow=0.0):
        self.calls = []
        self.pos = (0, 0)
        self.fail_on = fail_on
        self.slow = slow
        self.lock = threading.Lock()

    def desktop_rect(self, mode):
        return (0, 0, 1000, 800)

    def position(self):
        return self.pos

    def _log(self, *entry):
        if self.slow:
            time.sleep(self.slow)
        if self.fail_on and entry[0] == self.fail_on:
            raise OSError("simulated OS refusal")
        with self.lock:
            self.calls.append(entry)

    def move(self, x, y, button_down):
        self._log("move", x, y, button_down)
        self.pos = (x, y)

    def button(self, down, x, y, click_count=1):
        self._log("down" if down else "up", x, y)

    def wheel(self, notches):
        self._log("wheel", notches)
        return notches


def settle(out, timeout=2.0):
    end = time.monotonic() + timeout
    while (out.queue_depth() or out.busy_since) and time.monotonic() < end:
        time.sleep(0.005)
    time.sleep(0.02)


@pytest.fixture
def output():
    outs = []

    def make(backend, **kw):
        out = PointerOutput(backend, **kw)
        out.start()
        outs.append(out)
        return out

    yield make
    for out in outs:
        out.stop()


def test_commands_run_in_order_and_click_lands_on_its_point(output):
    b = Recorder()
    out = output(b)
    out.move(10, 10)
    out.click(50, 60)
    out.press(70, 80)
    out.move(90, 95)
    out.release()
    settle(out)
    kinds = [c[0] for c in b.calls]
    assert kinds == ["move", "move", "down", "up", "move", "down", "move", "up"]
    assert b.calls[1][1:3] == (50, 60) and b.calls[3][1:] == (50, 60)
    assert b.calls[6] == ("move", 90, 95, True)          # moves while held are drags
    assert not out.button_down


def test_a_burst_of_moves_collapses_to_the_newest(output):
    b = Recorder(slow=0.02)
    out = output(b)
    for i in range(50):
        out.move(i, i)
    settle(out)
    moves = [c for c in b.calls if c[0] == "move"]
    assert moves[-1][1:3] == (49, 49)
    assert len(moves) < 10                               # no backlog to work through


def test_stop_releases_a_held_button(output):
    b = Recorder()
    out = output(b)
    out.press(5, 5)
    settle(out)
    assert out.button_down
    out.stop()
    assert b.calls[-1][0] == "up"
    assert not out.button_down


def test_release_all_skips_queued_work_and_lifts_the_button(output):
    b = Recorder(slow=0.01)
    out = output(b)
    out.press(5, 5)
    settle(out)
    assert out.button_down
    for i in range(20):
        out.click(i, i)
    out.release_all()
    settle(out)
    assert b.calls[-1][0] == "up"
    assert not out.button_down
    assert sum(1 for c in b.calls if c[0] == "down") < 5   # the queued clicks were dropped


def test_an_os_error_is_reported_and_still_releases(output):
    events = []
    b = Recorder(fail_on="move")
    out = output(b, on_event=lambda kind, msg: events.append(kind))
    out._button_down = True        # as if a drag were in progress
    out.move(1, 1)
    settle(out)
    assert "error" in events
    assert not out.button_down
    assert b.calls[-1][0] == "up"


def test_moving_the_real_mouse_pauses_hand_control(output):
    events = []
    b = Recorder()
    out = output(b, on_event=lambda kind, msg: events.append(kind))
    out.move(100, 100)
    settle(out)
    b.pos = (400, 300)             # the person grabbed their mouse
    out.move(110, 105)
    out.click(110, 105)
    settle(out)
    assert "override" in events
    assert out.paused
    assert [c[0] for c in b.calls].count("down") == 0     # no click while they're in charge
    out.OVERRIDE_PAUSE = 0.05
    out._paused_until = time.monotonic() + 0.05
    time.sleep(0.1)
    out.move(120, 110)
    settle(out)
    assert b.calls[-1][:3] == ("move", 120, 110)


def test_override_can_be_turned_off(output):
    b = Recorder()
    out = output(b, pause_on_physical_mouse=False)
    out.move(100, 100)
    settle(out)
    b.pos = (400, 300)
    out.click(110, 105)
    settle(out)
    assert [c[0] for c in b.calls].count("down") == 1


def test_scroll_passes_through(output):
    b = Recorder()
    out = output(b)
    out.scroll(0.5)
    out.scroll(0)                  # zero is ignored
    out.scroll(-1.25)
    settle(out)
    assert [c for c in b.calls if c[0] == "wheel"] == [("wheel", 0.5), ("wheel", -1.25)]
