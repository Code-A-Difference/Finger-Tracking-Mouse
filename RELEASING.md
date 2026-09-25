# Releasing Finger Mouse

The GitHub Actions build creates a Windows installer and macOS disk images from
the tagged source. GitHub release assets are the download-page source of truth.

## Windows signing

Add these GitHub Actions secrets: `WINDOWS_CERT_BASE64`, `WINDOWS_CERT_PASSWORD`,
and `WINDOWS_CERT_THUMBPRINT`. The certificate must be an Authenticode code-signing
certificate issued to Code-A-Difference. The workflow decodes it only on the
Windows runner, signs the installer with `signtool`, and removes the certificate
file. Never commit certificates, passwords, or signing tokens. New signed apps
can still show Microsoft reputation warnings until they build reputation.

## macOS signing and notarization

Use an Apple Developer ID Application certificate and an App Store Connect API
key stored as GitHub secrets. Sign the `.app` with `codesign --options runtime`,
submit its DMG with `xcrun notarytool submit --wait`, then staple with
`xcrun stapler staple`. The current CI creates unsigned DMGs unless those secrets
and steps are enabled. Users will otherwise see Gatekeeper warnings.

## Linux

Linux remains source/X11 supported. Publish an AppImage or distribution package
only after testing it on its target distribution; do not point users at an
untested binary. Wayland may prevent global pointer control.
