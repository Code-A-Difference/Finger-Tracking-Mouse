"""Cameras for Finger Mouse: finding them, opening them, and reading them
without ever blocking the rest of the app.

* ``list_cameras()`` names the webcams the way the capture backend numbers
  them — DirectShow on Windows, V4L2 on Linux, AVFoundation on macOS — so the
  list says "Integrated Webcam", not "Camera 0", wherever the OS will say.
* ``CameraSource`` is the interface every input implements. ``OpenCVCamera``
  is a webcam; ``NetworkStreamCamera`` reads an MJPEG/RTSP stream, which is
  how phone-camera apps (IP Webcam, DroidCam and similar) publish video over
  Wi-Fi. A future native phone link would be another ``CameraSource``.
* ``FrameGrabber`` runs a source on its own thread and keeps only the newest
  frame. Reading a webcam can block for seconds when a driver hiccups; with
  the grabber, that stalls nothing but the grabber, and the tracker simply
  sees no new frame, releases the mouse and waits while the grabber
  reconnects.

Nothing here claims a camera can do more than its driver reports: zoom is
offered only if the driver accepts it, resolutions are what the camera
actually delivers, and field of view is fixed by the lens.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Some Windows machines take many seconds to open a camera through Media
# Foundation's hardware transforms; this avoids that path without other effect.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2  # noqa: E402  (after the environment tweak above)
import numpy as np  # noqa: E402


@dataclass(frozen=True)
class CameraInfo:
    id: str            # "cv:<index>" or "url"
    label: str
    index: int = -1

    @property
    def virtual(self) -> bool:
        return is_virtual_camera(self.label)

    @property
    def display_name(self) -> str:
        return f"{self.label} (virtual)" if self.virtual else self.label


# Software cameras (streaming tools, video-call effects, phone bridges) show
# up as webcams too, often ahead of the real one, and many send a blank or
# "no signal" picture until something feeds them. Automatic mode tries real
# cameras first. Phone bridges are still perfectly usable when picked.
_VIRTUAL_HINTS = ("virtual", "ndi", "obs", "droidcam", "iriun", "epoccam", "camo", "snap camera",
                  "manycam", "xsplit", "splitcam", "vcam", "nvidia broadcast", "mmhmm",
                  "streamlabs", "avatarify", "e2esoft", "continuity")


def is_virtual_camera(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _VIRTUAL_HINTS)


@dataclass
class Frame:
    image: np.ndarray  # BGR
    captured_at: float # time.monotonic() when it arrived
    seq: int


@dataclass
class CameraCapabilities:
    """What the open camera really offers. Filled in after it opens."""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    backend: str = ""
    zoom: Optional[float] = None          # current zoom if the driver lets us change it
    driver_settings: bool = False         # Windows: the driver has its own settings window
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Listing cameras
# ---------------------------------------------------------------------------

def list_cameras() -> list[CameraInfo]:
    """Cameras in the order OpenCV numbers them, with names where the OS gives them.

    Doesn't open any camera, so it's quick and never turns on a camera light.
    """
    try:
        if sys.platform == "win32":
            names = _directshow_names()
        elif sys.platform.startswith("linux"):
            return _v4l2_cameras()
        elif sys.platform == "darwin":
            names = _avfoundation_names()
        else:
            names = []
    except Exception:
        log.exception("Could not list cameras")
        names = []
    return [CameraInfo(f"cv:{i}", name or f"Camera {i}", i) for i, name in enumerate(names)]


def probe_camera_indices(limit: int = 6) -> list[CameraInfo]:
    """Fallback when the OS can't list cameras: try opening indices 0..limit-1.

    Briefly turns each camera on, so only run it when the person asks
    ("Refresh") and tracking is stopped.
    """
    found = []
    for i in range(limit):
        cap = cv2.VideoCapture(i, _backend_for_platform())
        try:
            if cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    found.append(CameraInfo(f"cv:{i}", f"Camera {i}", i))
        finally:
            cap.release()
    return found


def _directshow_names() -> list[str]:
    """Video capture devices via DirectShow — the same list and order
    OpenCV's CAP_DSHOW backend uses for its indices."""
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.OleDLL("ole32")
    oleaut32 = ctypes.WinDLL("oleaut32")

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def guid(text: str) -> GUID:
        g = GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g

    class VARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("value", ctypes.c_void_p), ("extra", ctypes.c_void_p)]

    def call(obj: ctypes.c_void_p, index: int, *args: tuple[Any, Any]) -> int:
        """Call method ``index`` of a COM object's vtable; returns the HRESULT."""
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *(t for t, _ in args))
        return proto(vtable[index])(obj, *(v for _, v in args))

    def release(obj: ctypes.c_void_p) -> None:
        if obj:
            call(obj, 2)

    COINIT_APARTMENTTHREADED, RPC_E_CHANGED_MODE = 0x2, -2147417850
    try:
        hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        initialised = True
    except OSError as exc:  # already initialised in another mode on this thread: fine
        initialised = False
        if getattr(exc, "winerror", None) not in (RPC_E_CHANGED_MODE, None):
            raise
    names: list[str] = []
    dev_enum = ctypes.c_void_p()
    enum_moniker = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(guid("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")), None, 1,
                               ctypes.byref(guid("{29840822-5B84-11D0-BD3B-00A0C911CE86}")),
                               ctypes.byref(dev_enum))
        category = guid("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")   # video input devices
        hr = call(dev_enum, 3, (ctypes.POINTER(GUID), ctypes.byref(category)),
                  (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(enum_moniker)), (wintypes.DWORD, 0))
        if hr != 0 or not enum_moniker:   # S_FALSE: the category is empty
            return []
        iid_bag = guid("{55272A00-42CB-11CE-8135-00AA004BB851}")
        while True:
            moniker = ctypes.c_void_p()
            fetched = wintypes.ULONG()
            if call(enum_moniker, 3, (wintypes.ULONG, 1), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(moniker)),
                    (ctypes.POINTER(wintypes.ULONG), ctypes.byref(fetched))) != 0 or not fetched.value:
                break
            bag = ctypes.c_void_p()
            name = ""
            try:
                # IMoniker::BindToStorage is vtable slot 9
                if call(moniker, 9, (ctypes.c_void_p, None), (ctypes.c_void_p, None),
                        (ctypes.POINTER(GUID), ctypes.byref(iid_bag)),
                        (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(bag))) == 0:
                    var = VARIANT()
                    if call(bag, 3, (ctypes.c_wchar_p, "FriendlyName"), (ctypes.POINTER(VARIANT), ctypes.byref(var)),
                            (ctypes.c_void_p, None)) == 0 and var.vt == 8:   # VT_BSTR
                        name = ctypes.wstring_at(var.value)
                    oleaut32.VariantClear(ctypes.byref(var))
            finally:
                release(bag)
                release(moniker)
            names.append(name)
    finally:
        release(enum_moniker)
        release(dev_enum)
        if initialised:
            ole32.CoUninitialize()
    return names


