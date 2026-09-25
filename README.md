# Finger Mouse

Finger Mouse is a hands-free desktop pointer controlled by a webcam. It moves the computer’s real system cursor and sends a real left-click when you pinch your thumb and index finger. The app processes camera frames locally.

## Downloads

The public download page is published at [code-a-difference.github.io/Finger-Tracking-Mouse](https://code-a-difference.github.io/Finger-Tracking-Mouse/) from [website/downloads.html](website/downloads.html). It links to the latest GitHub Release. After a version tag is published, GitHub Actions builds archives for Windows x64, macOS Apple silicon, macOS Intel, and Linux x64. A manual workflow run also creates downloadable CI artifacts. The page can also be copied to the Code-A-Difference website later; see [website/README.md](website/README.md).

## Features

- Live preview with hand landmark visualization.
- Normalized thumb-index pinch distance with adjustable sensitivity.
- Two-frame arm and click gates. A held pinch registers once; opening the fingers re-arms the next click.
- Camera selection and wide (16:9) or standard (4:3) camera modes.
- Adjustable pointer movement response and tracking halo size.
- Native system cursor-size settings shortcut for Windows and macOS.
- Settings saved in the user's application settings folder.
- Escape stop shortcut, Stop button, and PyAutoGUI corner failsafe.
- Runs on Windows, macOS, and Linux desktop systems with compatible webcam and OS permissions.

## Run from source

Python 3.9–3.12 and a working webcam are required. Create a virtual environment and install the pinned dependencies:

```sh
python -m venv .venv
```

On macOS or Linux:

```sh
source .venv/bin/activate
python -m pip install -r requirements.txt
python Finger_tracker.py
```

On Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python Finger_tracker.py
```

Choose **Automatic** to try camera devices 0–5, or select a camera manually. **Wide 16:9** asks for 1280×720; **Standard 4:3** asks for 640×480. The camera driver determines the actual frame dimensions and field of view. The app shows the resolution it receives.

## Use

1. Start the app and allow camera access when asked.
2. Select your camera and choose **Start tracking**.
3. Move your index fingertip to move the pointer.
4. Open thumb and index to arm clicking. Pinch their tips together for one left-click. Open the fingers to arm the next click.
5. Use **Stop tracking** or press **Esc** while the app window is active. Moving the pointer into the screen's top-left corner activates the emergency failsafe.

The **Pinch distance** slider adjusts how close the fingertips need to be. Raise it if pinches are missed or lower it if clicking triggers too early. **Movement response** changes pointer smoothing. **Tracking halo size** changes the visual indicator. Use **System cursor size…** to open the operating system’s own pointer-size setting; that changes the real cursor. On Linux, open your desktop environment’s Accessibility settings.

## Permissions and compatibility

- **macOS:** move Finger Mouse to **Applications before granting permission**. Allow Camera access for tracking. For pointer movement and clicks, enable the same `/Applications/Finger Mouse.app` copy under **System Settings → Privacy & Security → Accessibility**, then quit and reopen it. If access remains blocked, remove old Finger Mouse entries and add that exact copy again. The app reports the Accessibility and event-posting checks separately and shows the path of the copy macOS needs to trust. Release bundles are ad-hoc signed, not notarized; macOS may show an opening warning, and after an update it may require granting Accessibility access again.
- **Windows:** allow camera access in Privacy & security settings.
- **Linux:** use an X11 session and grant the logged-in user access to the webcam. Wayland may restrict global pointer movement and clicks; the app verifies system pointer movement and reports when the desktop blocks it.
- Movement maps to the primary display. Lighting, camera placement, hand visibility, and motion blur affect tracking. The preview halo follows the fingertip, but the OS cursor is moved separately; the app stops with an error if the desktop refuses that movement.
- A wide camera mode can request a wider sensor mode. Software cannot widen a camera lens or force a driver to expose a different physical field of view.
- This is a desktop app. It does not control the system pointer on Android or iOS.

## Build locally

Install `requirements.txt`, then build on the target operating system and architecture:

```sh
python -m PyInstaller --noconfirm AI_Classroom_Mouse.spec
```

PyInstaller does not cross-compile camera, Qt, or mouse-control libraries. The GitHub Actions workflow performs native builds for Windows x64, macOS Apple silicon, macOS Intel, and Linux x64. Review the licenses for Qt, MediaPipe, OpenCV, and other bundled dependencies before redistributing binaries.

## Website download page

`website/downloads.html` is deployed through GitHub Pages at [code-a-difference.github.io/Finger-Tracking-Mouse](https://code-a-difference.github.io/Finger-Tracking-Mouse/). The page can also be copied to another site host later.

## License

Finger Mouse source code is licensed under the [MIT License](LICENSE). Third-party libraries retain their own licenses.
