"""Gesture state machines, frame by frame (30 fps unless a test says otherwise)."""

import math

import pytest

from gesture_state import HeldPose, OneEuroFilter, PinchGesture, PointerFilter, PointerStabilizer, ScrollGesture

DT = 1 / 30
OPEN, CLOSED, BETWEEN = 0.7, 0.2, 0.36   # threshold 0.30, release 0.44


def names(actions):
    return [a.name for a in actions]


class Driver:
    """Feeds a PinchGesture one frame at a time and collects everything it does."""

    def __init__(self, **kw):
        kw.setdefault("confirm_frames", 2)
        self.g = PinchGesture(0.30, 0.4, release_threshold=0.44, **kw)
        self.t = 0.0
        self.log = []

    def frame(self, ratio, pos=(100, 100), dt=DT):
        self.t += dt
        acts = self.g.update(ratio, pos, self.t)
        self.log += acts
        return acts

    def frames(self, n, ratio, pos=(100, 100)):
        out = []
        for _ in range(n):
            out += self.frame(ratio, pos)
        return out

    def arm(self):
        self.frames(3, OPEN)
        assert self.g.state == "ready"


# ---- clicking --------------------------------------------------------------

def test_quick_pinch_clicks_once_at_the_exact_crossing_position():
    d = Driver()
    d.arm()
    d.frame(0.5, (100, 100))                   # closing, still above threshold
    d.frame(0.29, (103, 101))                  # crosses here: this is the lock
    d.frame(0.2, (110, 108))                   # the pinch motion drags the fingertip…
    d.frames(3, 0.2, (118, 115))
    acts = d.frames(3, OPEN, (140, 150))       # …and releasing moves it further
    clicks = [a for a in d.log if a.name == "click"]
    assert len(clicks) == 1
    assert (clicks[0].x, clicks[0].y) == (103, 101)
    assert names(d.log).count("pinch_started") == 1
    assert "mouse_down" not in names(d.log)
    assert d.g.state == "ready"                # open fingers re-arm straight away


def test_a_second_pinch_needs_the_fingers_opened_again():
    d = Driver()
    d.arm()
    d.frames(3, CLOSED)
    d.frames(3, OPEN)
    d.frames(3, CLOSED)
    d.frames(3, OPEN)
    assert names(d.log).count("click") == 2


def test_single_frame_noise_below_the_threshold_does_not_click():
    d = Driver()
    d.arm()
    for ratio in [0.25, OPEN, 0.28, OPEN, OPEN, 0.27, OPEN]:
        d.frame(ratio)
    assert "click" not in names(d.log)
    assert "pinch_started" not in names(d.log)


def test_jitter_between_the_thresholds_neither_clicks_nor_releases():
    d = Driver()
    d.arm()
    d.frames(2, CLOSED)                        # confirmed pinch
    d.frames(5, BETWEEN)                       # wobbling near the line
    assert "click" not in names(d.log)
    assert d.g.state == "pressed"


def test_a_half_pinch_that_never_settles_times_out_instead_of_freezing():
    d = Driver()
    d.arm()
    d.frame(0.29)                              # crossed once
    d.frames(12, BETWEEN)                      # then hovers above the line
    assert d.g.state == "ready"
    assert d.g.locked_position is None


def test_no_click_without_seeing_an_open_hand_first():
    d = Driver()
    d.frames(10, CLOSED)                       # hand arrives already pinched
    assert d.log == []
    assert d.g.state == "idle"


def test_a_fist_holds_still_rather_than_counting_as_a_pinch():
    d = Driver()
    d.arm()
    during = []
    for _ in range(10):
        d.t += DT
        during += d.g.update(0.25, (100, 100), d.t, hold_still=True)
    assert during == []
    assert d.g.state == "ready"


# ---- press and drag ---------------------------------------------------------