def _v4l2_cameras() -> list[CameraInfo]:
    """Linux: /sys names each /dev/videoN; index 0 of a device is its capture node."""
    root = "/sys/class/video4linux"
    cams = []
    try:
        entries = sorted(os.listdir(root), key=lambda n: int(n[5:]) if n[5:].isdigit() else 999)
    except OSError:
        return []
    for entry in entries:
        if not entry.startswith("video") or not entry[5:].isdigit():
            continue
        try:
            if open(f"{root}/{entry}/index").read().strip() not in ("", "0"):
                continue   # metadata node of the same camera
            name = open(f"{root}/{entry}/name").read().strip()
        except OSError:
            name = ""
        n = int(entry[5:])
        cams.append(CameraInfo(f"cv:{n}", name or f"Camera {n}", n))
    return cams


def _avfoundation_names() -> list[str]:
    """macOS: OpenCV's AVFoundation backend numbers cameras sorted by uniqueID."""
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeVideo  # pyobjc-framework-AVFoundation
    except ImportError:
        return []
    devices = list(AVCaptureDevice.devicesWithMediaType_(AVMediaTypeVideo) or [])
    devices.sort(key=lambda d: str(d.uniqueID()))
    return [str(d.localizedName()) for d in devices]


def _backend_for_platform() -> int:
    if sys.platform == "win32":
        return cv2.CAP_DSHOW
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_V4L2


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class CameraSource:
    """One video input. ``read`` may block; only a FrameGrabber calls it."""

    label = "camera"

    def open(self) -> CameraCapabilities:
        raise NotImplementedError

    def read(self) -> Optional[np.ndarray]:
        raise NotImplementedError

    def release(self) -> None:
        pass

    def set_zoom(self, value: float) -> Optional[float]:
        return None

    def show_driver_settings(self) -> bool:
        return False


