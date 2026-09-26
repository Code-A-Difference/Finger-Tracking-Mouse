"""The tracking loop: newest camera frame -> hand landmarks -> gestures ->
pointer commands. No Qt here, so it runs (and is tested) without a window.

Threads and who owns what:

    camera thread    FrameGrabber      reads the webcam, keeps the newest frame
    tracking thread  TrackingEngine    this file: inference and gestures
    output thread    PointerOutput     moves and clicks the real pointer
    UI thread        Qt                reads ``engine.view`` on a timer

The engine never waits on the UI or the pointer, and nothing waits on the
engine: the window polls a snapshot, the pointer thread takes commands from
a queue. If the camera stalls, the engine sees no new frame, lets go of the
mouse and restarts the grabber; if inference throws, it rebuilds the hand
tracker; a result that arrives too late to trust is treated as "no hand".
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import cv2
import numpy as np

import eye_pose
import hand_pose
from app_settings import Settings
from camera import CameraCapabilities, CameraInfo, FrameGrabber, make_source
from eye_tracker import EyeTracker, draw_eye_points
from gesture_state import DwellClick, GazeCalibration, HeldPose, PinchGesture, PointerFilter, PointerStabilizer, \
    ScrollGesture
from hand_tracker import HandTracker, draw_landmarks
from pointer_output import PointerOutput

log = logging.getLogger(__name__)

INFERENCE_WIDTH = 640          # frames are shrunk to this before hand/face tracking
STALE_AFTER = 0.5              # s: a result about an older frame is not acted on
CAMERA_STALL_AFTER = 2.5       # s without a frame before the camera is restarted
PREVIEW_FPS = 15

CAMERA_KEYS = ("camera", "camera_resolution", "stream_url")
TRACKER_FACTORIES: dict[str, Callable[[], Any]] = {"hand": HandTracker, "eye": EyeTracker}


@dataclass
class View:
    """What the window shows. Replaced wholesale, so reading it is always consistent."""
    preview: Optional[np.ndarray] = None      # RGB
    preview_seq: int = 0
    cursor: Optional[tuple[int, int]] = None  # native desktop coordinates
    gesture: str = "starting"
    label: str = "Starting…"
    tracked: bool = False          # a hand (hand mode) or a face (eye mode) is in view
    camera_state: str = "opening"
    camera_detail: Any = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class TrackingEngine(threading.Thread):
    """Runs until ``stop()``. Posts ``(kind, payload)`` events to ``events``."""

    def __init__(self, settings: Settings, output: PointerOutput, cameras: list[CameraInfo],
                 tracker_factory: Optional[Callable[[], Any]] = None,
                 grabber_factory: Callable[..., FrameGrabber] = FrameGrabber,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(name="tracking", daemon=True)
        self.settings = settings
        self.output = output
        self.cameras = cameras
        # None (the production default) means "pick hand vs. eye from settings";
        # tests pass their own fake tracker, which then applies to either mode.
        self._tracker_factory_override = tracker_factory
        self.grabber_factory = grabber_factory
        self.clock = clock
        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.view = View()
        self.preview_width = 640

        self._halt = threading.Event()
        self._pending_settings: Optional[Settings] = None
        self._settings_lock = threading.Lock()
        self.grabber: Optional[FrameGrabber] = None
        self.tracker: Any = None

        s = settings
        self.pinch = PinchGesture(s.pinch_threshold / 100, s.pinch_hold_ms / 1000, s.click_stability,
                                  s.release_threshold / 100, s.drag_enabled)
        self.filter = PointerFilter(s.smoothing)
        self.stabilizer = PointerStabilizer()
        self.scroll = ScrollGesture(s.scroll_sensitivity, s.scroll_dead_zone / 100, s.scroll_reverse)
        self.hide = HeldPose(s.hide_gesture_hold_ms / 1000)

        # eye tracking
        self.gaze_filter = PointerFilter(s.eye_smoothing)
        self.gaze_calibration = GazeCalibration.from_json(s.eye_calibration)
        self.dwell = DwellClick(s.eye_dwell_ms / 1000, s.eye_dwell_radius / 100)
        self.last_gaze_offset: Optional[tuple[float, float]] = None   # read by the calibration dialog
        self._blink_since: Optional[float] = None
        self._blink_fired = False

        # diagnostics
        self.infer_started: Optional[float] = None
        self.loop_started: Optional[float] = None
        self.infer_ms = 0.0
        self.loop_fps = 0.0
        self.inference_errors = 0
        self.frames_processed = 0
        self.stale_frames = 0
        self._last_preview = 0.0
        self._preview_seq = 0
        self._desktop: tuple[int, int, int, int] = (0, 0, 1920, 1080)
        self._desktop_checked = -1e9
        self._last_cursor: Optional[tuple[int, int]] = None
        self._hand_frames = 0
        self._face_frames = 0
        self._label = "Starting…"
        self._gesture = "starting"
        self._camera_state = "opening"
        self._camera_detail: Any = None
        self._frame_times: list[float] = []

    # -- control, from any thread -------------------------------------------
    def stop(self) -> None:
        self._halt.set()

    def update_settings(self, settings: Settings) -> None:
        with self._settings_lock:
            self._pending_settings = settings

    def camera_command(self, command: str, value: Any = None) -> None:
        if self.grabber is not None:
            self.grabber.request(command, value)

    def post(self, kind: str, payload: Any = None) -> None:
        self.events.put((kind, payload))

    # -- the loop -------------------------------------------------------------
    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # the loop must never die holding the mouse
            log.exception("Tracking stopped by an unexpected error")
            self.post("fatal", f"Tracking stopped: {exc}")
        finally:
            self._release_everything()
            if self.grabber is not None:
                self.grabber.stop()
            if self.tracker is not None:
                self.tracker.close()
            self.post("stopped", None)

    def _run(self) -> None:
        self._start_grabber()
        if not self._make_tracker():
            return
        last_seq = 0
        errors_in_row = 0
        while not self._halt.is_set():
            self._apply_pending_settings()
            frame = self.grabber.wait_frame(last_seq, 0.2) if self.grabber else None
            now = self.clock()
            if frame is None:
                self._no_frame(now)
                continue
            last_seq = frame.seq
            self.loop_started = now
            try:
                self._process(frame.image, frame.captured_at)
                errors_in_row = 0
            except Exception:
                errors_in_row += 1
                log.exception("Frame processing failed")
                self._release_everything()
                if errors_in_row >= 30:
                    raise
            finally:
                self.loop_started = None

    def _tracker_factory(self) -> Callable[[], Any]:
        return self._tracker_factory_override or TRACKER_FACTORIES[self.settings.tracking_mode]

    def _make_tracker(self) -> bool:
        try:
            self.tracker = self._tracker_factory()()
            return True
        except Exception as exc:
            log.exception("Tracker failed to start")
            self.post("fatal", str(exc))
            return False

    def _start_grabber(self) -> None:
        if self.grabber is not None:
            self.grabber.stop()   # a grabber stuck in a driver call is abandoned, not waited on
        s = self.settings
        source = make_source(s.camera, s.camera_resolution, s.camera_zoom, s.stream_url, self.cameras)
        self.grabber = self.grabber_factory(source, self._on_camera_status)
        self.grabber.start()

    def _on_camera_status(self, state: str, detail: Any) -> None:
        # Called from the camera thread.
        self._camera_state, self._camera_detail = state, detail
        if state == "connected" and isinstance(detail, CameraCapabilities):
            self._label = "Look at the camera" if self.settings.tracking_mode == "eye" else "Show one hand to the camera"
        elif state == "error":
            self._label = str(detail)
        elif state == "reconnecting":
            self._label = str(detail)
        self.post("camera", (state, detail))
        self._publish()

    # -- settings -----------------------------------------------------------
    def _apply_pending_settings(self) -> None:
        with self._settings_lock:
            new, self._pending_settings = self._pending_settings, None
        if new is None:
            return
        old = self.settings
        self.settings = new
        self.pinch.configure(new.pinch_threshold / 100, new.release_threshold / 100,
                             new.pinch_hold_ms / 1000, new.click_stability, new.drag_enabled)
        self.filter.set_smoothing(new.smoothing)
        self.scroll.configure(new.scroll_sensitivity, new.scroll_dead_zone / 100, new.scroll_reverse)
        self.hide.hold_seconds = new.hide_gesture_hold_ms / 1000
        self.gaze_filter.set_smoothing(new.eye_smoothing)
        self.dwell.configure(new.eye_dwell_ms / 1000, new.eye_dwell_radius / 100)
        if new.eye_calibration != old.eye_calibration:
            self.gaze_calibration = GazeCalibration.from_json(new.eye_calibration)
        self.output.pause_on_physical_mouse = new.pause_on_physical_mouse
        if not new.click_enabled and old.click_enabled:
            self._send(self.pinch.cancel())
        if new.screen != old.screen:
            self._desktop_checked = -1e9
        if new.tracking_mode != old.tracking_mode:
            # A different model entirely: let go of everything, close the old
            # tracker, open the new one, and reset filters tuned for the mode
            # that just left so the new mode doesn't inherit a stale gesture.
            self._release_everything()
            self.filter.reset()
            self.gaze_filter.reset()
            self._label = "Switching tracking mode…"
            self._rebuild_tracker()
        if any(getattr(new, k) != getattr(old, k) for k in CAMERA_KEYS):
            # Switching cameras: let go of everything first, then switch.
            self._release_everything()
            self._label = "Switching camera…"
            self._start_grabber()
        elif new.camera_zoom != old.camera_zoom and new.camera_zoom >= 0:
            self.camera_command("zoom", new.camera_zoom)

    # -- per frame ----------------------------------------------------------
    def _no_frame(self, now: float) -> None:
        """No new frame within the wait: treat as no hand/face, and watch for a dead camera."""
        if self.settings.tracking_mode == "eye":
            self._eye_absent(now)
        else:
            self._hand_absent(now)
        g = self.grabber
        if g is None or self._halt.is_set():
            return
        age = g.latest_age()
        if g.connected and age is not None and age > CAMERA_STALL_AFTER:
            log.warning("No camera frame for %.1f s; restarting the camera", age)
            self._label = "The camera stopped responding. Reconnecting…"
            self.post("camera", ("reconnecting", self._label))
            self._start_grabber()
        elif not g.is_alive() and not g.stopped:
            self._start_grabber()
        self._publish()

    def _desktop_rect(self, now: float) -> tuple[int, int, int, int]:
        if now - self._desktop_checked > 2.0:   # screens can be plugged in at any time
            try:
                rect = self.output.backend.desktop_rect(self.settings.screen)
                if rect[2] > 0 and rect[3] > 0:
                    self._desktop = rect
            except Exception:
                log.exception("Couldn't read the desktop size")
            self._desktop_checked = now
        return self._desktop

    def _process(self, image: np.ndarray, captured_at: float) -> None:
        s = self.settings
        if s.mirror:
            image = cv2.flip(image, 1)
        h, w = image.shape[:2]
        small = image
        if w > INFERENCE_WIDTH:
            scale = INFERENCE_WIDTH / w
            small = cv2.resize(image, (INFERENCE_WIDTH, max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)

        self.infer_started = self.clock()
        result = None
        try:
            result = self.tracker.process(rgb)
            self.inference_errors = 0
        except Exception:
            self.inference_errors += 1
            log.exception("Tracking inference failed (%d in a row)", self.inference_errors)
            if self.inference_errors >= 3:
                self._rebuild_tracker()
        finally:
            done = self.clock()
            self.infer_ms = (done - self.infer_started) * 1000
            self.infer_started = None

        now = self.clock()
        self.frames_processed += 1
        self._frame_times = [t for t in self._frame_times if t > now - 2.0] + [now]
        if len(self._frame_times) > 1:
            self.loop_fps = (len(self._frame_times) - 1) / (self._frame_times[-1] - self._frame_times[0])
        if now - captured_at > STALE_AFTER:
            # Too old to act on: the pointer would jump to where the hand *was*.
            self.stale_frames += 1
            result = None

        if s.tracking_mode == "eye":
            landmarks, blendshapes = result if result is not None else (None, None)
            measure = eye_pose.measure(landmarks, blendshapes, w / h) if landmarks is not None else None
            if measure is None:
                self._eye_absent(now)
            else:
                self._eye_present(measure, now)
        else:
            landmarks = result
            measure = hand_pose.measure(landmarks, w / h) if landmarks is not None else None
            if measure is None:
                self._hand_absent(now)
            else:
                self._hand_present(measure, now)
        self._maybe_preview(image, landmarks, measure, now)

    def _rebuild_tracker(self) -> None:
        log.warning("Rebuilding the tracker after repeated errors")
        try:
            if self.tracker is not None:
                self.tracker.close()
        except Exception:
            pass
        try:
            self.tracker = self._tracker_factory()()
            self.inference_errors = 0
        except Exception:
            log.exception("Tracker rebuild failed")

    def _hand_absent(self, now: float) -> None:
        self._hand_frames = 0
        actions = self.pinch.update(None, None, now)
        self._send(actions)
        if any(a.name == "lost" for a in actions) or self.pinch.state == "idle":
            self.filter.reset()
            self.stabilizer.reset()
        self.scroll.reset()
        self.hide.update(False, now)
        if self._camera_state == "connected":
            self._gesture, self._label = "no_hand", "Show one hand to the camera"

    def _hand_present(self, m: hand_pose.HandMeasure, now: float) -> None:
        s = self.settings
        self._hand_frames += 1
        left, top, width, height = self._desktop_rect(now)

        # Map the fingertip from the "reach" area of the frame onto the screen.
        margin = (1 - s.reach / 100) / 2
        span = max(s.reach / 100, 1e-3)
        nx = min(1.0, max(0.0, (m.index_tip[0] - margin) / span))
        ny = min(1.0, max(0.0, (m.index_tip[1] - margin) / span))

        paused = self.output.paused
        if paused and self.pinch.active:
            self._send(self.pinch.cancel())

        fist = all(state == hand_pose.FOLDED for state in m.fingers.values())
        ratio = m.pinch_ratio if s.click_enabled and not paused else None
        fx, fy = self.filter(nx, ny, now, self.pinch.approach(ratio))
        target = (left + fx * (width - 1), top + fy * (height - 1))
        pixel = (round(target[0]), round(target[1]))

        # Scrolling: only from an open, steady hand that isn't mid-pinch.
        steady = self._hand_frames >= 6
        scroll_allowed = (s.scroll_enabled and not paused and steady
                          and self.pinch.state in ("idle", "ready")
                          and m.pinch_ratio >= self.pinch.release_threshold)
        pose = hand_pose.is_scroll_pose(m, s.scroll_pose)
        notches = self.scroll.update(pose, hand_pose.scroll_anchor_y(m, s.scroll_pose), m.scale,
                                     scroll_allowed, now)

        actions = []
        if s.click_enabled and not paused and not self.scroll.active:
            actions = self.pinch.update(ratio, pixel, now, hold_still=fist)
        if self.scroll.active and self._last_cursor is not None:
            # The pointer stays where scrolling began (scrolling acts under it)
            # and eases back to the hand afterwards instead of jumping.
            position = self.stabilizer.update(target, "frozen", self._last_cursor, now)
        else:
            position = self.stabilizer.update(target, self.pinch.state, self.pinch.locked_position, now)
        position = (min(left + width - 1, max(left, position[0])), min(top + height - 1, max(top, position[1])))

        # The optional hide gesture: its own pose, held, from a calm hand.
        hide_fired = False
        if s.hide_gesture_enabled and not self.pinch.active and not self.scroll.active:
            hide_fired = self.hide.update(hand_pose.is_hide_pose(m), now)
        else:
            self.hide.update(False, now)

        self._send(actions)
        if hide_fired:
            self._release_everything()
            self.post("hide_requested", s.hide_gesture_action)
        elif self.scroll.active:
            if notches:
                self.output.scroll(notches)
        elif not paused and position != self._last_cursor:
            self.output.move(*position)
        self._last_cursor = position
        self._update_label(m, now, paused)

    def _eye_absent(self, now: float) -> None:
        self._face_frames = 0
        self.gaze_filter.reset()
        self.dwell.reset()
        self._blink_since = None
        self._blink_fired = False
        if self._camera_state == "connected":
            self._gesture, self._label = "no_face", "Look at the camera"

    def _eye_present(self, m: eye_pose.EyeMeasure, now: float) -> None:
        s = self.settings
        self._face_frames += 1
        self.last_gaze_offset = m.offset
        left, top, width, height = self._desktop_rect(now)
        paused = self.output.paused

        screen_pt = self.gaze_calibration.apply(m.offset)
        if screen_pt is None:
            self.dwell.reset()
            self._blink_since = None
            self._update_eye_label(False, now, paused)
            return

        fx, fy = self.gaze_filter(screen_pt[0], screen_pt[1], now)
        target = (left + fx * (width - 1), top + fy * (height - 1))
        position = (min(left + width - 1, max(left, round(target[0]))),
                   min(top + height - 1, max(top, round(target[1]))))

        dwell_clicked = False
        if s.eye_click_mode in ("dwell", "both") and not paused:
            dwell_clicked = self.dwell.update((fx, fy), now)
        else:
            self.dwell.reset()

        # A deliberate blink held for eye_blink_ms, not the quick blinks
        # everyone does while just looking around.
        blink_clicked = False
        if s.eye_click_mode in ("blink", "both") and not paused and m.blink > 0.6:
            if self._blink_since is None:
                self._blink_since = now
            elif not self._blink_fired and now - self._blink_since >= s.eye_blink_ms / 1000:
                blink_clicked = True
                self._blink_fired = True
        else:
            self._blink_since = None
            self._blink_fired = False

        if not paused and position != self._last_cursor:
            self.output.move(*position)
        self._last_cursor = position
        if (dwell_clicked or blink_clicked) and not paused:
            self.output.click(*position)
            self.post("gesture", "click")
        self._update_eye_label(True, now, paused)

    def _update_eye_label(self, calibrated: bool, now: float, paused: bool) -> None:
        if paused:
            g, text = "paused", "Paused while you use the mouse"
        elif not calibrated:
            g, text = "uncalibrated", "Not calibrated yet — Settings → Eye tracking → Calibrate"
        elif self.dwell.progress(now) > 0:
            g, text = "dwelling", f"Hold your gaze… {int(self.dwell.progress(now) * 100)}%"
        else:
            g, text = "gazing", "Tracking your gaze"
        self._gesture, self._label = g, text

    def _send(self, actions) -> None:
        for a in actions:
            if a.name == "click":
                self.output.click(a.x, a.y)
            elif a.name == "mouse_down":
                self.output.press(a.x, a.y)
            elif a.name == "mouse_up":
                self.output.release()
            elif a.name == "pinch_started":
                self.output.move(a.x, a.y)   # the pointer sits on the locked point
            if a.name in ("click", "mouse_down", "mouse_up", "cancelled"):
                self.post("gesture", a.name)

    def _release_everything(self) -> None:
        self._send(self.pinch.cancel())
        self.scroll.reset()
        self.hide.reset()
        self.dwell.reset()
        self.output.release_all()

    def _update_label(self, m: hand_pose.HandMeasure, now: float, paused: bool) -> None:
        state = self.pinch.state
        if paused:
            g, text = "paused", "Paused while you use the mouse"
        elif self.scroll.active:
            g, text = "scrolling", "Scrolling — move up or down; lower your fingers to stop"
        elif self.hide.progress(now) > 0:
            g, text = "hide", f"Hold to hide Finger Mouse… {int(self.hide.progress(now) * 100)}%"
        elif state == "dragging":
            g, text = "dragging", "Dragging — open your fingers to drop"
        elif state in ("closing", "pressed"):
            g, text = "pinched", ("Pinch locked — release to click, hold to drag"
                                  if self.settings.drag_enabled else "Clicked")
        elif state == "ready":
            g, text = "ready", "Ready — pinch thumb and index to click"
        elif not self.settings.click_enabled:
            g, text = "pointing", "Pointing (clicking is off)"
        else:
            g, text = "open", "Open thumb and index to get ready"
        self._gesture, self._label = g, text

    # -- preview & snapshot -------------------------------------------------
    def _maybe_preview(self, image: np.ndarray, landmarks, m, now: float) -> None:
        if now - self._last_preview < 1 / PREVIEW_FPS:
            self._publish()
            return
        self._last_preview = now
        s = self.settings
        h, w = image.shape[:2]
        width = max(160, min(960, int(self.preview_width)))
        preview = cv2.resize(image, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
        ph, pw = preview.shape[:2]
        if s.tracking_mode == "eye":
            if landmarks is not None and s.show_landmarks:
                color = {"dwelling": (60, 220, 120), "gazing": (255, 200, 60)}.get(self._gesture, (180, 180, 180))
                draw_eye_points(preview, landmarks, color)
        else:
            if landmarks is not None and s.show_landmarks:
                draw_landmarks(preview, landmarks)
            if landmarks is not None and m is not None:
                color = {"pinched": (60, 220, 120), "dragging": (60, 180, 255), "ready": (255, 200, 60)}.get(
                    self._gesture, (180, 180, 180))
                a = (int(landmarks[4].x * pw), int(landmarks[4].y * ph))
                b = (int(landmarks[8].x * pw), int(landmarks[8].y * ph))
                cv2.line(preview, a, b, color, 2, cv2.LINE_AA)
                # The reach area: the part of the frame that spans the screen.
                margin = (1 - s.reach / 100) / 2
                cv2.rectangle(preview, (int(margin * pw), int(margin * ph)),
                              (int((1 - margin) * pw), int((1 - margin) * ph)), (90, 90, 90), 1, cv2.LINE_AA)
        if s.show_gesture_state:
            cv2.rectangle(preview, (0, 0), (pw, 30), (0, 0, 0), -1)
            cv2.putText(preview, self._label[:70], (10, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (240, 240, 240), 1, cv2.LINE_AA)
        self._preview_seq += 1
        self._publish(cv2.cvtColor(preview, cv2.COLOR_BGR2RGB))

    def _camera_fps(self, g: Optional[FrameGrabber]) -> float:
        """Frames per second arriving from the camera, over the last ~2 s."""
        if g is None:
            return 0.0
        now = self.clock()
        self._camera_samples = [(t, n) for t, n in getattr(self, "_camera_samples", []) if t > now - 2.0]
        self._camera_samples.append((now, g.frames))
        (t0, n0), (t1, n1) = self._camera_samples[0], self._camera_samples[-1]
        return round((n1 - n0) / (t1 - t0), 1) if t1 > t0 and n1 >= n0 else 0.0

    def _publish(self, preview: Optional[np.ndarray] = None) -> None:
        g = self.grabber
        caps = g.capabilities if g else None
        self.view = View(
            preview=preview if preview is not None else self.view.preview,
            preview_seq=self._preview_seq,
            cursor=self._last_cursor,
            gesture=self._gesture,
            label=self._label,
            tracked=self._hand_frames > 0 or self._face_frames > 0,
            camera_state=self._camera_state,
            camera_detail=caps if caps is not None else self._camera_detail,
            diagnostics={
                "camera_fps": self._camera_fps(g),
                "frames": g.frames if g else 0,
                "dropped": g.dropped if g else 0,
                "frame_age_ms": round((g.latest_age() or 0) * 1000) if g else None,
                "inference_ms": round(self.infer_ms, 1),
                "tracking_fps": round(self.loop_fps, 1),
                "stale": self.stale_frames,
                "output_queue": self.output.queue_depth(),
                "output_errors": self.output.errors,
            },
        )
