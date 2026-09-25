"""The capture thread: newest frame only, and recovery from a camera that stalls."""

import threading
import time

import numpy as np
import pytest

import camera
from camera import CameraCapabilities, CameraSource, FrameGrabber


class FakeSource(CameraSource):
    def __init__(self, fps=100, fail_open=0, stall_after=None, die_after=None):
        self.fps = fps
        self.fail_open = fail_open
        self.stall_after = stall_after      # block in read() after this many frames
        self.die_after = die_after          # return None forever after this many frames
        self.opens = 0
        self.reads = 0
        self.release_count = 0
        self.unblock = threading.Event()

    def open(self):
        self.opens += 1
        if self.opens <= self.fail_open:
            raise RuntimeError("camera busy")
        self.reads = 0
        return CameraCapabilities(width=64, height=48, backend="fake")

    def read(self):
        self.reads += 1
        if self.stall_after is not None and self.reads > self.stall_after:
            self.unblock.wait(10)            # a driver that hangs
            return None
        if self.die_after is not None and self.reads > self.die_after:
            return None
        time.sleep(1 / self.fps)
        return np.full((48, 64, 3), self.reads % 255, np.uint8)

    def release(self):
        self.release_count += 1


def wait_until(check, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


def test_keeps_only_the_newest_frame_and_counts_what_it_dropped():
    statuses = []
    g = FrameGrabber(FakeSource(fps=200), lambda s, d: statuses.append(s))
    g.start()
    try:
        first = g.wait_frame(0, 2)
        time.sleep(0.2)                               # a slow tracker…
        latest = g.wait_frame(first.seq, 2)
        assert latest.seq > first.seq + 5              # …gets the newest, not the backlog
        assert g.dropped > 0
        assert "connected" in statuses
    finally:
        g.stop()
        g.join(2)


def test_wait_frame_times_out_instead_of_hanging():
    g = FrameGrabber(FakeSource(stall_after=1), lambda s, d: None)
    g.start()
    try:
        first = g.wait_frame(0, 2)
        started = time.monotonic()
        assert g.wait_frame(first.seq, 0.2) is None
        assert time.monotonic() - started < 0.5
        assert g.read_started is not None             # the watchdog can see the stuck read
    finally:
        g.source.unblock.set()
        g.stop()
        g.join(2)


def test_retries_opening_until_the_camera_is_free():
    statuses = []
    source = FakeSource(fail_open=2)
    g = FrameGrabber(source, lambda s, d: statuses.append(s))
    g.start()
    try:
        assert g.wait_frame(0, 5) is not None
        assert statuses.count("error") == 2 and "connected" in statuses
    finally:
        g.stop()
        g.join(2)


def test_reconnects_when_the_camera_stops_sending():
    statuses = []
    source = FakeSource(die_after=5)
    g = FrameGrabber(source, lambda s, d: statuses.append(s))
    g.start()
    try:
        assert wait_until(lambda: source.opens >= 2, 5)
        assert "reconnecting" in statuses
        assert source.release_count >= 1
    finally:
        g.stop()
        g.join(2)


def test_stream_urls_are_checked():
    assert camera.validate_stream_url("http://192.168.1.20:8080/video") is None
    assert camera.validate_stream_url("rtsp://phone.local/live") is None
    assert camera.validate_stream_url("file:///etc/passwd")
    assert camera.validate_stream_url("javascript:alert(1)")
    assert camera.validate_stream_url("http://")


def test_virtual_cameras_are_recognised_and_tried_last():
    cams = [camera.CameraInfo("cv:0", "NDI Webcam Video 1", 0),
            camera.CameraInfo("cv:1", "OBS Virtual Camera", 1),
            camera.CameraInfo("cv:2", "Integrated Camera", 2),
            camera.CameraInfo("cv:3", "Logitech BRIO", 3)]
    assert [c.virtual for c in cams] == [True, True, False, False]
    auto = camera.AutoCamera("auto", -1, cams)
    assert auto._candidates == [2, 3, 0, 1]


@pytest.mark.skipif(not hasattr(camera, "_directshow_names") or __import__("sys").platform != "win32",
                    reason="Windows only")
def test_directshow_listing_does_not_crash():
    cams = camera.list_cameras()
    assert all(c.id == f"cv:{c.index}" for c in cams)