def parse_resolution(text: str) -> Optional[tuple[int, int]]:
    try:
        w, h = (int(v) for v in text.lower().split("x"))
        return (w, h) if w > 0 and h > 0 else None
    except ValueError:
        return None


class OpenCVCamera(CameraSource):
    """A webcam by OpenCV index."""

    DEFAULT_REQUEST = (1280, 720)   # many webcams show their widest view in 16:9

    def __init__(self, index: int, resolution: str = "auto", zoom: float = -1.0, label: str = "") -> None:
        self.index = index
        self.resolution = resolution
        self.zoom = zoom
        self.label = label or f"Camera {index}"
        self.cap: Optional[cv2.VideoCapture] = None

    def _backends(self) -> list[int]:
        first = _backend_for_platform()
        return [first] if first == cv2.CAP_ANY else [first, cv2.CAP_ANY]

    def open(self) -> CameraCapabilities:
        last_error = "it did not respond"
        for api in self._backends():
            cap = cv2.VideoCapture(self.index, api)
            if not cap.isOpened():
                cap.release()
                continue
            want = parse_resolution(self.resolution) or self.DEFAULT_REQUEST
            if want[0] * want[1] > 640 * 480:
                # Uncompressed 720p over USB 2 is often capped near 10 fps;
                # MJPEG gets the full frame rate. Drivers that can't just ignore it.
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, want[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want[1])
            cap.set(cv2.CAP_PROP_FPS, 30)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ok, frame = cap.read()
            if not ok or frame is None or not frame.size:
                cap.release()
                last_error = "it opened but sent no picture"
                continue
            self.cap = cap
            return self._capabilities(frame, api)
        raise RuntimeError(f"{self.label} could not be used: {last_error}. "
                           "Check camera permissions and close other apps using it.")

    def _capabilities(self, frame: np.ndarray, api: int) -> CameraCapabilities:
        assert self.cap is not None
        caps = CameraCapabilities(width=frame.shape[1], height=frame.shape[0],
                                  fps=float(self.cap.get(cv2.CAP_PROP_FPS) or 0),
                                  backend=self.cap.getBackendName())
        requested = parse_resolution(self.resolution)
        if requested and requested != (caps.width, caps.height):
            caps.notes.append(f"Asked for {requested[0]}×{requested[1]}; the camera chose "
                              f"{caps.width}×{caps.height}.")
        caps.zoom = self._probe_zoom()
        if caps.zoom is not None and self.zoom >= 0:
            caps.zoom = self.set_zoom(self.zoom)
        caps.driver_settings = sys.platform == "win32" and api == cv2.CAP_DSHOW
        return caps

    def _probe_zoom(self) -> Optional[float]:
        """Zoom is offered only if the driver really changes it when asked."""
        cap = self.cap
        if cap is None:
            return None
        try:
            current = cap.get(cv2.CAP_PROP_ZOOM)
            if current is None or current < 0:
                return None
            trial = current + 1
            if not cap.set(cv2.CAP_PROP_ZOOM, trial) or abs(cap.get(cv2.CAP_PROP_ZOOM) - trial) > 0.5:
                cap.set(cv2.CAP_PROP_ZOOM, current)
                return None
            cap.set(cv2.CAP_PROP_ZOOM, current)
            return float(current)
        except cv2.error:
            return None

    def read(self) -> Optional[np.ndarray]:
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        return frame if ok and frame is not None and frame.size else None

    def release(self) -> None:
        if self.cap is not None:
            try:
                self.cap.release()
            finally:
                self.cap = None

    def set_zoom(self, value: float) -> Optional[float]:
        if self.cap is None:
            return None
        self.cap.set(cv2.CAP_PROP_ZOOM, float(value))
        return float(self.cap.get(cv2.CAP_PROP_ZOOM))

    def show_driver_settings(self) -> bool:
        # DirectShow opens the driver's own property window on a thread of
        # its own; nothing here waits for it.
        if self.cap is None or sys.platform != "win32":
            return False
        return bool(self.cap.set(cv2.CAP_PROP_SETTINGS, 1))


