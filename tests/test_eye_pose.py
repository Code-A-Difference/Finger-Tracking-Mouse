"""eye_pose.measure(), from synthetic landmarks (no camera, no MediaPipe)."""

import eye_pose


class L:
    """A stand-in for MediaPipe's landmark objects: just x and y."""
    __slots__ = ("x", "y")

    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y


def landmarks(right_iris=(0.35, 0.5), left_iris=(0.65, 0.5)):
    """478 landmarks, mostly at the origin, with the eight eye-socket points
    and both iris centres placed for a face looking at (0.5, 0.5) — outer
    corners at the frame edges, inner corners toward the nose, eyes centred
    vertically around y=0.5."""
    pts = [L(0.0, 0.0)] * 479
    pts[eye_pose.RIGHT_EYE["outer"]] = L(0.2, 0.5)
    pts[eye_pose.RIGHT_EYE["inner"]] = L(0.45, 0.5)
    pts[eye_pose.RIGHT_EYE["top"]] = L(0.325, 0.46)
    pts[eye_pose.RIGHT_EYE["bottom"]] = L(0.325, 0.54)
    pts[eye_pose.LEFT_EYE["outer"]] = L(0.8, 0.5)
    pts[eye_pose.LEFT_EYE["inner"]] = L(0.55, 0.5)
    pts[eye_pose.LEFT_EYE["top"]] = L(0.675, 0.46)
    pts[eye_pose.LEFT_EYE["bottom"]] = L(0.675, 0.54)
    pts[eye_pose.RIGHT_IRIS] = L(*right_iris)
    pts[eye_pose.LEFT_IRIS] = L(*left_iris)
    return pts


def test_iris_centred_in_the_socket_is_a_zero_offset():
    m = eye_pose.measure(landmarks(right_iris=(0.325, 0.5), left_iris=(0.675, 0.5)), {}, 1.0)
    assert m is not None
    assert abs(m.offset[0]) < 1e-6
    assert abs(m.offset[1]) < 1e-6


def test_iris_toward_the_inner_corner_reads_positive_x():
    # Right eye: outer 0.2, inner 0.45 — iris near the inner corner is +1;
    # the left eye's iris stays centred (0), so the average is roughly half that.
    m = eye_pose.measure(landmarks(right_iris=(0.44, 0.5), left_iris=(0.675, 0.5)), {}, 1.0)
    assert m is not None
    assert m.offset[0] > 0.3


def test_iris_toward_the_top_of_the_socket_reads_negative_y():
    m = eye_pose.measure(landmarks(right_iris=(0.325, 0.465), left_iris=(0.675, 0.465)), {}, 1.0)
    assert m is not None
    assert m.offset[1] < -0.5


def test_both_eyes_average_together():
    # Right eye centred (offset 0), left eye pushed fully to +1: average is +0.5.
    m = eye_pose.measure(landmarks(right_iris=(0.325, 0.5), left_iris=(0.8, 0.5)), {}, 1.0)
    assert m is not None
    assert 0.4 < m.offset[0] < 0.6


def test_one_collapsed_eye_socket_still_reads_from_the_other():
    pts = landmarks()
    # Collapse the left eye's socket to a single point.
    pts[eye_pose.LEFT_EYE["outer"]] = L(0.675, 0.5)
    pts[eye_pose.LEFT_EYE["inner"]] = L(0.675, 0.5)
    pts[eye_pose.LEFT_EYE["top"]] = L(0.675, 0.5)
    pts[eye_pose.LEFT_EYE["bottom"]] = L(0.675, 0.5)
    m = eye_pose.measure(pts, {}, 1.0)
    assert m is not None   # the right eye alone is enough


def test_both_eyes_collapsed_is_no_measurement():
    pts = landmarks()
    for eye in (eye_pose.RIGHT_EYE, eye_pose.LEFT_EYE):
        for k in eye.values():
            pts[k] = L(0.5, 0.5)
    assert eye_pose.measure(pts, {}, 1.0) is None


def test_blink_is_the_stronger_of_the_two_eyes_and_clamped():
    m = eye_pose.measure(landmarks(), {"eyeBlinkLeft": 0.9, "eyeBlinkRight": 0.2}, 1.0)
    assert m.blink_left == 0.9
    assert m.blink_right == 0.2
    assert m.blink == 0.9


def test_missing_blendshapes_read_as_open():
    m = eye_pose.measure(landmarks(), {}, 1.0)
    assert m.blink == 0.0


def test_out_of_range_blendshape_scores_are_clamped():
    m = eye_pose.measure(landmarks(), {"eyeBlinkLeft": 1.4, "eyeBlinkRight": -0.3}, 1.0)
    assert m.blink_left == 1.0
    assert m.blink_right == 0.0
