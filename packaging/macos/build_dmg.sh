#!/usr/bin/env bash
# Package "dist/Finger Mouse.app" (PyInstaller output) as a disk image, and
# sign and notarize it when the credentials are present:
#
#   packaging/macos/build_dmg.sh arm64      (or x64)
#
# Produces dist/FingerMouse-macos-<arch>.dmg: open it, drag Finger Mouse to
# Applications.
#
# Signing (optional; see RELEASING.md). All come from CI secrets, never the repo:
#   MACOS_SIGN_IDENTITY    "Developer ID Application: <Name> (<TEAMID>)", already
#                          imported into the build keychain
#   MACOS_NOTARY_KEY_PATH  App Store Connect API key (.p8) written to a temp file
#   MACOS_NOTARY_KEY_ID    its key ID
#   MACOS_NOTARY_ISSUER    its issuer ID
# Without them the app keeps PyInstaller's ad-hoc signature: it runs, but
# Gatekeeper warns on first open.
set -euo pipefail

ARCH="${1:?usage: build_dmg.sh arm64|x64}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$ROOT/dist/Finger Mouse.app"
OUT="$ROOT/dist/FingerMouse-macos-$ARCH.dmg"
STAGE="$ROOT/build/dmg"

[ -d "$APP" ] || { echo "Build the app first: python -m PyInstaller FingerMouse.spec" >&2; exit 1; }

if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  # PyInstaller signed every binary inside; this seals the bundle itself.
  codesign --force --options runtime --timestamp \
    --entitlements "$ROOT/packaging/macos/entitlements.plist" \
    --sign "$MACOS_SIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
fi

rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Finger Mouse" -srcfolder "$STAGE" -ov -format UDZO "$OUT"

if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
  codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$OUT"
  if [ -n "${MACOS_NOTARY_KEY_PATH:-}" ] && [ -n "${MACOS_NOTARY_KEY_ID:-}" ] && [ -n "${MACOS_NOTARY_ISSUER:-}" ]; then
    xcrun notarytool submit "$OUT" --key "$MACOS_NOTARY_KEY_PATH" \
      --key-id "$MACOS_NOTARY_KEY_ID" --issuer "$MACOS_NOTARY_ISSUER" --wait
    xcrun stapler staple "$OUT"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$OUT"
  else
    echo "Signed but not notarized: notary credentials not set." >&2
  fi
fi
echo "Built $OUT"
