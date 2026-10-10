# What changed

## 2.8.0

### Tells you when a new version is out
- A few seconds after opening, Finger Mouse checks GitHub for a newer
  release. If there is one, it shows what's new with a **Download** button
  for your computer's installer, **Later**, and **Skip this version**.
- Nothing is downloaded or installed by itself, and nothing about your
  computer is sent. Turn it off in Settings → Advanced → "Tell me when a
  new version is out".

## 2.7.0

### Sign-language typing — a fourth mode
- **Type by fingerspelling** (the ASL alphabet) into any app. Teach your own
  signs once (about two seconds each, with a space and a delete sign of your
  choice); Finger Mouse then recognises your hand by comparing it with what
  you showed it, and refuses to type when a sign doesn't clearly match.
- Hold a sign steady to type it once; relax between double letters. Hold
  time, certainty and capital letters in Settings → Sign language.
- Hand shape is measured independently of position, size and tilt, while
  keeping which way the hand points (so K/P and G/Q stay distinct).
- Keyboard output added on Windows (Unicode SendInput), macOS and Linux.

### Snap, for eye tracking and the head pointer
- Optional (off by default): once the pointer settles it locks completely
  still, and only lets go on a deliberate move past the snap strength. Dwell
  clicking still judges steadiness on your real gaze.

## 2.6.0

### Head pointer — a third tracking mode
- **Steer with your nose, click with winks.** Settings → Pointer → Tracking
  mode → Head pointer; its own Settings tab. No calibration.
- The nose tip is used because it's the most rigid point on a face, and it's
  measured in face widths, so the speed doesn't change with distance from the
  camera. Two styles: *Like a mouse* (relative, with acceleration — the
  default) and *Point at the spot* (absolute, with Re-centre). One Euro
  smoothing plus a dead zone for tremor and breathing.
- **Left wink = left click, right wink = right click**, held for a moment
  (adjustable). Two-eyed blinks never click; the pointer freezes while an eye
  is closing so the click lands where you aimed. A long-blink option for
  anyone who can't wink one eye.
- **Mouth open** drags (button held while open), scrolls (nod while open) or
  clicks; **a held smile** can pause/resume, double-click or right-click.
  Both need a short hold and have hysteresis, so talking or a passing grin
  doesn't trigger them.
- Right-click support added to the pointer output on Windows, macOS and Linux.

## 2.5.0

### Eye tracking: a much more thorough calibration
- **Thorough calibration (the new default, about 70 seconds):** a 5×5 grid
  of 25 points edge to edge, then a dot that glides around the screen for 20
  seconds while you follow it (hundreds of samples at every place between
  the grid points), then five check points it never trained on. Settings →
  Eye tracking → Calibration still offers the quick 13-point version.
- **An honest accuracy number.** After a thorough calibration, the error
  reported is measured on those check points — then they're added to the
  calibration too.
- **The mapping looks at more of your eyes.** Each eye separately (they
  disagree usefully near the edges), how open your eyes are (the eyelid
  follows the eyeball up and down, which the iris barely does inside its
  socket — this is what fixes vertical accuracy), and head turn and nod with
  how they interact with gaze: fifteen terms instead of eight, fitted to all
  those samples by weighted ridge regression, with the smoothing amount
  chosen automatically by testing on held-back samples.
- **Re-centre (1 s).** If the pointer drifts later because you or the laptop
  moved, Settings → Eye tracking → Re-centre: look at one ring for a second
  and the whole calibration shifts back, no recalibrating.
- Older calibrations still load; calibrate again to get all of this.

## 2.4.0

### Eye tracking accuracy
- **Calibration uses 13 points, not 9**: the 3x3 grid now sits nearer the
  screen edges, with four more points between it and the centre, where most
  of what you look at is.
- **A shrinking ring instead of a dot.** The old 28-pixel dot let your eyes
  rest anywhere on it, and every pixel of that became error. The ring closes
  onto a 4-pixel centre, and sampling starts only once it has.
- **Blinks and glances no longer skew a point.** Blink frames are dropped
  and the median of the rest is used; a point that couldn't be read is
  shown again.
- **Head movement is part of the mapping.** Calibration records where your
  head is (nose relative to the eyes) and the fit uses it, so a small head
  turn after calibrating no longer throws the pointer across the screen.
  Older calibrations still load and work as before; recalibrate to get this.
- **You're told how accurate it came out** (average error as % of the
  screen and roughly in pixels), so you know whether to redo it.
- **A smaller ring follows your gaze** in eye mode (a third of the hand-mode
  halo), with a sharp centre point.

## 2.3.0

### Eye tracking
- **A second way to control the pointer: your gaze.** Settings → Pointer →
  Tracking mode switches between hand gestures and eye tracking. Eye mode
  reads where your iris sits in its own eye socket (MediaPipe's face and
  iris landmarks), which moves with the eyeball and barely with the head,
  and maps it onto the screen with a short calibration (look at nine dots
  in turn) — same idea as the hand-mode setup, one screen instead of one
  step.
- **Click by holding your gaze still** (dwell, the default) **or by a
  deliberate blink**, or both — Settings → Eye tracking. A quick, ordinary
  blink doesn't count; only one held past a configurable threshold does.
  Dwelling tolerates a little drift (the "steadiness" setting) rather than
  demanding a frozen stare, and re-arms only once you've looked away, so it
  can't click twice by lingering.
- Runs on the same four-thread engine as hand mode — its own gesture state
  (`gesture_state.GazeCalibration`, `DwellClick`), its own model
  (`face_landmarker.task`, pinned by SHA-256 next to the hand model), but
  the same camera thread, pointer output thread and watchdog. Switching
  modes tears down and rebuilds just the tracker, live.
- Best with your head reasonably still and facing the camera; a head turn
  (not just an eye movement) can throw off the mapping more than hand
  tracking's equivalent wobble would. No drag gesture yet — click only.

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
