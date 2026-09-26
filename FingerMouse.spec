# PyInstaller spec for Finger Mouse.
#
#   python tools/fetch_model.py            (once)
#   python -m PyInstaller --noconfirm FingerMouse.spec
#
# Build on the platform you're shipping for: PyInstaller doesn't
# cross-compile the camera, Qt or input libraries. The result is a folder
# app in dist/ (dist/FingerMouse, or dist/Finger Mouse.app on macOS) that
# the installer builders then package. Name, version and publisher come from
# app_version.py, so they match everywhere.
#
# macOS signing: set MACOS_SIGN_IDENTITY (a "Developer ID Application: …"
# identity in the build keychain) and PyInstaller signs every binary in the
# bundle with the hardened runtime and packaging/macos/entitlements.plist.
# Without it, the bundle is ad-hoc signed, which Apple silicon requires.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH)  # noqa: F821 (provided by PyInstaller)
sys.path.insert(0, str(ROOT))
import app_version as v  # noqa: E402

hand_model = ROOT / "models" / "hand_landmarker.task"
eye_model = ROOT / "models" / "face_landmarker.task"
if not hand_model.exists() or not eye_model.exists():
    raise SystemExit("A tracking model is missing. Run `python tools/fetch_model.py` first.")

datas = [
    (str(hand_model), "models"),
    (str(eye_model), "models"),
    (str(ROOT / "assets" / "icon.png"), "assets"),
    (str(ROOT / "assets" / "check.png"), "assets"),
]

sign_identity = os.environ.get("MACOS_SIGN_IDENTITY") or None
entitlements = str(ROOT / "packaging" / "macos" / "entitlements.plist") if sign_identity else None

version_resource = None
if sys.platform == "win32":
    from PyInstaller.utils.win32 import versioninfo as vi

    numbers = v.version_tuple()
    version_resource = vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F, flags=0x0,
                             OS=0x40004, fileType=0x1, subtype=0x0),
        kids=[
            vi.StringFileInfo([vi.StringTable("040904B0", [
                vi.StringStruct("CompanyName", v.PUBLISHER),
                vi.StringStruct("FileDescription", v.APP_NAME),
                vi.StringStruct("FileVersion", v.APP_VERSION),
                vi.StringStruct("InternalName", "FingerMouse"),
                vi.StringStruct("LegalCopyright", f"© {v.PUBLISHER}. MIT License."),
                vi.StringStruct("OriginalFilename", "FingerMouse.exe"),
                vi.StringStruct("ProductName", v.APP_NAME),
                vi.StringStruct("ProductVersion", v.APP_VERSION),
                vi.StringStruct("Comments", v.DESCRIPTION),
            ])]),
            vi.VarFileInfo([vi.VarStruct("Translation", [1033, 1200])]),
        ],
    )

icon = {
    "win32": str(ROOT / "assets" / "FingerMouse.ico"),
    "darwin": str(ROOT / "assets" / "FingerMouse.icns"),
}.get(sys.platform)

a = Analysis(  # noqa: F821
    ["Finger_tracker.py"],
    pathex=[str(ROOT)],
    # MediaPipe 1.0 finds its native library (libmediapipe.dll/.so/.dylib) through
    # the mediapipe.tasks.c package at run time, which static analysis can't see.
    binaries=collect_dynamic_libs("mediapipe"),
    datas=datas,
    hiddenimports=[
        "mediapipe.tasks.python.vision.hand_landmarker",
        "mediapipe.tasks.python.vision.face_landmarker",
        "mediapipe.tasks.python.core.base_options",
    ] + collect_submodules("mediapipe.tasks.c") + (["Quartz", "ApplicationServices", "AVFoundation", "AppKit"] if sys.platform == "darwin" else []),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Never used by Finger Mouse; keeps the download smaller.
    excludes=["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtSql",
              "PySide6.QtTest", "PySide6.QtPdf", "PySide6.QtOpenGL", "IPython", "pytest"],
    noarchive=False,
    optimize=0,
)
# Qt's software OpenGL fallback (20 MB); Finger Mouse draws nothing with OpenGL.
a.binaries = [b for b in a.binaries if Path(b[0]).name.lower() != "opengl32sw.dll"]
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FingerMouse",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # UPX-packed executables trip antivirus heuristics
    console=False,
    icon=icon,
    version=version_resource,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=sign_identity,
    entitlements_file=entitlements,
)
collection = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FingerMouse",
)

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        collection,
        name="Finger Mouse.app",
        icon=icon,
        bundle_identifier=v.APP_ID,
        version=v.APP_VERSION,
        info_plist={
            "CFBundleName": v.APP_NAME,
            "CFBundleDisplayName": v.APP_NAME,
            "CFBundleShortVersionString": v.APP_VERSION,
            "CFBundleVersion": v.APP_VERSION,
            "NSHumanReadableCopyright": f"© {v.PUBLISHER}. MIT License.",
            "NSCameraUsageDescription": "Finger Mouse uses the camera to follow your hand and move the pointer. "
                                        "Video stays on this Mac.",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0",   # Qt 6.11 and OpenCV 5 need Ventura or later
            "LSApplicationCategoryType": "public.app-category.utilities",
        },
    )
