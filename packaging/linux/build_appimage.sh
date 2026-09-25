#!/usr/bin/env bash
# Package dist/FingerMouse (PyInstaller output) as a single-file AppImage:
#
#   packaging/linux/build_appimage.sh 2.2.0
#
# Produces dist/FingerMouse-linux-x86_64.AppImage. Download, mark executable,
# run — nothing to install, nothing fetched at run time. It needs glibc 2.35
# or newer (Ubuntu 22.04, Debian 12, Fedora 36 and later) and an X11 session;
# see RELEASING.md.
set -euo pipefail

VERSION="${1:?usage: build_appimage.sh VERSION}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="$ROOT/dist"
APPDIR="$ROOT/build/AppDir"
OUT="$DIST/FingerMouse-linux-x86_64.AppImage"

# appimagetool, pinned by version and checksum.
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
TOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
TOOL="$ROOT/build/appimagetool-1.9.1.AppImage"

[ -x "$DIST/FingerMouse/FingerMouse" ] || { echo "Build the app first: python -m PyInstaller FingerMouse.spec" >&2; exit 1; }

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/512x512/apps"
cp -a "$DIST/FingerMouse" "$APPDIR/usr/lib/finger-mouse"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/finger-mouse/FingerMouse" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/finger-mouse.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Finger Mouse
Comment=Control the mouse pointer with one hand and a webcam
Exec=FingerMouse
Icon=finger-mouse
Categories=Utility;Accessibility;
Terminal=false
X-AppImage-Version=$VERSION
EOF
cp "$APPDIR/finger-mouse.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT/assets/icon.png" "$APPDIR/finger-mouse.png"
cp "$ROOT/assets/icon.png" "$APPDIR/usr/share/icons/hicolor/512x512/apps/finger-mouse.png"

if [ ! -x "$TOOL" ]; then
  mkdir -p "$(dirname "$TOOL")"
  curl -fsSL -o "$TOOL" "$TOOL_URL"
  echo "$TOOL_SHA256  $TOOL" | sha256sum -c -
  chmod +x "$TOOL"
fi

rm -f "$OUT"
# --appimage-extract-and-run: works on build machines without FUSE.
ARCH=x86_64 VERSION="$VERSION" "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
chmod +x "$OUT"
echo "Built $OUT"
