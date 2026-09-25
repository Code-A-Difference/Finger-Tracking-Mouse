# Finger Mouse

Control your computer's real mouse pointer with one hand and a webcam: point
to move, pinch to click, pinch and hold to drag, raise two fingers to scroll.
Built for people who find a physical mouse hard to use. Everything runs on
your own computer; no video leaves it, and it doesn't need the internet.

## Download

**[codeadifference.ct.ws/projects/finger-tracking-mouse](https://codeadifference.ct.ws/projects/finger-tracking-mouse/)**
or the [GitHub download page](https://code-a-difference.github.io/Finger-Tracking-Mouse/)
— both suggest the right file for your computer and link straight to the
latest [release](https://github.com/Code-A-Difference/Finger-Tracking-Mouse/releases/latest):

| System | File |
|---|---|
| Windows 10/11, 64-bit | `FingerMouse-windows-x64-setup.exe` — installer, no administrator rights needed |
| macOS 13+, Apple silicon | `FingerMouse-macos-arm64.dmg` — drag to Applications |
| macOS 13+, Intel | `FingerMouse-macos-x64.dmg` — drag to Applications |
| Linux x86-64 (glibc 2.35+, X11) | `FingerMouse-linux-x86_64.AppImage` — `chmod +x`, then run |

Once installed, Finger Mouse runs entirely offline.

## Using it

| Do this | To |
|---|---|
| Point with your index finger | move the pointer |
| Quick pinch (thumb + index), then open | left-click **where the pinch began** |
| Pinch and hold (0.42 s by default), move, open | press, drag, drop |
| Index and middle fingers up, move up or down | scroll — further from where you started is faster |
| Move your real mouse | pause hand control for a moment |
| Esc, or **Stop tracking** | stop |

**Clicks land where you meant.** As your fingers close, pointer smoothing
tightens so the pinch itself barely moves the pointer. The click point is
locked the instant the pinch crosses its threshold, and the pointer holds
still there until the click or drag is decided. Several frames must agree
before a pinch or release counts, and releasing needs the fingers to open
past a wider threshold, so wobble near the line can't click or drop by
itself.

**Dragging always lets go.** Opening your fingers releases the button, and
so does losing sight of your hand, stopping, switching cameras, a camera
failure, an error, or quitting.

**Scrolling is its own gesture.** Two fingers up is deliberately unlike the
pointing hand used to move, and scrolling only starts from a steady hand
that isn't pinching. The pointer stays still while you scroll. There's a
dead zone, a speed cap and a gentle start.

### Settings

Everything is under **Settings**, applies immediately, and is saved to your
user folder (`%APPDATA%\Finger Mouse`, `~/Library/Application Support/Finger
Mouse`, or `~/.config/Finger Mouse`):

- **Pointer:** smoothing, hand reach (how much of the camera view spans the
  screen), main screen or all screens, halo size, pause while using the real
  mouse, and a shortcut to the system's own pointer-size setting.
- **Click & drag:** turn clicking or dragging on and off, pinch threshold,
  release gap, how many frames must agree, hold time before a drag.
- **Scrolling:** on/off, pose (two fingers, or index only), speed, dead zone,
  direction.
- **Camera:** see below.
- **Hide gesture** (off by default): hold up only your middle finger to stop
  tracking and hide Finger Mouse to the tray, minimise it, or quit it. It
  asks for confirmation before it can be turned on, and it never touches
  other apps.
- **Advanced:** hand landmarks and gesture state in the preview, a
  diagnostics line, detailed logging, and the log folder.

### Cameras

The camera list shows real names ("Integrated Camera", "Logitech BRIO")
wherever the system provides them. Software cameras (OBS, NDI, and the like)
are marked *virtual*, and **Automatic** tries real cameras first, because
virtual ones often come first in the list and show a blank picture.

- **Resolution:** pick one, or ask the camera which it actually supports.
- **Zoom** and the **driver settings window** appear only when the camera's
  driver really offers them.
- **Field of view** is set by the lens and can't be widened in software;
  Finger Mouse never crops or stretches the picture to fake it. Some cameras
  show a wider area in 16:9 modes such as 1280×720, which is the default.
- **Phone as a camera:** DroidCam, Iriun and similar apps (and Continuity
  Camera on a Mac) add your phone as a normal camera in the list. Apps that
  publish a video address over Wi-Fi (IP Webcam and others) work through
  **Phone or network stream**, which is experimental.

## Permissions

- **Windows:** allow camera access (Settings → Privacy & security → Camera).
  Windows doesn't let normal apps control windows running as administrator,
  so the pointer can't click inside those.
- **macOS:** keep Finger Mouse in Applications. Allow the camera, and turn it
  on under System Settings → Privacy & Security → Accessibility so it can
  move the pointer, then reopen it. If the pointer doesn't move, Finger Mouse
  says so and explains what to do.
- **Linux:** use an X11 session; Wayland blocks apps from moving the pointer,
  and Finger Mouse tells you if it can't. If the AppImage won't start,
  install `libxcb-cursor0` and `libgles2`.

## If something goes wrong

Turn on **Settings → Advanced → Show diagnostics**. The line under the
preview shows camera frame rate, how old the latest frame is, hand-tracking
time, tracking frame rate, the window's own frame rate, and the pointer
queue. If any part stalls, it names which one ("camera read blocked
(2.1 s)").

The same appears in the log (**Open the log folder** in Settings). The log
records each stall once, with how long it lasted and when it recovered.

Camera capture, hand tracking, the window and pointer output each run on
their own thread, so none of them can freeze the others. A camera that stops
sending is reconnected automatically. Hand tracking that fails repeatedly is
restarted. A result that arrives too late to trust is ignored rather than
acted on.

## Run from source

Python 3.12 or newer.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python tools/fetch_model.py        # the hand model, checked against a pinned SHA-256
python Finger_tracker.py
```

`python -m pytest` runs the tests. `python Finger_tracker.py --self-test`
checks a setup without a camera.

## How it's built

| File | Job |
|---|---|
| `Finger_tracker.py` | the window, settings dialog, tray icon, pointer halo |
| `tracking_engine.py` | tracking thread: frames → landmarks → gestures → pointer commands |
| `gesture_state.py` | pointer smoothing, click/drag, scroll and hold-gesture state machines (pure Python) |
| `hand_pose.py` | pinch distance and finger states from landmarks, independent of hand angle and distance |
| `hand_tracker.py` | MediaPipe Tasks hand landmarker (model loaded from memory) |
| `camera.py` | camera listing, webcam and network-stream sources, capture thread |
| `pointer_output.py` | output thread; SendInput (Windows), Quartz (macOS), XTest via PyAutoGUI (Linux) |
| `diagnostics.py` | rotating log file and the stall watchdog |
| `app_settings.py` | settings: validation, migration from 2.1, atomic saves |
| `app_version.py` | name, version and publisher, used by the build and installers |

Tests cover every gesture transition, pose reading (including MediaPipe's
own sample photos through the real model), settings, the output thread's
ordering and release guarantees, and camera stalls. There are also
end-to-end runs of the whole tracking loop and of the app itself. On
Windows, a test checks what SendInput delivers through a low-level hook,
without clicking anything.

## Building and releasing

See [RELEASING.md](RELEASING.md): installers for every platform, code signing
(Authenticode via Azure Artifact Signing or a .pfx; Apple Developer ID with
notarization), and the release workflow. What changed in each version is in
[CHANGES.md](CHANGES.md).

## License

MIT — see [LICENSE](LICENSE). Bundled libraries (Qt, MediaPipe, OpenCV and
others) keep their own licenses. The hand model is Google's MediaPipe hand
landmarker (Apache 2.0).
