"""The one place the app's name, version and publisher are written down.

The PyInstaller spec, the Windows version resource, the macOS Info.plist and
the About text all read from here; CI passes the same version to the
installer builders. Bump APP_VERSION, tag ``v<APP_VERSION>``, push the tag.
"""

APP_NAME = "Finger Mouse"
APP_VERSION = "2.3.0"
APP_ID = "app.fingermouse.desktop"          # macOS bundle id / Linux app id
PUBLISHER = "Code-A-Difference"
PUBLISHER_URL = "https://codeadifference.ct.ws/projects/finger-tracking-mouse/"
SOURCE_URL = "https://github.com/Code-A-Difference/Finger-Tracking-Mouse"
DESCRIPTION = "Control the mouse pointer with one hand and a webcam"


def version_tuple() -> tuple[int, int, int, int]:
    """APP_VERSION as four integers, the shape Windows version resources need."""
    parts = [int(p) for p in APP_VERSION.split(".") if p.isdigit()]
    return tuple((parts + [0, 0, 0, 0])[:4])  # type: ignore[return-value]
