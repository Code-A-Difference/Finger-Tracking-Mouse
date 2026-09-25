# Add Finger Mouse downloads to codeadifference.ct.ws

`downloads.html` is a standalone, mobile-friendly download page. It links to the latest release assets in the public `Code-A-Difference/Finger-Tracking-Mouse` repository.

1. Publish a version tag such as `v2.0.0` in GitHub. The Actions workflow builds Windows x64, macOS Apple silicon, macOS Intel, and Linux x64 archives and attaches them to the release.
2. Upload `downloads.html` to your website host, for example as `public_html/finger-mouse/index.html`.
3. Add a link to `https://codeadifference.ct.ws/finger-mouse/` from your existing website navigation or downloads section.

The source workspace does not include website-hosting credentials or files, so the existing website cannot be updated from this project folder. The GitHub release links on the page work after the first release is published.
