from pathlib import Path

import pytest

import eye_tracker

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not (ROOT / "models" / "face_landmarker.task").exists(), reason="model not downloaded")
def test_bundle_check_needs_no_graph():
    assert "SHA-256 ok" in eye_tracker.check_bundle()


def test_a_damaged_or_missing_model_is_refused(tmp_path):
    bad = tmp_path / "face_landmarker.task"
    bad.write_bytes(b"not a model")
    with pytest.raises(RuntimeError, match="damaged"):
        eye_tracker.load_model_bytes(bad)
    with pytest.raises(RuntimeError, match="fetch_model"):
        eye_tracker.load_model_bytes(tmp_path / "missing.task")


@pytest.mark.skipif(not (ROOT / "models" / "face_landmarker.task").exists(), reason="model not downloaded")
@pytest.mark.skipif(not eye_tracker.start_check()[0],
                       reason="MediaPipe can't open a model on this machine (e.g. a macOS CI VM)")
def test_eye_tracker_runs_real_inference_and_finds_no_face_in_a_blank_frame():
    import numpy as np

    tracker = eye_tracker.EyeTracker()
    try:
        assert tracker.process(np.zeros((240, 320, 3), np.uint8)) is None
    finally:
        tracker.close()


@pytest.mark.skipif(not (ROOT / "models" / "face_landmarker.task").exists(), reason="model not downloaded")
def test_eye_tracker_shares_the_hand_models_macos_probe():
    # eye_tracker deliberately has no probe machinery of its own — it reuses
    # hand_tracker.start_check(), since the crash it guards against is in
    # MediaPipe's native library, not any one model.
    import hand_tracker

    assert eye_tracker.start_check is hand_tracker.start_check