def test_hold_presses_at_the_lock_then_release_lifts_the_button():
    d = Driver()
    d.arm()
    d.frame(0.25, (200, 300))                  # lock
    d.frames(20, CLOSED, (260, 340))           # held for 0.67 s > 0.4 s
    downs = [a for a in d.log if a.name == "mouse_down"]
    assert len(downs) == 1 and (downs[0].x, downs[0].y) == (200, 300)
    assert d.g.state == "dragging"
    d.frames(3, OPEN)
    assert names(d.log)[-1] == "mouse_up"
    assert "click" not in names(d.log)          # a drag is not also a click


def test_hold_time_is_measured_from_the_crossing():
    d = Driver()
    d.arm()
    d.frame(0.25)
    d.frames(10, CLOSED)                       # 0.37 s: not yet
    assert "mouse_down" not in names(d.log)
    d.frames(2, CLOSED)                        # 0.43 s
    assert "mouse_down" in names(d.log)


def test_with_drag_off_a_pinch_clicks_as_soon_as_it_is_confirmed():
    d = Driver(drag_enabled=False)
    d.arm()
    d.frames(2, CLOSED, (50, 60))
    assert names(d.log)[-2:] == ["pinch_started", "click"]
    d.frames(30, CLOSED)                       # holding never drags
    d.frames(3, OPEN)
    assert names(d.log).count("click") == 1
    assert "mouse_down" not in names(d.log)


# ---- tracking loss and cleanup ---------------------------------------------

def test_a_brief_dropout_during_a_drag_does_not_drop_it():
    d = Driver()
    d.arm()
    d.frames(20, CLOSED)
    assert d.g.state == "dragging"
    d.frames(4, None)                          # 0.13 s gap < 0.25 s grace
    d.frames(3, CLOSED)
    assert d.g.state == "dragging"
    assert "mouse_up" not in names(d.log)


def test_losing_the_hand_mid_drag_always_releases():
    d = Driver()
    d.arm()
    d.frames(20, CLOSED)
    d.frames(10, None)                         # 0.33 s > grace
    assert "mouse_up" in names(d.log)
    assert d.g.state == "idle"


def test_losing_the_hand_mid_click_cancels_without_clicking():
    d = Driver()
    d.arm()
    d.frames(3, CLOSED)
    d.frames(10, None)
    assert "click" not in names(d.log)
    assert "cancelled" in names(d.log)


@pytest.mark.parametrize("frames_held, expect_up", [(20, True), (3, False), (0, False)])
def test_cancel_releases_only_if_the_button_is_down(frames_held, expect_up):
    d = Driver()
    d.arm()
    d.frames(frames_held, CLOSED)
    acts = names(d.g.cancel())
    assert ("mouse_up" in acts) == expect_up
    assert d.g.state == "idle"


def test_approach_rises_as_the_pinch_closes():
    g = PinchGesture(0.30, 0.4, release_threshold=0.44)
    g.state = "ready"
    assert g.approach(0.7) == 0
    assert 0.4 < g.approach(0.37) < 0.6
    assert g.approach(0.3) == 1


# ---- pointer ----------------------------------------------------------------

def test_one_euro_filter_calms_jitter_but_follows_real_movement():
    f = OneEuroFilter(min_cutoff=1.0, beta=5.0)
    t, outs = 0.0, []
    for i in range(60):
        t += DT
        outs.append(f(0.5 + (0.004 if i % 2 else -0.004), t))
    assert max(outs[20:]) - min(outs[20:]) < 0.002          # ±0.004 jitter shrinks a lot
    for i in range(30):
        t += DT
        out = f(0.5 + 0.02 * (i + 1), t)                     # a real sweep
    assert out > 0.5 + 0.02 * 30 * 0.8                       # lag stays small


def test_pointer_filter_barely_moves_while_a_pinch_closes():
    free, damped = PointerFilter(40), PointerFilter(40)
    t = 0.0
    for f in (free, damped):
        f(0.5, 0.5, 0.0)
    for _ in range(6):
        t += DT
        a = free(0.52, 0.5, t, approach=0.0)
        b = damped(0.52, 0.5, t, approach=1.0)
    assert (b[0] - 0.5) < (a[0] - 0.5) * 0.4


