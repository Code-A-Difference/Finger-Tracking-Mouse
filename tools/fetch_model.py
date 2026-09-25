"""Download MediaPipe's hand landmark model into models/, checking its SHA-256.

Run once before running from source or building:

    python tools/fetch_model.py

The model (Apache 2.0, from Google's MediaPipe model repository) is pinned
by hash in hand_tracker.py; a file that doesn't match is refused. Installed
copies of Finger Mouse carry the model inside the app and never download
anything.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hand_tracker import MODEL_NAME, MODEL_SHA256, MODEL_URL  # noqa: E402


def main() -> int:
    target = ROOT / "models" / MODEL_NAME
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == MODEL_SHA256:
        print(f"Model already present: {target}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {MODEL_URL}")
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != MODEL_SHA256:
        print(f"Refusing the download: SHA-256 {digest} is not the pinned {MODEL_SHA256}.", file=sys.stderr)
        return 1
    temporary = target.with_suffix(".part")
    temporary.write_bytes(data)
    temporary.replace(target)
    print(f"Saved {target} ({len(data):,} bytes, SHA-256 verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
