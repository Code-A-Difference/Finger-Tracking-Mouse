# Build on the platform you want to distribute for. PyInstaller does not
# cross-compile native camera, Qt, or mouse-control libraries.
import sys

from PyInstaller.utils.hooks import collect_data_files

# The hand graph and its model files are loaded by path at runtime. Collect
# MediaPipe's data without force-including every optional task dependency.
datas = collect_data_files("mediapipe")
binaries = []
hiddenimports = [
    "mediapipe.python.solutions.hands",
    "mediapipe.python.solutions.drawing_utils",
]

a = Analysis(
    ["Finger_tracker.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FingerMouse",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FingerMouse",
)

if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name="Finger Mouse.app",
        bundle_identifier="app.fingermouse.desktop",
        info_plist={
            "CFBundleShortVersionString": "2.1.0",
            "CFBundleVersion": "2.1.0",
            "NSCameraUsageDescription": "Finger Mouse uses your camera to track your hand and control the pointer.",
            "NSHighResolutionCapable": True,
        },
    )
