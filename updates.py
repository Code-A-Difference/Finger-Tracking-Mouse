"""Telling people when a newer Finger Mouse is out.

At start-up the app asks GitHub (once, in the background, a few seconds after
it opens) for the latest release. If it's newer than this copy, the window
shows what's new (the release notes) with a Download button for this
platform's installer, Later, and Skip this version.

Nothing is downloaded or installed by itself: an installer that runs on its
own is a much bigger trust question than a button. Only the version number
and notes are read; no information about the computer is sent.

The parts here are pure (no Qt, no network) apart from ``fetch_latest``, so
tests/test_updates.py checks them directly.
"""
from __future__ import annotations

import json
import platform
import re
import sys
import urllib.request
from typing import Optional

RELEASES_API = "https://api.github.com/repos/Code-A-Difference/Finger-Tracking-Mouse/releases/latest"
RELEASES_PAGE = "https://github.com/Code-A-Difference/Finger-Tracking-Mouse/releases/latest"


def parse_version(v: str) -> tuple[int, int, int]:
    """'v2.10.1' -> (2, 10, 1). Anything unreadable counts as 0.0.0."""
    m = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", v or "")
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)) if m else (0, 0, 0)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def asset_for(assets: list[dict], plat: str = sys.platform, machine: str = "") -> Optional[str]:
    """The installer's download URL for this computer, or None."""
    machine = (machine or platform.machine()).lower()
    if plat.startswith("win"):
        want = "windows-x64-setup.exe"
    elif plat == "darwin":
        want = "macos-arm64.dmg" if machine in ("arm64", "aarch64") else "macos-x64.dmg"
    else:
        want = "linux-x86_64.AppImage"
    for a in assets or []:
        if str(a.get("name", "")).endswith(want):
            return a.get("browser_download_url")
    return None


def notes_text(body: str, limit: int = 1800) -> str:
    """Release notes as plain-ish Markdown for the dialog: no HTML comments, trimmed."""
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S).strip()
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n\n…and more on the release page."
    return body or "Fixes and improvements."


def offer(release: dict, current: str, skipped: str = "", plat: str = sys.platform, machine: str = "") -> Optional[dict]:
    """What to show for a release dict from GitHub, or None when there's nothing to offer."""
    tag = str(release.get("tag_name") or "")
    if not tag or release.get("draft") or release.get("prerelease"):
        return None
    if not is_newer(tag, current):
        return None
    version = ".".join(str(n) for n in parse_version(tag))
    if skipped and parse_version(skipped) == parse_version(version):
        return None
    return {
        "version": version,
        "notes": notes_text(str(release.get("body") or "")),
        "url": asset_for(release.get("assets") or [], plat, machine) or str(release.get("html_url") or RELEASES_PAGE),
        "page": str(release.get("html_url") or RELEASES_PAGE),
    }


def fetch_latest(timeout: float = 8.0) -> Optional[dict]:
    """The latest release from GitHub, or None (offline, rate-limited, anything)."""
    try:
        req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                            "User-Agent": "FingerMouse-update-check"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
