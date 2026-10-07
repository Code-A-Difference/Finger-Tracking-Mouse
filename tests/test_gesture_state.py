"""Gesture state machines, frame by frame (30 fps unless a test says otherwise)."""

import math

import pytest

from gesture_state import DwellClick, GazeCalibration, gaze_features, steady_reading, HeldPose, OneEuroFilter, PinchGesture, PointerFilter, \
    PointerStabilizer, ScrollGesture

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


# ---------------------------------------------------------------------------
# Eye tracking: calibration and dwell-to-click
# ---------------------------------------------------------------------------

def test_gaze_calibration_needs_at_least_six_points():
    cal = GazeCalibration()
    samples = [((-0.5, -0.5), (0.1, 0.1))] * 5
    assert cal.fit(samples) is False
    assert not cal.is_calibrated
    assert cal.apply((0, 0)) is None


def test_gaze_calibration_fits_a_grid_and_interpolates_between_points():
    def true_screen(offset):
        x, y = offset
        return (0.5 + 0.4 * x, 0.5 + 0.4 * y)

    grid = [-0.8, 0.0, 0.8]
    samples = [((x, y), true_screen((x, y))) for x in grid for y in grid]
    cal = GazeCalibration()
    assert cal.fit(samples) is True
    assert cal.is_calibrated

    got = cal.apply((0.8, -0.8))          # a calibration point: recovered almost exactly
    want = true_screen((0.8, -0.8))       # (the ridge costs a hair: well under 0.1% of the screen)
    assert abs(got[0] - want[0]) < 1e-3
    assert abs(got[1] - want[1]) < 1e-3

    got = cal.apply((0.4, 0.2))           # a point it never saw: interpolated closely
    want = true_screen((0.4, 0.2))
    assert abs(got[0] - want[0]) < 0.02
    assert abs(got[1] - want[1]) < 0.02


def test_gaze_calibration_apply_clamps_to_the_screen():
    grid = [-0.5, 0.0, 0.5]
    samples = [((x, y), (0.5 + 0.6 * x, 0.5 + 0.6 * y)) for x in grid for y in grid]
    cal = GazeCalibration()
    cal.fit(samples)
    x, y = cal.apply((5.0, 5.0))          # far outside the calibrated range
    assert 0.0 <= x <= 1.0
    assert 0.0 <= y <= 1.0


def test_gaze_calibration_round_trips_through_json():
    grid = [-0.6, 0.0, 0.6]
    samples = [((x, y), (0.5 + 0.3 * x, 0.5 + 0.3 * y)) for x in grid for y in grid]
    cal = GazeCalibration()
    cal.fit(samples)
    restored = GazeCalibration.from_json(cal.to_json())
    assert restored.is_calibrated
    for point in ((0.3, -0.4), (-0.6, 0.6)):
        a, b = cal.apply(point), restored.apply(point)
        assert abs(a[0] - b[0]) < 1e-9
        assert abs(a[1] - b[1]) < 1e-9


def test_gaze_calibration_from_json_rejects_garbage():
    for bad in ("", "not json", "{}", '{"x": [1,2,3], "y": [1,2,3,4,5,6]}', "[1,2,3]"):
        cal = GazeCalibration.from_json(bad)
        assert not cal.is_calibrated
        assert cal.apply((0, 0)) is None


def test_dwell_fires_once_after_holding_steady():
    d = DwellClick(hold_seconds=0.5, radius=0.03)
    t = 0.0
    fired = [d.update((0.5, 0.5), t := t + DT) for _ in range(20)]   # 0.67 s, plenty
    assert fired.count(True) == 1
    assert fired.index(True) >= 14   # not before ~0.5 s of holding


def test_dwell_tolerates_small_jitter_within_the_radius():
    d = DwellClick(hold_seconds=0.4, radius=0.03)
    t = 0.0
    fired = []
    for i in range(18):
        t += DT
        jitter = 0.01 if i % 2 == 0 else -0.01   # smaller than the radius
        fired.append(d.update((0.5 + jitter, 0.5), t))
    assert True in fired


def test_dwell_re_anchors_instead_of_firing_when_the_gaze_jumps():
    d = DwellClick(hold_seconds=0.4, radius=0.03)
    t = 0.0
    for _ in range(10):                   # a third of a second: not enough to fire
        t += DT
        assert d.update((0.5, 0.5), t) is False
    t += DT
    assert d.update((0.9, 0.9), t) is False   # jumps far away: re-anchors, doesn't fire
    fired = []
    for _ in range(10):                   # another third of a second at the new spot
        t += DT
        fired.append(d.update((0.9, 0.9), t))
    assert True not in fired              # needs the full hold time again from here


