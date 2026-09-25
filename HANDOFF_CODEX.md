# Handoff to Codex — Finger Mouse 2.2.0

Written 2026-09-25 by Claude, after shipping v2.2.0 end to end (code, tests,
CI, installers, release, and the download site). This is the orientation
doc for whoever (human or agent) picks this repo up next. Everything below
is already merged to `main` and released — nothing here is a pending PR.

Repo: `Code-A-Difference/Finger-Tracking-Mouse` (nested inside the
`Code a Diffrence` website folder on disk, but it's its own git repo/remote
and is gitignored by the website repo — don't confuse the two).

Current HEAD: `6505539` on `main`. Latest tag: `v2.2.0` (released, public,
has built installers attached — see "What's live" below).

## What shipped in 2.2.0

Full user-facing changelog is in [CHANGES.md](CHANGES.md). Short version:

1. **Stable pinch clicking** — click point locks the instant the pinch
   crosses its threshold (not a few frames later), with hysteresis,
   confirm-frame debouncing, and a closing timeout so a half-pinch that
   never settles gives up instead of freezing the pointer.
2. **Pinch-and-hold drag** — press on pinch, drag follows the hand from the
   locked point, releases on open/hand-lost/camera-switch/error/exit. The
   button is *always* released somewhere (`PointerOutput._halt` guarantees
   this even mid-drag).
3. **Separate scroll gesture** — two-finger pose (index+middle up, palm
   roughly vertical), joystick-style: dead zone, speed grows with distance
   (capped), smoothing, only arms from a steady non-pinching hand.
4. **Freeze fixes** — four independent threads (camera grab, hand tracking,
   pointer output, Qt UI at 60 Hz) so none can block another; a watchdog
   with heartbeats; the halo is now a small moving window instead of a
   full-desktop transparent overlay (that was the main stall cause).
5. **Settings dialog** — six tabs, persisted to `app_settings.py`'s
   dataclass, validated and migrated from v1 keys.
6. **Camera switching** — real device names (DirectShow/V4L2/AVFoundation),
   virtual cameras sorted last in Automatic mode, zoom/driver-dialog/FOV
   controls only offered when the driver actually supports them, plus an
   experimental network-stream (phone camera) source.
7. **Middle-finger hide gesture** — opt-in, confirmation-gated, tray-based.
8. **Website**: direct OS-specific installer links (no ZIPs), OS detection,
   "Not available yet" label when a platform build is missing. This part
   lives in the *website* repo, not here — see `site/_lib/releases.php` and
   `site/_lib/view-project.php` there.
9. **Signing pipeline**: Windows via Azure Artifact Signing (or a raw PFX
   secret) in CI; macOS Developer ID + notarytool documented in
   [RELEASING.md](RELEASING.md); Linux AppImage (unsigned, standard for the
   format).

## Architecture (read this before changing anything)

- **Four threads**, wired up in `tracking_engine.py`:
  - `FrameGrabber` (`camera.py`) — reads the camera, keeps only the newest
    frame.
  - `TrackingEngine`'s own thread — runs `HandTracker.process()`
    (MediaPipe), gesture state machines, produces a `View` snapshot.
  - `PointerOutput` (`pointer_output.py`) — its own thread with an ordered
    queue; coalesces moves, never drops a `release`.
  - Qt `MainWindow` — polls `engine.view` at 60 Hz, does no blocking work.
  - `diagnostics.Watchdog` — heartbeats from each thread; restarts the
    camera if it stalls > 2.5s.
- **Hand tracking**: MediaPipe Tasks `HandLandmarker`, VIDEO mode, model
  loaded as bytes (`model_asset_buffer`) and SHA-256 pinned
  (`hand_tracker.MODEL_SHA256`). On macOS a subprocess probe
  (`--probe-hand-model`) opens the model once before the real run, because
  MediaPipe hard-aborts (not raises) in `DrishtiMetalHelper` on some VMs —
  this is the only way to turn that abort into a normal error message.
- **Gesture state**: `gesture_state.py` — `PinchGesture`
  (idle→ready→closing→pressed→dragging, `CLOSING_TIMEOUT = 0.35`),
  `PointerStabilizer` (freezes pointer during closing/pressed, relative
  move during drag, decays offset after), `ScrollGesture` (joystick model),
  `HeldPose` (hide gesture, tolerance+latch+cooldown). `OneEuroFilter`
  smooths raw landmark positions with damping that tightens as a pinch
  closes.
- **Pose reading**: `hand_pose.py` — `measure()` uses 3D joint angles and
  reach, not raw pixel positions, so it's rotation-independent.
- **Native pointer backends** (`pointer_output.py`): `WindowsBackend`
  (SendInput, absolute coords, ceil mapping, MARKER in `dwExtraInfo` to
  detect physical-mouse override), `MacBackend` (Quartz dragged events +
  click-state counting for real double-clicks), `PyAutoGUIBackend` (Linux,
  XTest, with a `mouseinfo` stub so missing tkinter doesn't `sys.exit()`
  the whole process on import).
- **Settings**: `app_settings.py`, a dataclass with `validate`, `migrate`
  (from v1 keys), atomic `save`.
- **Packaging**: `FingerMouse.spec` (PyInstaller), `installer/FingerMouse.iss`
  (Inno Setup, per-user install), `packaging/macos/build_dmg.sh`
  (codesign + notarytool), `packaging/linux/build_appimage.sh`
  (appimagetool 1.9.1, SHA-256 pinned).
- **CI**: `.github/workflows/build.yml` — matrix of
  windows-2022/macos-15/macos-15-intel/ubuntu-22.04, Python 3.12, tests run
  with `--capture=sys` and failures turned into annotations by
  `tools/ci_annotate.py` (so a failure is readable from the workflow run
  page without needing to be signed into GitHub). Release job triggers on
  `v*` tags and also kicks the Pages workflow via `gh workflow run`.

## Tests

90 tests, all passing locally and in CI. Layout in `tests/`:
`test_gesture_state.py` (state machine transitions — this is the file to
extend if you add new gesture behavior), `test_hand_pose.py` (uses real
photos in `tests/data`), `test_settings.py`, `test_pointer_output.py`,
`test_camera.py`, `test_engine.py`, `test_windows_backend.py` (skipped on
CI / non-Windows), `test_hand_tracker.py`, `test_threads.py`,
`test_app_smoke.py` (end-to-end, runs last per `conftest.py`).

Run with:
```
python -m pytest
```
On Windows, build/test under a short path — see the note below, this bit
me hard during this session.

## What's live right now (verify before assuming stale)

- GitHub release: `v2.2.0` with `FingerMouse-windows-x64-setup.exe`,
  `-macos-arm64.dmg`, `-macos-x64.dmg`, `-linux-x86_64.AppImage`, and
  `SHA256SUMS.txt`.
- Website: https://codeadifference.ct.ws/projects/finger-tracking-mouse/
  — four installer links, Windows highlighted, no ZIP links.
- GitHub Pages: https://code-a-difference.github.io/Finger-Tracking-Mouse/
  — same four links, `release.json` pinned to v2.2.0.
- I downloaded and verified the released Windows installer myself:
  checksum matches, correct product metadata, installs, self-test (`--self-
  test`) passes all five checks, uninstalls cleanly. It is **not signed**
  (see Known limitations).

## Known limitations / things I could not verify

- **No real webcam/hand test.** I never turned on the user's camera or
  tested with an actual hand. All gesture logic is verified via
  `tests/data` photos and unit tests of the state machines, not live use.
  If gestures feel wrong in practice, start here.
- **Real Apple-silicon hand tracking is untested.** GitHub's macOS runners
  can't run MediaPipe's Metal path at all (that's the whole reason the
  probe subprocess exists) — CI only proves the app doesn't crash, not that
  tracking actually works on an M-series Mac.
- **Installers are unsigned.** No signing secrets are configured in CI yet.
  Windows shows SmartScreen "unknown publisher", macOS Gatekeeper will
  refuse to open the DMG without a right-click-Open bypass. See "Signing"
  below for exact steps — this is probably the single highest-value next
  task if the goal is wider public distribution.
- **Phone-camera (network stream) source is untested against a real phone
  app.** It's wired up (`camera.py`'s `NetworkStreamCamera`) but I never
  had a phone streaming app to point it at.