def test_stabilizer_holds_the_lock_then_drags_relative_then_settles():
    s = PointerStabilizer(settle=0.25)
    assert s.update((100, 100), "ready", None, 0.0) == (100, 100)
    assert s.update((130, 125), "pressed", (100, 100), 0.1) == (100, 100)   # frozen on the lock
    assert s.update((140, 130), "dragging", (100, 100), 0.2) == (100, 100)  # drag starts on the lock
    assert s.update((160, 150), "dragging", (100, 100), 0.3) == (120, 120)  # and moves with the hand
    first = s.update((170, 150), "ready", None, 0.35)       # dropped: no jump to (170, 150)
    assert first == (120, 120)
    later = s.update((170, 150), "ready", None, 0.9)
    assert later == (170, 150)                              # gap closed smoothly


# ---- scrolling --------------------------------------------------------------

def run_scroll(g, ys, pose=True, allowed=True, scale=0.1):
    t, total = 0.0, 0.0
    for y in ys:
        t += DT
        total += g.update(pose, y, scale, allowed, t)
    return total


def test_scroll_needs_the_pose_held_before_it_starts():
    g = ScrollGesture(sensitivity=50, dead_zone=0.2)
    assert run_scroll(g, [0.5, 0.5, 0.5]) == 0 and not g.active
    run_scroll(g, [0.5])
    assert g.active


def test_scroll_dead_zone_then_speed_grows_with_distance():
    g = ScrollGesture(sensitivity=50, dead_zone=0.2, ease_seconds=0)
    run_scroll(g, [0.5] * 4)
    assert run_scroll(g, [0.49] * 10) == 0             # 0.1 palm: inside the dead zone
    small = run_scroll(g, [0.46] * 10)                 # 0.4 palm up
    big = run_scroll(g, [0.40] * 10)                   # 1.0 palm up
    assert 0 < small < big                             # up scrolls up, further is faster
    assert run_scroll(g, [0.62] * 10) < 0              # below the centre scrolls down


def test_scroll_speed_is_capped():
    g = ScrollGesture(sensitivity=100, dead_zone=0.1, ease_seconds=0)
    run_scroll(g, [0.5] * 4)
    per_second = run_scroll(g, [0.0] * 30)             # absurdly far, for one second
    assert per_second <= ScrollGesture.MAX_NOTCHES_PER_SECOND * 1.05


def test_scroll_reverse_flips_direction():
    g = ScrollGesture(sensitivity=50, dead_zone=0.1, reverse=True, ease_seconds=0)
    run_scroll(g, [0.5] * 4)
    assert run_scroll(g, [0.4] * 10) < 0


def test_scroll_stops_when_not_allowed_or_the_pose_ends():
    g = ScrollGesture(sensitivity=50, dead_zone=0.1, ease_seconds=0)
    run_scroll(g, [0.5] * 4)
    assert run_scroll(g, [0.4] * 3, allowed=False) == 0 and not g.active   # e.g. a pinch began
    run_scroll(g, [0.5] * 4)
    assert g.active
    run_scroll(g, [0.4] * 4, pose=False)
    assert not g.active


# ---- held pose ----------------------------------------------------------------

def test_held_pose_fires_once_after_the_hold_and_forgives_a_flicker():
    h = HeldPose(hold_seconds=1.0, tolerance=2, cooldown=2.0)
    fired, t = [], 0.0
    for i in range(45):                        # 1.5 s with a one-frame dropout
        t += DT
        fired.append(h.update(i != 10, t))
    assert fired.count(True) == 1
    for i in range(60):                        # still held: no repeat
        t += DT
        fired.append(h.update(True, t))
    assert fired.count(True) == 1


def test_held_pose_short_holds_never_fire():
    h = HeldPose(hold_seconds=1.0)
    t = 0.0
    for cycle in range(5):
        for _ in range(20):                    # 0.67 s on…
            t += DT
            assert not h.update(True, t)
        for _ in range(5):                     # …then let go
            t += DT
            h.update(False, t)
