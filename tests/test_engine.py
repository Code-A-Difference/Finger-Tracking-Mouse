"""The whole tracking loop with a scripted camera and hand, and a recording mouse.

Each scenario is a list of frames: a hand (landmarks), ``None`` for a frame
with no hand, ``GAP`` for no frame at all (a stalled camera), ``BOOM`` for a
frame the hand tracker throws on, or ``(hand, age)`` for a frame that was
already ``age`` seconds old when it arrived. The engine, the gesture code
and the real PointerOutput thread all run for real; only the camera, the
model and the OS mouse are stand-ins.
"""

import time

import numpy as np
import pytest

from app_settings import Settings
from camera import CameraCapabilities, Frame
from handgen import index_tip_at, make_hand
from pointer_output import PointerOutput
from test_pointer_output import Recorder, settle
from tracking_engine import TrackingEngine

GAP, BOOM = "gap", "boom"
FPS = 30


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Script:
    """Shared by the fake camera (which advances it) and the fake model."""

    def __init__(self, frames, clock):
        self.frames = list(frames)
        self.clock = clock
        self.current = None
        self.engine = None
        self.before_frame = None   # optional hook(frames_left)
        self.output = None


def make_fakes(script):
    grabbers = []
    trackers = []

    class FakeTracker:
        def __init__(self):
            trackers.append(self)

        def process(self, rgb):
            if script.current == BOOM:
                raise RuntimeError("inference failed")
            return script.current

        def close(self):
            pass

    class FakeGrabber:
        def __init__(self, source, on_status):
            self.on_status = on_status
            self.capabilities = CameraCapabilities(640, 360, 30.0, "fake")
            self.frames = self.dropped = 0
            self.connected = False
            self.stopped = False
            self.last = None
            self.requests = []
            grabbers.append(self)

        def start(self):
            self.connected = True
            self.on_status("connected", self.capabilities)

        def stop(self):
            self.stopped = True

        def is_alive(self):
            return not self.stopped

        def request(self, command, value=None):
            self.requests.append((command, value))

        def latest_age(self):
            return None if self.last is None else script.clock.now - self.last

        def wait_frame(self, after_seq, timeout):
            if not script.frames:
                # Let the pointer thread finish what was decided before
                # stopping: stopping deliberately drops queued commands.
                if script.output is not None:
                    settle(script.output)
                script.engine.stop()
                return None
            if script.before_frame:
                script.before_frame(len(script.frames))
            item = script.frames.pop(0)
            if item == GAP:
                script.clock.now += timeout
                return None
            script.clock.now += 1 / FPS
            age = 0.0
            if isinstance(item, tuple):
                item, age = item
            script.current = item
            self.last = script.clock.now - age
            self.frames += 1
            return Frame(np.zeros((360, 640, 3), np.uint8), self.last, after_seq + 1)

    return FakeGrabber, FakeTracker, grabbers, trackers


def run(frames, settings=None, **kw):
    clock = Clock()
    script = Script(frames, clock)
    FakeGrabber, FakeTracker, grabbers, trackers = make_fakes(script)
    backend = Recorder()
    output = PointerOutput(backend)
    output.start()
    engine = TrackingEngine(settings or Settings(), output, [], tracker_factory=FakeTracker,
                            grabber_factory=FakeGrabber, clock=clock)
    script.engine = engine
    script.output = output
    engine.start()
    engine.join(10)
    assert not engine.is_alive(), "engine didn't stop"
    settle(output)
    output.stop()
    events = []
    while not engine.events.empty():
        events.append(engine.events.get())
    return backend.calls, output, events, grabbers, trackers


def hand(x=0.5, y=0.5, pinch=0.9, extended=("index", "middle", "ring", "pinky")):
    return index_tip_at(make_hand(extended, pinch=pinch), x, y)


def of(calls, kind):
    return [c for c in calls if c[0] == kind]


def screen(x, y):
    """Where a fingertip at frame (x, y) lands with default reach on a 1000×800 desktop."""
    nx, ny = (x - 0.1) / 0.8, (y - 0.1) / 0.8
    return nx * 999, ny * 799


# ---------------------------------------------------------------------------

def test_quick_pinch_clicks_where_the_pinch_began_not_where_the_hand_drifted():
    frames = [hand() for _ in range(15)]                                   # settle, arm
    frames += [hand(pinch=0.30), hand(pinch=0.15)]                         # crossing (ratio ≈ 0.17)
    frames += [hand(0.53 + 0.01 * i, 0.53, pinch=0.1) for i in range(4)]   # pinching drags the tip
    frames += [hand(0.58, 0.56) for _ in range(5)]                         # release, moved away
    calls, output, events, *_ = run(frames)
    downs, ups = of(calls, "down"), of(calls, "up")
    assert len(downs) == 1 and len(ups) == 1                               # exactly one click
    x, y = downs[0][1:]
    ex, ey = screen(0.5, 0.5)
    assert abs(x - ex) <= 3 and abs(y - ey) <= 3                           # at the pinch start…
    drift_x, _ = screen(0.56, 0.53)
    assert abs(x - drift_x) > 40                                           # …not where it drifted
    assert ("gesture", "click") in events
    assert not output.button_down


