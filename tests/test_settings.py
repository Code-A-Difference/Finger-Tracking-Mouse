import json

import app_settings as st


def test_defaults_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    assert st.save(st.Settings(), path)
    assert st.load(path) == st.Settings()


def test_missing_or_corrupt_files_fall_back_to_defaults(tmp_path):
    assert st.load(tmp_path / "nope.json") == st.Settings()
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert st.load(bad) == st.Settings()
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    assert st.load(bad) == st.Settings()


def test_values_are_clamped_and_bad_choices_rejected():
    s = st.validate({"pinch_threshold": 999, "smoothing": -5, "pinch_hold_ms": "fast",
                     "screen": "moon", "scroll_pose": "index", "camera": "cv:x",
                     "show_landmarks": "yes", "reach": float("nan")})
    assert s.pinch_threshold == 60 and s.smoothing == 0
    assert s.pinch_hold_ms == st.Settings().pinch_hold_ms
    assert s.screen == "primary" and s.scroll_pose == "index"
    assert s.camera == "auto"
    assert s.show_landmarks is True          # a string is not a boolean
    assert s.reach == st.Settings().reach


def test_hide_gesture_cannot_be_enabled_without_the_confirmation():
    assert st.validate({"hide_gesture_enabled": True}).hide_gesture_enabled is False
    ok = st.validate({"hide_gesture_enabled": True, "hide_gesture_confirmed": True})
    assert ok.hide_gesture_enabled is True


def test_settings_from_version_2_1_are_migrated(tmp_path):
    old = {"camera": 2, "camera_view": "standard", "response": 50, "cursor_size": 44,
           "pinch_distance": 25, "pinch_hold_ms": 500, "scroll_enabled": False,
           "scroll_sensitivity": 60, "show_landmarks": False, "flip_off_enabled": True}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    s = st.load(path)
    assert s.camera == "cv:2"
    assert s.camera_resolution == "640x480"
    assert s.smoothing == 0                  # response 50% was the snappiest
    assert s.halo_size == 44 and s.pinch_threshold == 25 and s.pinch_hold_ms == 500
    assert s.scroll_enabled is False and s.scroll_sensitivity == 60
    assert s.hide_gesture_enabled and s.hide_gesture_confirmed
    assert st.validate(st.migrate({"camera": -1})).camera == "auto"


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "deep" / "settings.json"
    st.save(st.Settings(smoothing=77), path)
    assert json.loads(path.read_text(encoding="utf-8"))["smoothing"] == 77
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


def test_copy_validates_changes():
    s = st.Settings().copy(pinch_threshold=5, scroll_pose="two_fingers")
    assert s.pinch_threshold == 10
    assert s.release_threshold == 10 + s.pinch_release_gap


def test_eye_tracking_defaults_and_bad_choices():
    s = st.Settings()
    assert s.tracking_mode == "hand" and s.eye_click_mode == "dwell" and s.eye_calibration == ""
    s = st.validate({"tracking_mode": "gaze", "eye_click_mode": "wink", "eye_dwell_ms": 99999})
    assert s.tracking_mode == "hand"          # not a real mode: falls back
    assert s.eye_click_mode == "dwell"
    assert s.eye_dwell_ms == 2500             # clamped to the max


def test_eye_calibration_json_round_trips_through_settings():
    from gesture_state import GazeCalibration

    cal = GazeCalibration()
    cal.fit([((x, y), (0.5 + 0.3 * x, 0.5 + 0.3 * y)) for x in (-0.5, 0, 0.5) for y in (-0.5, 0, 0.5)])
    payload = cal.to_json()
    s = st.validate({"eye_calibration": payload})
    assert s.eye_calibration == payload
    assert GazeCalibration.from_json(s.eye_calibration).is_calibrated


def test_junk_eye_calibration_is_dropped_not_stored():
    s = st.validate({"eye_calibration": "not a real calibration"})
    assert s.eye_calibration == ""
