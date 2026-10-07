"""Head pointer: head_pose.measure() and the HeadPointer / WinkClick / FaceSwitch
state machines, from synthetic data (no camera, no MediaPipe)."""

import head_pose
from gesture_state import FaceSwitch, HeadPointer, WinkClick


class L:
    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x, self.y = x, y


def face(nose=(0.5, 0.55), eye_gap=0.2, mouth_gap=0.01):
    """478 points with the eye corners, nose tip and lips placed."""
    pts = [L(0.0, 0.0)] * 478
    cx = 0.5
    pts[33], pts[133] = L(cx - eye_gap / 2 - 0.04, 0.45), L(cx - eye_gap / 2 + 0.04, 0.45)
    pts[263], pts[362] = L(cx + eye_gap / 2 + 0.04, 0.45), L(cx + eye_gap / 2 - 0.04, 0.45)
    pts[1] = L(*nose)
    pts[61], pts[291] = L(0.45, 0.65), L(0.55, 0.65)
    pts[13], pts[14] = L(0.5, 0.65 - mouth_gap / 2), L(0.5, 0.65 + mouth_gap / 2)
    return pts


# -- head_pose --------------------------------------------------------------

def test_nose_is_measured_in_face_widths_so_distance_from_the_camera_does_not_change_speed():
    near = head_pose.measure(face(nose=(0.55, 0.55), eye_gap=0.3), {}, 1.0)
    near0 = head_pose.measure(face(nose=(0.5, 0.55), eye_gap=0.3), {}, 1.0)
    far = head_pose.measure(face(nose=(0.525, 0.55), eye_gap=0.15), {}, 1.0)
    far0 = head_pose.measure(face(nose=(0.5, 0.55), eye_gap=0.15), {}, 1.0)
    # the same head turn looks half as big from twice as far, and measures the same
    assert abs((near.nose[0] - near0.nose[0]) - (far.nose[0] - far0.nose[0])) < 1e-9


def test_winks_are_the_persons_own_left_and_right_in_a_mirrored_picture():
    m = head_pose.measure(face(), {"eyeBlinkLeft": 0.9, "eyeBlinkRight": 0.05}, 1.0)
    assert m.wink_right == 0.9 and m.wink_left == 0.05          # the face's left is your right, mirrored
    m = head_pose.measure(face(), {"eyeBlinkLeft": 0.9, "eyeBlinkRight": 0.05}, 1.0, swap_eyes=True)
    assert m.wink_left == 0.9


def test_mouth_and_smile():
    m = head_pose.measure(face(), {"jawOpen": 0.7, "mouthSmileLeft": 0.8, "mouthSmileRight": 0.6}, 1.0)
    assert m.mouth_open == 0.7 and abs(m.smile - 0.7) < 1e-9
    closed = head_pose.measure(face(mouth_gap=0.0), {}, 1.0).mouth_open
    wide = head_pose.measure(face(mouth_gap=0.06), {}, 1.0).mouth_open     # no blendshape: lip geometry
    assert closed == 0.0 and wide > 0.5


def test_a_face_too_small_to_steer_with_is_rejected():
    assert head_pose.measure(face(eye_gap=0.0), {}, 1.0) is None


# -- HeadPointer ------------------------------------------------------------

def run_relative(path, **kw):
    p = HeadPointer(smoothing=0, **kw)
    total = [0.0, 0.0]
    for i, nose in enumerate(path):
        dx, dy = p.update(nose, i / 30)
        total[0] += dx
        total[1] += dy
    return total


def test_relative_moves_with_the_nose_and_holds_still_when_the_head_does():
    moved = run_relative([(10 + i * 0.01, 5.0) for i in range(30)])
    assert moved[0] > 0.2 and abs(moved[1]) < 1e-9
    still = run_relative([(10 + (0.0005 if i % 2 else 0), 5.0) for i in range(60)])   # tremor
    assert still == [0.0, 0.0]


def test_relative_accelerates_so_a_quick_flick_goes_further_than_a_slow_drift():
    slow = run_relative([(10 + i * 0.005, 5) for i in range(61)])          # 0.3 face widths in 2 s
    fast = run_relative([(10 + i * 0.03, 5) for i in range(11)])           # the same 0.3 in a third of a second
    assert fast[0] > slow[0] * 1.5


def test_freezing_holds_the_pointer_and_does_not_jump_afterwards():
    p = HeadPointer(smoothing=0)
    p.update((10, 5), 0.0)
    assert p.update((10.5, 5), 0.1, frozen=True) == (0.0, 0.0)   # winking: head twitches, pointer doesn't
    assert p.update((10.5, 5), 0.2) == (0.0, 0.0)                 # and nothing is owed when it lets go


def test_absolute_maps_straight_ahead_to_the_centre_and_reach_to_the_edge():
    p = HeadPointer(mode="absolute", smoothing=0, reach=30)
    assert p.update((10, 5), 0.0) == (0.5, 0.5)
    for i in range(1, 40):                       # hold the turned head long enough for the smoothing to settle
        x, y = p.update((10.3, 5), i / 30)
    assert abs(x - 1.0) < 1e-3 and abs(y - 0.5) < 1e-9
    p.recentre()
    for i in range(40, 80):
        x, y = p.update((10.3, 5), i / 30)
    assert abs(x - 0.5) < 1e-3 and abs(y - 0.5) < 1e-9


# -- WinkClick ---------------------------------------------------------------

def test_a_held_wink_clicks_once_with_the_matching_button():
    w = WinkClick(0.2)
    events = [w.update(0.9, 0.05, t / 30) for t in range(20)]
    assert events.count("left") == 1
    assert w.update(0.05, 0.05, 1.0) is None
    events = [w.update(0.05, 0.9, 1 + t / 30) for t in range(20)]
    assert events.count("right") == 1


def test_an_ordinary_blink_or_a_too_short_wink_does_nothing():
    w = WinkClick(0.2)
    assert all(w.update(0.95, 0.95, t / 30) is None for t in range(30))          # blink
    w.reset()
    assert all(w.update(0.9, 0.05, t / 30) is None for t in range(3))            # 0.07 s: too short


# -- FaceSwitch ---------------------------------------------------------------

def test_face_switch_needs_a_hold_and_has_hysteresis():
    s = FaceSwitch(on=0.4, off=0.2, hold_seconds=0.15)
    assert s.update(0.5, 0.0) is None and s.pending
    assert s.update(0.5, 0.1) is None
    assert s.update(0.5, 0.2) == "start" and s.active
    assert s.update(0.3, 0.3) is None                 # between thresholds: stays on
    assert s.update(0.1, 0.4) == "end"
    s.reset()
    s.update(0.5, 1.0)
    assert s.update(0.1, 1.05) is None and not s.pending      # a word, not a hold