def test_pinch_and_hold_drags_then_drops():
    frames = [hand() for _ in range(15)]
    frames += [hand(pinch=0.1) for _ in range(16)]                         # held 0.53 s > 0.42 s
    frames += [hand(0.5 + 0.01 * i, 0.5, pinch=0.1) for i in range(1, 11)] # drag right
    frames += [hand(0.6, 0.5) for _ in range(5)]                           # open: drop
    calls, output, events, *_ = run(frames)
    kinds = [c[0] for c in calls]
    first_down = kinds.index("down")
    assert kinds.count("down") == 1 and kinds.count("up") == 1
    assert kinds.index("up") > first_down
    drag_moves = [c for c in calls[first_down:] if c[0] == "move" and c[3]]
    assert drag_moves, "movement while held must be sent as dragging"
    assert drag_moves[-1][1] > calls[first_down][1] + 50                   # it really moved
    assert ("gesture", "mouse_down") in events and ("gesture", "mouse_up") in events
    assert not output.button_down


def test_losing_the_hand_mid_drag_releases_the_button():
    frames = [hand() for _ in range(15)] + [hand(pinch=0.1) for _ in range(20)] + [None] * 15
    calls, output, *_ = run(frames)
    kinds = [c[0] for c in calls]
    assert kinds.count("down") == 1
    assert kinds[-1] == "up"
    assert not output.button_down


def test_a_stalled_camera_releases_the_mouse_and_restarts_capture():
    frames = [hand() for _ in range(15)] + [hand(pinch=0.1) for _ in range(20)]
    frames += [GAP] * 16                                                   # 3.2 s with no frames
    frames += [hand() for _ in range(5)]
    calls, output, events, grabbers, _ = run(frames)
    kinds = [c[0] for c in calls]
    assert kinds.count("down") == 1 and "up" in kinds[kinds.index("down"):]
    assert len(grabbers) >= 2, "the camera should have been restarted"
    assert grabbers[0].stopped
    assert any(e[0] == "camera" and e[1][0] == "reconnecting" for e in events)


def test_inference_errors_rebuild_the_tracker_and_tracking_continues():
    frames = [hand() for _ in range(5)] + [BOOM] * 3 + [hand(0.3, 0.3) for _ in range(10)]
    calls, output, events, _, trackers = run(frames)
    assert len(trackers) == 2
    moves = of(calls, "move")
    ex, ey = screen(0.3, 0.3)
    assert abs(moves[-1][1] - ex) < 30 and abs(moves[-1][2] - ey) < 30     # still following the hand
    assert not any(e[0] == "fatal" for e in events)


def test_stale_results_never_click():
    frames = [(hand(), 1.0) for _ in range(15)]
    frames += [(hand(pinch=0.1), 1.0) for _ in range(3)] + [(hand(), 1.0) for _ in range(5)]
    calls, *_ = run(frames)
    assert of(calls, "down") == []
    assert of(calls, "move") == []                                         # nor move to old positions


def test_two_finger_scroll_scrolls_without_clicking_or_moving():
    scroll = ("index", "middle")
    frames = [hand(0.5, 0.5, extended=scroll) for _ in range(12)]          # hold the pose: enters
    frames += [hand(0.5, 0.5 - 0.01 * i, extended=scroll) for i in range(1, 16)]   # move up
    calls, output, *_ = run(frames)
    wheel = of(calls, "wheel")
    assert wheel and sum(c[1] for c in wheel) > 0                          # up = scroll up
    assert of(calls, "down") == []
    moves_after_entry = [c for c in calls[calls.index(wheel[0]):] if c[0] == "move"]
    assert moves_after_entry == []                                         # the pointer held still


def test_pointing_hand_moves_the_pointer_and_never_scrolls():
    frames = [hand(0.5 - 0.005 * i, 0.5 - 0.01 * i, extended=("index",)) for i in range(30)]
    calls, *_ = run(frames)
    assert of(calls, "wheel") == []
    assert len(of(calls, "move")) > 10


def test_switching_camera_releases_first():
    frames = [hand() for _ in range(15)] + [hand(pinch=0.1) for _ in range(20)]
    frames += [hand(pinch=0.1) for _ in range(5)]
    clock = Clock()
    script = Script(frames, clock)
    FakeGrabber, FakeTracker, grabbers, _ = make_fakes(script)
    backend = Recorder()
    output = PointerOutput(backend)
    output.start()
    engine = TrackingEngine(Settings(), output, [], tracker_factory=FakeTracker,
                            grabber_factory=FakeGrabber, clock=clock)
    script.engine = engine
    script.output = output

    def switch_mid_drag(frames_left):
        if frames_left == 5:
            engine.update_settings(Settings(camera="cv:3"))

    script.before_frame = switch_mid_drag
    engine.start()
    engine.join(10)
    settle(output)
    output.stop()
    kinds = [c[0] for c in backend.calls]
    assert kinds.count("down") == 1 and "up" in kinds[kinds.index("down"):]
    assert len(grabbers) == 2 and grabbers[0].stopped


def test_hide_gesture_only_when_enabled():
    hide = [hand(extended=("middle",)) for _ in range(60)]                  # 2 s of the pose
    _, _, events, *_ = run(hide)
    assert not any(e[0] == "hide_requested" for e in events)
    on = Settings(hide_gesture_enabled=True, hide_gesture_confirmed=True, hide_gesture_action="minimize")
    _, output, events, *_ = run([hand(extended=("middle",)) for _ in range(60)], settings=on)
    assert ("hide_requested", "minimize") in events
    assert not output.button_down