def validate_stream_url(url: str) -> Optional[str]:
    """None if the URL is usable, else a reason it isn't."""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return "That isn't a valid address."
    if parts.scheme not in ("http", "https", "rtsp"):
        return "Use an http://, https:// or rtsp:// address from your phone's camera app."
    if not parts.hostname:
        return "The address needs a host, like http://192.168.1.20:8080/video."
    return None


class NetworkStreamCamera(OpenCVCamera):
    """Video from a URL: phone camera apps on the same Wi-Fi, or IP cameras.

    Experimental. Latency depends on the network and the app; plain http
    streams aren't encrypted, so use them only on a network you trust.
    """

    def __init__(self, url: str) -> None:
        super().__init__(-1, "auto", -1.0, "Network camera")
        self.url = url.strip()

    def open(self) -> CameraCapabilities:
        problem = validate_stream_url(self.url)
        if problem:
            raise RuntimeError(problem)
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000]
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, params)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError("Couldn't connect to the stream. Check the address and that the phone "
                               "app is running on the same network.")
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            raise RuntimeError("Connected, but the stream sent no picture.")
        self.cap = cap
        return CameraCapabilities(width=frame.shape[1], height=frame.shape[0],
                                  fps=float(cap.get(cv2.CAP_PROP_FPS) or 0),
                                  backend="network stream",
                                  notes=["Phone and network cameras are experimental."])


def make_source(camera_id: str, resolution: str, zoom: float, stream_url: str,
                cameras: Optional[list[CameraInfo]] = None) -> CameraSource:
    if camera_id == "url":
        return NetworkStreamCamera(stream_url)
    if camera_id.startswith("cv:"):
        index = int(camera_id[3:])
        label = next((c.label for c in cameras or [] if c.index == index), "")
        return OpenCVCamera(index, resolution, zoom, label)
    return AutoCamera(resolution, zoom, cameras)


class AutoCamera(CameraSource):
    """"Automatic": the first listed camera that works."""

    def __init__(self, resolution: str, zoom: float, cameras: Optional[list[CameraInfo]]) -> None:
        self.resolution, self.zoom = resolution, zoom
        # Real cameras first, then virtual ones, each in the OS's order.
        ordered = sorted(cameras or [], key=lambda c: (c.virtual, c.index))
        self._candidates = [c.index for c in ordered] or list(range(4))
        self._current: Optional[OpenCVCamera] = None
        self._labels = {c.index: c.label for c in cameras or []}
        self.label = "Automatic"

    def open(self) -> CameraCapabilities:
        errors = []
        for index in self._candidates:
            cam = OpenCVCamera(index, self.resolution, self.zoom, self._labels.get(index, ""))
            try:
                caps = cam.open()
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            self._current = cam
            self.label = cam.label
            return caps
        raise RuntimeError("No working camera was found. Check camera permissions, close other "
                           "apps using the camera, or pick one in Settings.")

    def read(self) -> Optional[np.ndarray]:
        return self._current.read() if self._current else None

    def release(self) -> None:
        if self._current:
            self._current.release()

    def set_zoom(self, value: float) -> Optional[float]:
        return self._current.set_zoom(value) if self._current else None

    def show_driver_settings(self) -> bool:
        return self._current.show_driver_settings() if self._current else False


# ---------------------------------------------------------------------------
# The capture thread
# ---------------------------------------------------------------------------