def test_dwell_requires_looking_away_before_firing_again():
    d = DwellClick(hold_seconds=0.3, radius=0.03)
    t = 0.0
    fired = [d.update((0.5, 0.5), t := t + DT) for _ in range(12)]
    assert fired.count(True) == 1
    fired += [d.update((0.5, 0.5), t := t + DT) for _ in range(20)]   # still staring: no repeat
    assert fired.count(True) == 1
    t += DT
    d.update((0.9, 0.9), t)               # look away…
    fired += [d.update((0.5, 0.5), t := t + DT) for _ in range(12)]  # …and back: fires again
    assert fired.count(True) == 2


def test_dwell_resets_on_losing_the_gaze():
    d = DwellClick(hold_seconds=0.3, radius=0.03)
    t = 0.0
    for _ in range(8):
        t += DT
        d.update((0.5, 0.5), t)
    assert d.progress(t) > 0
    t += DT
    assert d.update(None, t) is False
    assert d.progress(t) == 0.0


def test_dwell_progress_climbs_then_drops_back_to_zero_after_firing():
    d = DwellClick(hold_seconds=0.5, radius=0.03)
    t = 0.0
    progress = []
    for _ in range(20):
        t += DT
        d.update((0.5, 0.5), t)
        progress.append(d.progress(t))
    assert progress[0] < progress[5] < progress[10]
    assert progress[-1] == 0.0   # fired partway through; armed=False reads as no progress


def _thirteen_points():
    return [(fx, fy) for fx in (0.05, 0.5, 0.95) for fy in (0.05, 0.5, 0.95)] +            [(0.275, 0.275), (0.725, 0.275), (0.275, 0.725), (0.725, 0.725)]


def test_gaze_calibration_follows_the_head_when_head_was_recorded():
    # Truth: the screen point depends on where the eye sits in its socket AND
    # on head turn. A head-blind fit gets this wrong as soon as the head moves.
    def screen(offset, head):
        return (0.5 + 0.45 * offset[0] + 0.3 * head[0], 0.5 + 0.45 * offset[1] + 0.3 * head[1])

    samples = []
    for i, (tx, ty) in enumerate(_thirteen_points()):
        head = (0.05 * ((i % 3) - 1), 0.04 * ((i % 2) * 2 - 1))     # the head wanders a little between dots
        offset = ((tx - 0.5 - 0.3 * head[0]) / 0.45, (ty - 0.5 - 0.3 * head[1]) / 0.45)
        samples.append((offset, (tx, ty), head))
    cal = GazeCalibration()
    assert cal.fit(samples) is True
    assert cal.use_head
    assert cal.error(samples) < 0.005

    moved = (0.08, -0.06)                                            # later, the head has turned
    off = (0.2, -0.3)
    got, want = cal.apply(off, moved), screen(off, moved)
    assert abs(got[0] - want[0]) < 0.01 and abs(got[1] - want[1]) < 0.01

    blind = GazeCalibration()
    blind.fit([(o, t) for o, t, _ in samples])
    assert not blind.use_head
    b = blind.apply(off)
    assert abs(b[0] - want[0]) > abs(got[0] - want[0])               # ignoring the head is worse


def test_head_aware_calibration_round_trips_and_old_six_term_ones_still_load():
    samples = [((tx - 0.5, ty - 0.5), (tx, ty), (0.01 * i, -0.01 * i)) for i, (tx, ty) in enumerate(_thirteen_points())]
    cal = GazeCalibration()
    cal.fit(samples)
    again = GazeCalibration.from_json(cal.to_json())
    assert again.use_head and again.head_ref == cal.head_ref
    a, b = cal.apply((0.1, 0.2), (0.03, 0.0)), again.apply((0.1, 0.2), (0.03, 0.0))
    assert abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9

    old = '{"x": [0.5, 0.4, 0, 0, 0, 0], "y": [0.5, 0, 0.4, 0, 0, 0]}'
    legacy = GazeCalibration.from_json(old)
    assert legacy.is_calibrated and not legacy.use_head
    assert legacy.apply((0.0, 0.0), (0.5, 0.5)) == (0.5, 0.5)       # head is ignored for an old calibration
    assert not GazeCalibration.from_json('{"x": [1,2,3,4,5,6,7,8], "y": [1,2,3,4,5,6,7,8], "head": "x"}').is_calibrated


def test_steady_reading_drops_blinks_and_ignores_a_glance():
    frames = [((0.20, -0.10), (0.0, 0.0), 0.05)] * 12
    frames += [((0.20, 0.60), (0.0, 0.0), 0.9)] * 4          # a blink drags the iris down
    frames += [((-0.70, 0.40), (0.0, 0.0), 0.05)] * 2        # a glance elsewhere
    offset, head = steady_reading(frames)
    assert offset == (0.20, -0.10)
    assert steady_reading([((0, 0), (0, 0), 0.9)] * 20) is None       # all blinks: no reading
    assert steady_reading([((0, 0), (0, 0), 0.0)] * 3) is None        # too few frames


