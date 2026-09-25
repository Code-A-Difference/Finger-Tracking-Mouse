# What changed

## 2.2.0

### Clicking and dragging
- **Clicks land where the pinch began.** The click point is locked the
  instant the pinch crosses its threshold. Before, it was taken a couple of
  frames later, after the pinch had already dragged the fingertip. Pointer
  smoothing also tightens as the fingers close, so the pinch barely moves the
  pointer in the first place.
- Quick pinch = click; **pinch and hold = press and drag**; open = release.
  The drag starts exactly on the locked point and follows the hand from
  there, and the pointer eases back to the hand afterwards instead of
  jumping.
- Debounced with hysteresis and frame agreement. A hand must be seen open
  before a pinch counts, a fist doesn't count as a pinch, and a half-pinch
  that never settles gives up rather than freezing the pointer.
- The button is always released: when your fingers open, when the hand is
  lost (after a short grace period, so a flicker doesn't drop a drag), and
  on stop, camera switch, camera failure, error or exit.
- **macOS drag-and-drop now works.** Movement with the button held is sent
  as real drag events. Double-clicks register as double-clicks on macOS too.

### Scrolling
- New default pose: **index and middle fingers up**, move up or down. The
  old index-only pose was the same as the pointing hand people move with, so
  it scrolled by accident; it's still available in Settings.
- Joystick-style: dead zone, speed that grows with distance, a speed cap and
  a gentle start. The pointer holds still while scrolling, and scrolling only
  starts from a steady hand that isn't pinching.
- Smooth scrolling in fractions of a wheel step on Windows and macOS.

### Freezes
- **Four threads that can't block each other:** camera capture, hand
  tracking, the window and pointer output. Previously the camera read, the
  tracking and the drawing shared one thread, and the window did the pointer
  work.
- **The pointer halo** is now a small window that moves. It used to be a
  transparent window the size of the whole desktop, repainted every frame,
  which was enough to stall everything on large or multiple screens.
- The window polls the latest frame instead of being sent every one, so a
  slow repaint can't back up the tracker. A burst of pointer moves collapses
  into the newest one.
- A camera that stops sending is reconnected automatically. Hand tracking is
  rebuilt after repeated errors. Results about frames too old to trust are
  ignored.
- A watchdog names whichever part stalls ("camera read blocked (2.1 s)") in
  the diagnostics line and the log, and logs when it recovers.
- Moving your real mouse pauses hand control for a moment, which is also a
  way to reach Stop when tracking goes wrong. This replaces PyAutoGUI's
  corner failsafe, which also blocked reaching screen corners.

### Settings, cameras, gestures
- A **Settings** dialog for everything: pointer smoothing and reach, screen,
  halo, click and drag thresholds and timing, scrolling, cameras, the hide
  gesture, and debugging. Changes apply live and are saved. Settings from
  2.1 carry over.
- **Camera names** from the system, virtual cameras marked, and Automatic
  tries real cameras first. On one test machine, four NDI virtual cameras
  came before the built-in one. Any number of cameras is supported (2.1
  stopped at six).
- Zoom and the Windows driver settings window, only when the camera
  actually offers them. Resolution choice, with a check of what the camera
  really delivers.
- **Phone or network stream** (experimental). Phone bridge apps already
  appear as normal cameras.
- The **hide gesture** (off by default, confirmation required) now hides to
  the tray, minimises, or quits. Before, it hid the window with no way to
  bring it back.

### Under the hood
- MediaPipe's **Tasks API**. MediaPipe 1.0 removed the `mp.solutions.hands`
  API 2.1 used, so 2.1 no longer starts with current MediaPipe. The model is
  pinned by SHA-256 and loaded from memory, which avoids MediaPipe's trouble
  with some Windows install paths.
- Native pointer control: SendInput on Windows (exact pixels, verified
  against what Windows receives), Quartz on macOS, XTest on Linux. The app
  now says clearly when the desktop ignores it (macOS without Accessibility
  permission, Linux Wayland).
- Logs in the settings folder (`logs/finger-mouse.log`, rotated), not the
  home folder.

### Installers and releases
- A real **Windows installer**: per-user, no administrator prompt, Start
  menu entry, clean uninstall, publisher and version metadata. **macOS disk
  images** and a **Linux AppImage**. No more ZIPs.
- An app icon; publisher and version in the Windows file properties, the
  macOS bundle and the installer.
- CI tests and packages all four platforms on every push, self-tests each
  built app, and tests the AppImage in a clean Ubuntu container. Tags
  publish a release with SHA-256 checksums.
- Signing pipeline: Authenticode (Azure Trusted Signing or .pfx, timestamped,
  installer and uninstaller included) and Apple Developer ID with
  notarization, all from CI secrets. See RELEASING.md.
- Download pages show only real installers and label anything missing as
  "not available yet".
