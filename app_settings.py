"""Finger Mouse settings: defaults, validation, migration, and storage.

Settings live in one JSON file in the per-user config folder
(``%APPDATA%\\Finger Mouse`` on Windows, ``~/Library/Application Support/Finger
Mouse`` on macOS, ``$XDG_CONFIG_HOME/Finger Mouse`` elsewhere). Every value is
checked on load, so a hand-edited or half-written file can never stop the app
from starting — anything unreadable falls back to its default.

No Qt or camera imports here, so it's usable from tests and the tracking
thread alike.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from app_version import APP_NAME
from gesture_state import GazeCalibration

SETTINGS_VERSION = 2
log = logging.getLogger(__name__)

# Allowed values for the choice settings, first one is the default.
SCREEN_MODES = ("primary", "all")
SCROLL_POSES = ("two_fingers", "index")
HIDE_ACTIONS = ("tray", "minimize", "quit")
RESOLUTIONS = ("auto", "640x480", "800x600", "960x540", "1280x720", "1280x960", "1920x1080")
TRACKING_MODES = ("hand", "eye")
EYE_CLICK_MODES = ("dwell", "blink", "both")


@dataclass
class Settings:
    # ---- tracking mode --------------------------------------------------
    tracking_mode: str = "hand"      # "hand" (pinch/drag/scroll) or "eye" (gaze pointer, dwell/blink click)

    # ---- camera -------------------------------------------------------
    camera: str = "auto"            # "auto", "cv:<index>" or "url" (uses stream_url)
    camera_resolution: str = "auto"  # one of RESOLUTIONS; the driver has the final say
    camera_zoom: float = -1.0        # -1 = leave the driver's zoom alone
    stream_url: str = ""             # phone / network camera stream (experimental)
    mirror: bool = True              # selfie view: move right, pointer goes right

    # ---- pointer ------------------------------------------------------
    screen: str = "primary"          # which area the hand maps onto
    reach: int = 80                  # % of the camera frame that spans the whole screen
    smoothing: int = 50              # 0 = raw and twitchy, 100 = very steady but laggy
    halo_size: int = 36              # px, the on-screen tracking ring
    show_halo: bool = True
    pause_on_physical_mouse: bool = True

    # ---- click & drag -------------------------------------------------
    click_enabled: bool = True
    pinch_threshold: int = 30        # thumb–index gap, % of hand size, that counts as a pinch
    pinch_release_gap: int = 14      # how much further apart they must open to release
    click_stability: int = 2         # frames the pinch must hold before it counts
    drag_enabled: bool = True
    pinch_hold_ms: int = 420         # hold a pinch this long to press-and-hold (drag)

    # ---- scrolling ----------------------------------------------------
    scroll_enabled: bool = True
    scroll_pose: str = "two_fingers"
    scroll_sensitivity: int = 35
    scroll_dead_zone: int = 25       # % of hand size to move before scrolling starts
    scroll_reverse: bool = False

    # ---- eye tracking ---------------------------------------------------
    eye_smoothing: int = 65          # like `smoothing`, but gaze needs steadier defaults — it's noisier
    eye_click_mode: str = "dwell"    # "dwell" (look and hold), "blink" or "both"
    eye_dwell_ms: int = 700          # how long a steady gaze takes to click
    eye_dwell_radius: int = 4        # % of screen width the gaze may drift and still count as "steady"
    eye_blink_ms: int = 250          # how long an eye must stay shut to count as a deliberate blink
    eye_calibration: str = ""        # GazeCalibration.to_json(); "" = not calibrated yet

    # ---- optional hide gesture ----------------------------------------
    hide_gesture_enabled: bool = False
    hide_gesture_confirmed: bool = False  # the user has read and accepted the warning
    hide_gesture_action: str = "tray"
    hide_gesture_hold_ms: int = 1200

    # ---- debugging ----------------------------------------------------
    show_landmarks: bool = True
    show_gesture_state: bool = True
    show_diagnostics: bool = False
    verbose_logging: bool = False

    version: int = SETTINGS_VERSION

    # ------------------------------------------------------------------
    @property
    def release_threshold(self) -> int:
        return self.pinch_threshold + self.pinch_release_gap

    def copy(self, **changes: Any) -> "Settings":
        data = asdict(self)
        data.update(changes)
        return validate(data)


# (minimum, maximum) for every integer / float setting.
_RANGES: dict[str, tuple[float, float]] = {
    "camera_zoom": (-1.0, 1000.0),
    "reach": (50, 100),
    "smoothing": (0, 100),
    "halo_size": (16, 80),
    "pinch_threshold": (10, 60),
    "pinch_release_gap": (5, 40),
    "click_stability": (1, 5),
    "pinch_hold_ms": (150, 1500),
    "scroll_sensitivity": (5, 100),
    "scroll_dead_zone": (5, 60),
    "hide_gesture_hold_ms": (600, 3000),
    "eye_smoothing": (0, 100),
    "eye_dwell_ms": (300, 2500),
    "eye_dwell_radius": (1, 15),
    "eye_blink_ms": (100, 800),
}
_CHOICES: dict[str, tuple[str, ...]] = {
    "screen": SCREEN_MODES,
    "scroll_pose": SCROLL_POSES,
    "hide_gesture_action": HIDE_ACTIONS,
    "camera_resolution": RESOLUTIONS,
    "tracking_mode": TRACKING_MODES,
    "eye_click_mode": EYE_CLICK_MODES,
}


def validate(data: dict[str, Any]) -> Settings:
    """Build a Settings from untrusted data: clamp numbers, reject bad choices."""
    defaults = Settings()
    clean: dict[str, Any] = {}
    for f in fields(Settings):
        name = f.name
        fallback = getattr(defaults, name)
        value = data.get(name, fallback)
        if isinstance(fallback, bool):
            clean[name] = value if isinstance(value, bool) else fallback
        elif isinstance(fallback, (int, float)):
            lo, hi = _RANGES.get(name, (-(2**31), 2**31))
            try:
                number = float(value)
                if number != number:  # NaN
                    raise ValueError
                number = max(lo, min(hi, number))
                clean[name] = int(round(number)) if isinstance(fallback, int) else number
            except (TypeError, ValueError, OverflowError):
                clean[name] = fallback
        elif name in _CHOICES:
            clean[name] = value if value in _CHOICES[name] else fallback
        else:
            clean[name] = value if isinstance(value, str) else fallback

    cam = clean["camera"]
    if not (cam in ("auto", "url") or (cam.startswith("cv:") and cam[3:].isdigit())):
        clean["camera"] = "auto"
    clean["stream_url"] = clean["stream_url"].strip()[:500]
    if not GazeCalibration.from_json(clean["eye_calibration"]).is_calibrated:
        clean["eye_calibration"] = ""
    clean["eye_calibration"] = clean["eye_calibration"][:2000]
    # The hide gesture only runs once its warning has been accepted.
    if not clean["hide_gesture_confirmed"]:
        clean["hide_gesture_enabled"] = False
    clean["version"] = SETTINGS_VERSION
    return Settings(**clean)


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Carry settings from Finger Mouse 2.1 and earlier into the new names."""
    if int(data.get("version", 1) or 1) >= SETTINGS_VERSION:
        return data
    out = dict(data)
    if "camera" in data and not isinstance(data["camera"], str):
        try:
            index = int(data["camera"])
            out["camera"] = "auto" if index < 0 else f"cv:{index}"
        except (TypeError, ValueError):
            out["camera"] = "auto"
    view = data.get("camera_view")
    if view == "wide":
        out.setdefault("camera_resolution", "1280x720")
    elif view == "standard":
        out.setdefault("camera_resolution", "640x480")
    if "response" in data:
        # "response" was the share of each new position used (5–50%): high =
        # snappy. Smoothing runs the other way.
        try:
            response = max(5, min(50, int(data["response"])))
            out.setdefault("smoothing", round(100 - (response - 5) * 100 / 45))
        except (TypeError, ValueError):
            pass
    renames = {
        "pinch_distance": "pinch_threshold",
        "cursor_size": "halo_size",
    }
    for old, new in renames.items():
        if old in data:
            out.setdefault(new, data[old])
    if data.get("flip_off_enabled") is True:
        # They already accepted the old confirmation dialog to turn it on.
        out.setdefault("hide_gesture_enabled", True)
        out.setdefault("hide_gesture_confirmed", True)
    return out


def config_dir() -> Path:
    """Per-user folder for settings and logs; never inside the app bundle."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load(path: Path | None = None) -> Settings:
    path = path or settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("settings file is not an object")
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        log.warning("Settings unreadable (%s); using defaults", exc)
        return Settings()
    return validate(migrate(raw))


def save(settings: Settings, path: Path | None = None) -> bool:
    """Write atomically (temp file + rename) so a crash can't leave half a file."""
    path = path or settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError as exc:
        # Settings are a convenience; failing to save must never stop the mouse.
        log.warning("Could not save settings: %s", exc)
        return False
