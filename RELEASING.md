# Releasing Finger Mouse

Every push to `main` builds, tests and packages Finger Mouse on Windows,
macOS (Apple silicon and Intel) and Linux, runs each built app's self-test,
and keeps the installers as workflow artifacts. Pushing a version tag does
the same, signs whatever it has credentials for, and publishes a GitHub
Release. The download pages read the latest release, so they update by
themselves.

## Cutting a release

1. Set `APP_VERSION` in `app_version.py` (for example `2.2.1`). The build
   refuses a tag that doesn't match it.
2. Add a section to `CHANGES.md`.
3. Commit, push, and wait for the **Build, test and release** workflow to go
   green on `main`.
4. Tag and push the tag:

   ```sh
   git tag v2.2.1
   git push origin v2.2.1
   ```

5. The workflow publishes the release with these files, plus
   `SHA256SUMS.txt`, then refreshes the GitHub Pages download page:

   | File | What it is |
   |---|---|
   | `FingerMouse-windows-x64-setup.exe` | Inno Setup installer; per-user by default, no administrator prompt |
   | `FingerMouse-macos-arm64.dmg` | Apple silicon, macOS 13+ |
   | `FingerMouse-macos-x64.dmg` | Intel Macs, macOS 13+ |
   | `FingerMouse-linux-x86_64.AppImage` | one executable file; glibc 2.35+ (Ubuntu 22.04, Debian 12, Fedora 36 and later) |

   The Code A Difference project page caches the release list for up to an
   hour.

Nothing here uploads a ZIP, and the download pages never offer one: a
platform without an installer in the latest release shows "Not available
yet" instead of a link.

## Signing

Signing is optional. Unsigned builds work, but Windows SmartScreen and macOS
Gatekeeper warn about them. All credentials go in **GitHub → Settings →
Secrets and variables → Actions**. None ever belong in the repository, and
the workflow only uses them for tag builds, never for pull requests.

**Signing reduces warnings; it doesn't remove them overnight.** Windows
SmartScreen also weighs an app's *reputation* — how many people have
downloaded and run it without trouble — so a newly signed app, or the first
release under a new certificate, can still show "Windows protected your PC"
for a while. macOS is stricter but clearer: a Developer-ID-signed *and
notarized* app opens without the "unidentified developer" block. Nothing in
this project tries to get around either system; the only fix is signing and
time.

### Windows (Authenticode)

The workflow supports two ways. Use whichever matches your certificate.

**A. Azure Trusted Signing** (Microsoft's signing service; certificates are
held in Microsoft's HSM, which is how new certificates have to be stored
since June 2023). Create a Trusted Signing account and certificate profile,
and an Entra app registration with the *Trusted Signing Certificate Profile
Signer* role on it. Add secrets:

| Secret | Value |
|---|---|
| `AZURE_TENANT_ID` | directory (tenant) ID |
| `AZURE_CLIENT_ID` | app registration's client ID |
| `AZURE_CLIENT_SECRET` | its client secret |
| `AZURE_SIGNING_ENDPOINT` | the account's region endpoint, e.g. `https://eus.codesigning.azure.net/` |
| `AZURE_SIGNING_ACCOUNT` | Trusted Signing account name |
| `AZURE_SIGNING_PROFILE` | certificate profile name |

The workflow signs `FingerMouse.exe` before packaging and the installer
after it, with SHA-256 and an RFC 3161 timestamp.

**B. An exportable .pfx certificate** (older OV/EV certificates issued
before the hardware-key rule). Add:

| Secret | Value |
|---|---|
| `WINDOWS_CERT_PFX_BASE64` | the .pfx, base64 (`[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))`) |
| `WINDOWS_CERT_PASSWORD` | its password |

`packaging/windows/build_installer.ps1` imports it into the runner's
certificate store for the build only (the password never appears on a
command line), signs `FingerMouse.exe`, then has Inno Setup sign both the
installer and the uninstaller it writes, and removes the certificate
afterwards. Every signature is SHA-256 and timestamped, so it stays valid
after the certificate expires. This path was tested end to end with a
throwaway self-signed certificate.

The certificate's subject should be **Code-A-Difference**, the publisher
name used in the app's version resource and the installer, so what Windows
shows matches.

### macOS (Developer ID + notarization)

Needs an Apple Developer Program membership (paid, yearly).

1. Create a **Developer ID Application** certificate; export it with its
   private key from Keychain Access as a `.p12`.
2. Create an **App Store Connect API key** (Users and Access → Integrations →
   Keys) with the Developer role; download the `.p8`.
3. Add secrets:

| Secret | Value |
|---|---|
| `MACOS_CERT_P12_BASE64` | the .p12, base64 (`base64 -i cert.p12`) |
| `MACOS_CERT_PASSWORD` | its export password |
| `MACOS_SIGN_IDENTITY` | e.g. `Developer ID Application: Code-A-Difference (ABCDE12345)` |
| `MACOS_NOTARY_KEY_P8_BASE64` | the .p8, base64 |
| `MACOS_NOTARY_KEY_ID` | the key's ID |
| `MACOS_NOTARY_ISSUER` | the issuer ID shown above the keys list |

The workflow puts the certificate in a temporary keychain, and PyInstaller
signs every binary in the app with the hardened runtime and
`packaging/macos/entitlements.plist` (camera access, plus the executable
memory Python's ctypes needs). `packaging/macos/build_dmg.sh` then seals the
app, builds and signs the disk image, submits it with `xcrun notarytool
submit --wait`, staples the ticket (`xcrun stapler staple`) and checks it
with `spctl`. Without these secrets the app keeps an ad-hoc signature: it
runs, but Gatekeeper asks people to right-click → Open the first time.

Pointer control needs no entitlement: macOS asks the person to allow Finger
Mouse under Privacy & Security → Accessibility. macOS ties that permission
to the app's signature, so signed releases keep it across updates; unsigned
ones may have to be allowed again after each update.

### Linux

AppImages aren't code-signed in any way desktops check. The release's
`SHA256SUMS.txt` lets people verify a download:
`sha256sum -c SHA256SUMS.txt --ignore-missing`. The workflow runs the
finished AppImage's self-test inside a clean Ubuntu 24.04 container before
publishing, to catch a library that only happened to be on the build
machine.

## Building locally

```sh
python -m pip install -r requirements.txt
python tools/fetch_model.py
python -m pytest
python -m PyInstaller --noconfirm FingerMouse.spec
```

Then, on the matching system:

- Windows: `packaging\windows\build_installer.ps1 -Version 2.2.0` (needs
  [Inno Setup 6](https://jrsoftware.org/isinfo.php)).
- macOS: `packaging/macos/build_dmg.sh arm64` (or `x64`).
- Linux: `packaging/linux/build_appimage.sh 2.2.0`.

Check any build with `FingerMouse --self-test result.json`. It loads the
hand model and runs it once, starts Qt offscreen, and checks the pointer
backend and camera listing; exit code 0 means all passed.

On Windows, keep the checkout and virtual environment at a short path:
Windows won't load a DLL whose full path is longer than 260 characters, and
a Qt plugin buried in a long path fails with a message box that doesn't
appear when run from a script, so it looks like a hang.