# -- v3: the full calibration (grid + moving dot), many features -----------

def _simulated_eye(tx, ty, head=(0.0, 0.6), rng=None):
    """What a real eye looks like at screen point (tx, ty): the iris moves
    plenty sideways but barely up and down; the eyelid opening carries most
    of the vertical; the two eyes disagree slightly near the edges; the head
    shifts it all; plus measurement noise."""
    import random
    rng = rng or random.Random(0)
    n = lambda s: rng.gauss(0, s)
    gx = (tx - 0.5) / 0.55 + 0.4 * (head[0])
    gy = 0.25 * (ty - 0.5) / 0.55 + 0.2 * (head[1] - 0.6)
    openness = 0.36 - 0.18 * (ty - 0.5) + 0.05 * (head[1] - 0.6)
    rx, lx = gx + 0.12 * (tx - 0.5), gx - 0.12 * (tx - 0.5)
    return gaze_features((gx + n(0.02), gy + n(0.02)), (head[0] + n(0.005), head[1] + n(0.005)),
                         ((rx + n(0.02), gy + n(0.02)), (lx + n(0.02), gy + n(0.02))), openness + n(0.004))


def _calibration_set(rng):
    grid = [(x, y) for y in (0.04, 0.27, 0.5, 0.73, 0.96) for x in (0.04, 0.27, 0.5, 0.73, 0.96)]
    samples, weights = [], []
    for i, (tx, ty) in enumerate(grid):
        head = (0.03 * ((i % 3) - 1), 0.6 + 0.02 * ((i % 2) * 2 - 1))
        samples.append((_simulated_eye(tx, ty, head, rng), (tx, ty)))
        weights.append(8.0)
    for k in range(300):                                   # the moving dot
        import math as m
        tx, ty = 0.5 + 0.45 * m.sin(k / 23), 0.5 + 0.45 * m.sin(k / 17 + 1)
        samples.append((_simulated_eye(tx, ty, (0.0, 0.6), rng), (tx, ty)))
        weights.append(1.0)
    return samples, weights


def test_full_calibration_beats_the_old_mapping_on_points_it_never_saw():
    import random
    rng = random.Random(7)
    samples, weights = _calibration_set(rng)
    full = GazeCalibration()
    assert full.fit(samples, weights) is True
    assert full.version == 3 and full.ridge in GazeCalibration.RIDGE_GRID

    old = GazeCalibration()                                 # offset + head only, the 2.4.0 way
    old.fit([(f[:2], t, (f[7], f[8])) for f, t in samples[:25]])

    test = [(_simulated_eye(x, y, (0.0, 0.6), rng), (x, y)) for x, y in ((0.2, 0.3), (0.8, 0.35), (0.5, 0.8), (0.3, 0.65), (0.66, 0.12))]
    new_err = full.error(test)
    old_err = old.error([(f[:2], t, (f[7], f[8])) for f, t in test])
    assert new_err < 0.03, new_err
    assert new_err < old_err * 0.6, (new_err, old_err)     # clearly better, mostly on vertical


def test_full_calibration_round_trips_recentres_and_still_takes_a_bare_offset():
    import random
    samples, weights = _calibration_set(random.Random(3))
    cal = GazeCalibration()
    cal.fit(samples, weights)
    again = GazeCalibration.from_json(cal.to_json())
    assert again.version == 3
    f = samples[3][0]
    a, b = cal.apply(f), again.apply(f)
    assert abs(a[0] - b[0]) < 1e-4 and abs(a[1] - b[1]) < 1e-4     # stored to 7 significant figures

    # drift: everything now lands 0.1 to the right; one look at the centre fixes it
    centre = _simulated_eye(0.5, 0.5, (0.0, 0.6), random.Random(1))
    again.bias = (0.1, 0.0)
    assert again.recentre(centre) is True
    p = again.apply(centre)
    assert abs(p[0] - 0.5) < 1e-6 and abs(p[1] - 0.5) < 1e-6
    assert again.apply(f[:2]) is not None                   # older callers' bare offsets still work
    assert not GazeCalibration.from_json('{"v": 3, "x": [1], "y": [1], "mean": [], "scale": []}').is_calibrated


def test_steady_reading_takes_the_median_of_every_feature():
    frames = [(tuple(float(k) for k in range(9)), (0.0, 0.6), 0.0)] * 9 + [(tuple(9.0 for _ in range(9)), (0.0, 0.6), 0.0)] * 2
    features, head = steady_reading(frames)
    assert features == tuple(float(k) for k in range(9))
