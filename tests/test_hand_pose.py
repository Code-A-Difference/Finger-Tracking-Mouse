"""Pose reading: synthetic hands at many angles, plus real photos through MediaPipe."""

from pathlib import Path

import pytest

import hand_pose as hp
from handgen import make_hand

ASPECT = 16 / 9
ANGLES = (-60, -30, 0, 30, 60, 90)


def measure(hand):
    return hp.measure(hand, ASPECT)


@pytest.mark.parametrize("angle", ANGLES)
def test_two_finger_scroll_pose_at_any_angle(angle):
    m = measure(make_hand(("index", "middle"), rotate=angle))
    assert hp.is_scroll_pose(m, "two_fingers")
    assert not hp.is_scroll_pose(m, "index")
    assert not hp.is_hide_pose(m)


@pytest.mark.parametrize("angle", ANGLES)
def test_pointing_hand_is_not_the_default_scroll_pose(angle):
    # People move the pointer with a pointing hand; it must not scroll.
    m = measure(make_hand(("index",), rotate=angle))
    assert not hp.is_scroll_pose(m, "two_fingers")
    assert hp.is_scroll_pose(m, "index")      # only if they chose index-only scrolling


@pytest.mark.parametrize("angle", ANGLES)
def test_hide_pose_needs_middle_alone(angle):
    assert hp.is_hide_pose(measure(make_hand(("middle",), rotate=angle)))
    assert not hp.is_hide_pose(measure(make_hand(("middle", "index"), rotate=angle)))
    assert not hp.is_hide_pose(measure(make_hand(("middle", "ring"), rotate=angle)))
    assert not hp.is_hide_pose(measure(make_hand(("index", "middle", "ring", "pinky"), rotate=angle)))


@pytest.mark.parametrize("size", (0.06, 0.25))
def test_pinch_ratio_is_independent_of_distance_from_camera(size):
    def ratios(s):
        return (measure(make_hand(("index", "middle", "ring", "pinky"), pinch=0.9, size=s)).pinch_ratio,
                measure(make_hand(("index",), pinch=0.1, size=s)).pinch_ratio)

    near_or_far, reference = ratios(size), ratios(0.12)
    assert near_or_far == pytest.approx(reference, rel=1e-6)
    open_ratio, pinched_ratio = reference
    assert pinched_ratio < 0.3 < open_ratio      # either side of the default threshold


def test_open_hand_reads_all_fingers_extended_and_fist_all_folded():
    assert set(measure(make_hand(("index", "middle", "ring", "pinky"))).fingers.values()) == {hp.EXTENDED}
    assert set(measure(make_hand(())).fingers.values()) == {hp.FOLDED}


# ---- real photos --------------------------------------------------------------
# MediaPipe's own test images (Apache 2.0; see tests/data/README.md), run
# through the real model, at three rotations each.

DATA = Path(__file__).parent / "data"
MODEL = Path(__file__).resolve().parent.parent / "models" / "hand_landmarker.task"


@pytest.fixture(scope="module")
def landmarker():
    if not MODEL.exists():
        pytest.skip("model not downloaded (python tools/fetch_model.py)")
    import hand_tracker
    ok, why = hand_tracker.start_check()
    if not ok:
        pytest.skip(why)
    cv2 = pytest.importorskip("cv2")
    mp = pytest.importorskip("mediapipe")
    from mediapipe.tasks.python import BaseOptions, vision

    options = vision.HandLandmarkerOptions(base_options=BaseOptions(model_asset_buffer=MODEL.read_bytes()),
                                           running_mode=vision.RunningMode.IMAGE, num_hands=1)
    detector = vision.HandLandmarker.create_from_options(options)

    def detect(name, rotate):
        image = cv2.imread(str(DATA / name))
        if rotate:
            h, w = image.shape[:2]
            matrix = cv2.getRotationMatrix2D((w / 2, h / 2), rotate, 0.8)
            image = cv2.warpAffine(image, matrix, (w, h), borderValue=(200, 200, 200))
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        result = detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        assert result.hand_landmarks, f"no hand found in {name}"
        return hp.measure(result.hand_landmarks[0], image.shape[1] / image.shape[0])

    yield detect
    detector.close()


@pytest.mark.parametrize("rotate", (0, 45, 90))
def test_real_photos(landmarker, rotate):
    victory = landmarker("victory.jpg", rotate)
    assert hp.is_scroll_pose(victory, "two_fingers")

    pointing = landmarker("pointing_up.jpg", rotate)
    assert not hp.is_scroll_pose(pointing, "two_fingers")
    assert hp.is_scroll_pose(pointing, "index")

    for name in ("fist.jpg", "thumb_up.jpg"):
        m = landmarker(name, rotate)
        assert not hp.is_scroll_pose(m, "two_fingers")
        assert not hp.is_hide_pose(m)
        assert set(m.fingers.values()) == {hp.FOLDED}   # the engine's fist guard relies on this