class FrameGrabber(threading.Thread):
    """Reads a CameraSource on its own thread, keeping only the newest frame.

    ``on_status(state, detail)`` reports "opening", "connected" (detail is
    the CameraCapabilities), "error" and "reconnecting". The grabber keeps
    trying to reconnect with a growing pause until it's stopped.
    """

    READ_FAILURES_BEFORE_RECONNECT = 25

    def __init__(self, source: CameraSource, on_status: Callable[[str, Any], None]) -> None:
        super().__init__(name="camera", daemon=True)
        self.source = source
        self.on_status = on_status
        self._cond = threading.Condition()
        self._frame: Optional[Frame] = None
        self._halt = threading.Event()
        self._commands: list[tuple[str, Any]] = []
        self.capabilities: Optional[CameraCapabilities] = None
        self.frames = 0
        self.dropped = 0           # frames replaced before the tracker took them
        self.read_started: Optional[float] = None   # set while blocked in read()
        self.connected = False
        self._taken_seq = 0

    # -- called from other threads ------------------------------------------
    def stop(self) -> None:
        self._halt.set()
        with self._cond:
            self._cond.notify_all()

    @property
    def stopped(self) -> bool:
        return self._halt.is_set()

    def request(self, command: str, value: Any = None) -> None:
        """Run a camera command (zoom, driver settings) between reads."""
        with self._cond:
            self._commands.append((command, value))

    def wait_frame(self, after_seq: int, timeout: float) -> Optional[Frame]:
        """The newest frame newer than ``after_seq``, waiting up to ``timeout``."""
        end = time.monotonic() + timeout
        with self._cond:
            while (self._frame is None or self._frame.seq <= after_seq) and not self._halt.is_set():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            frame = self._frame
            if frame is not None and frame.seq > after_seq:
                self._taken_seq = frame.seq
                return frame
            return None

    def latest_age(self) -> Optional[float]:
        frame = self._frame
        return None if frame is None else time.monotonic() - frame.captured_at

    # -- the thread ---------------------------------------------------------
    def run(self) -> None:
        backoff = 0.5
        while not self._halt.is_set():
            self.on_status("opening", None)
            try:
                self.capabilities = self.source.open()
            except Exception as exc:
                log.warning("Camera open failed: %s", exc)
                self.on_status("error", str(exc))
                if self._halt.wait(backoff):
                    break
                backoff = min(backoff * 2, 5.0)
                continue
            backoff = 0.5
            self.connected = True
            self.on_status("connected", self.capabilities)
            self._read_loop()
            self.connected = False
            self.source.release()
            if not self._halt.is_set():
                self.on_status("reconnecting", "The camera stopped sending video. Reconnecting…")
                self._halt.wait(0.5)
        self.source.release()

    def _read_loop(self) -> None:
        failures = 0
        seq = self._frame.seq if self._frame else 0
        while not self._halt.is_set():
            self._run_commands()
            self.read_started = time.monotonic()
            try:
                image = self.source.read()
            except Exception:
                log.exception("Camera read failed")
                image = None
            finally:
                self.read_started = None
            if self._halt.is_set():
                return
            if image is None:
                failures += 1
                if failures >= self.READ_FAILURES_BEFORE_RECONNECT:
                    return
                time.sleep(0.02)
                continue
            failures = 0
            seq += 1
            self.frames += 1
            with self._cond:
                if self._frame is not None and self._frame.seq > self._taken_seq:
                    self.dropped += 1
                self._frame = Frame(image, time.monotonic(), seq)
                self._cond.notify_all()

    def _run_commands(self) -> None:
        with self._cond:
            commands, self._commands = self._commands, []
        for command, value in commands:
            try:
                if command == "zoom":
                    result = self.source.set_zoom(float(value))
                    if self.capabilities is not None:
                        self.capabilities.zoom = result
                elif command == "driver_settings":
                    self.source.show_driver_settings()
            except Exception:
                log.exception("Camera command %s failed", command)


def probe_resolutions(source: OpenCVCamera, candidates: tuple[str, ...]) -> list[str]:
    """Ask the camera for each resolution and keep the ones it really delivers.

    Opens the camera; run it only while tracking is stopped.
    """
    found: list[str] = []
    for text in candidates:
        want = parse_resolution(text)
        if not want:
            continue
        probe = OpenCVCamera(source.index, text, -1.0, source.label)
        try:
            caps = probe.open()
            got = f"{caps.width}x{caps.height}"
            if got not in found:
                found.append(got)
        except RuntimeError:
            break
        finally:
            probe.release()
    return found
