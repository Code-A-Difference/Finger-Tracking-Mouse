# Finger Mouse download page

`downloads.html` is the download page GitHub Pages publishes at
https://code-a-difference.github.io/Finger-Tracking-Mouse/.

The Pages workflow publishes it with a `release.json` snapshot of the latest
release's files beside it, and the release workflow re-publishes it after
each release. The page links only real installers from that release and
labels any platform without one "Not available yet". It highlights the
visitor's platform but keeps all four on offer. If `release.json` is
missing, it asks the GitHub API directly.
