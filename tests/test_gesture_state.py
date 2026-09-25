from gesture_state import PinchGesture


def step(gesture, ratio, position=(100, 200), now=0.0):
    return [event.name for event in gesture.update(ratio, position, now)]


def test_short_pinch_clicks_at_pinch_start_position():
    gesture = PinchGesture(0.30, 0.4)
    step(gesture, 0.6, now=0.0); step(gesture, 0.6, now=0.01)  # arm
    step(gesture, 0.2, (100, 200), 0.02); step(gesture, 0.2, (100, 200), 0.03)
    assert step(gesture, 0.6, (900, 900), 0.04) == []
    events = gesture.update(0.6, (900, 900), 0.05)
    assert events[0].name == "click" and (events[0].x, events[0].y) == (100, 200)


def test_hold_starts_drag_and_release_always_lifts_button():
    gesture = PinchGesture(0.30, 0.1)
    step(gesture, 0.6, now=0); step(gesture, 0.6, now=.01)
    step(gesture, .2, now=.02); step(gesture, .2, now=.03)
    assert step(gesture, .2, now=.14) == ["mouse_down"]
    assert [x.name for x in gesture.cancel()] == ["mouse_up"]
