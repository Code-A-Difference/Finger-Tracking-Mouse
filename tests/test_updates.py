"""The update check's decisions (updates.py) — no network, no Qt."""
import updates

ASSETS = [
    {"name": "FingerMouse-windows-x64-setup.exe", "browser_download_url": "https://x/win.exe"},
    {"name": "FingerMouse-macos-arm64.dmg", "browser_download_url": "https://x/arm.dmg"},
    {"name": "FingerMouse-macos-x64.dmg", "browser_download_url": "https://x/intel.dmg"},
    {"name": "FingerMouse-linux-x86_64.AppImage", "browser_download_url": "https://x/linux.AppImage"},
]


def rel(tag, **kw):
    d = {"tag_name": tag, "body": "### New\n- A thing", "assets": ASSETS, "html_url": "https://x/page"}
    d.update(kw)
    return d


def test_versions_compare_as_numbers():
    assert updates.is_newer("v2.10.0", "2.9.9")
    assert not updates.is_newer("v2.7.0", "2.7.0")
    assert not updates.is_newer("v2.6.9", "2.7.0")
    assert updates.parse_version("garbage") == (0, 0, 0)


def test_offers_a_newer_release_with_this_platforms_installer():
    o = updates.offer(rel("v2.8.0"), "2.7.0", plat="win32")
    assert o["version"] == "2.8.0" and o["url"] == "https://x/win.exe" and "A thing" in o["notes"]
    assert updates.offer(rel("v2.8.0"), "2.7.0", plat="darwin", machine="arm64")["url"] == "https://x/arm.dmg"
    assert updates.offer(rel("v2.8.0"), "2.7.0", plat="darwin", machine="x86_64")["url"] == "https://x/intel.dmg"
    assert updates.offer(rel("v2.8.0"), "2.7.0", plat="linux")["url"] == "https://x/linux.AppImage"


def test_nothing_to_offer():
    assert updates.offer(rel("v2.7.0"), "2.7.0") is None                  # same version
    assert updates.offer(rel("v2.8.0"), "2.7.0", skipped="2.8.0") is None  # skipped by the user
    assert updates.offer(rel("v2.8.0", prerelease=True), "2.7.0") is None
    assert updates.offer({}, "2.7.0") is None


def test_skipping_one_version_still_offers_the_next():
    assert updates.offer(rel("v2.9.0"), "2.7.0", skipped="2.8.0")["version"] == "2.9.0"


def test_missing_installer_falls_back_to_the_release_page():
    assert updates.offer(rel("v2.8.0", assets=[]), "2.7.0", plat="win32")["url"] == "https://x/page"


def test_long_notes_are_trimmed():
    o = updates.offer(rel("v2.8.0", body="line\n" * 1000), "2.7.0")
    assert len(o["notes"]) < 2000 and o["notes"].endswith("release page.")