- **Linux** needs X11 (not pure Wayland) plus `libgles2`/`libxcb-cursor0`
  and glibc 2.35+ (Ubuntu 22.04+ or equivalent) — not bundled, documented
  as a requirement instead.
- **Windows can't control elevated windows** — SendInput can't target a
  window running as administrator from a non-elevated process; this is an
  OS restriction, not a bug, but worth knowing if a user reports "clicks
  don't work" while an elevated app has focus.

## Signing and releasing — exact steps

Full detail in [RELEASING.md](RELEASING.md). Summary:

1. Add CI secrets (repo Settings → Secrets and variables → Actions):
   - Windows: either Azure Artifact Signing account secrets, or
     `WINDOWS_CERT_PFX_BASE64` + `WINDOWS_CERT_PASSWORD`.
   - macOS: `MACOS_CERT_P12_BASE64`, `MACOS_CERT_PASSWORD`,
     `MACOS_SIGN_IDENTITY`, `MACOS_NOTARY_KEY_P8_BASE64`,
     `MACOS_NOTARY_KEY_ID`, `MACOS_NOTARY_ISSUER_ID`.
   - Linux: no signing step exists for AppImages (not standard practice).
2. Bump `app_version.py` (`APP_VERSION`), add an entry to `CHANGES.md`.
3. Commit, push, wait for CI to go green on all four matrix legs.
4. `git tag vX.Y.Z && git push origin vX.Y.Z` — this **is** the public
   release trigger (the release workflow builds installers and publishes a
   GitHub Release, then triggers the Pages workflow to update the download
   site). Don't tag until you actually want it public.

## Local dev environment note (Windows-specific gotcha)

On this machine, building/testing from a deeply-nested path (like the
session's temp scratchpad) makes PySide6's Qt plugin DLL load fail with
"filename or extension too long" — and Qt shows this as a **native fatal
dialog box that a headless/tool shell never sees**, which looks exactly
like a hang. If Finger Mouse appears to hang on start:
```
set QT_FORCE_STDERR_LOGGING=1
set QT_DEBUG_PLUGINS=1
```
and re-run — the real error will be in stderr instead of a silent dialog.
Workaround used this session: build/venv under `%LOCALAPPDATA%\fm-build`
instead of anywhere with a long path.

## If you're Codex picking this up

Good next tasks, roughly in priority order:
1. **Real hardware verification** — run the app against an actual webcam
   and hand; this was never done in the session that shipped 2.2.0.
2. **Code-sign the installers** (see above) — currently the biggest
   barrier to anyone outside this org trusting the download.
3. **Test the phone-camera network stream** against a real streaming app.
4. If adding new gestures or pointer behavior, extend
   `tests/test_gesture_state.py` first — it's the fastest feedback loop and
   doesn't need a camera.
