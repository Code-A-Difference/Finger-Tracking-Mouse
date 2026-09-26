"""Download MediaPipe's hand and face landmark models into models/, checking
their SHA-256.

Run once before running from source or building:

    python tools/fetch_model.py

Both models (Apache 2.0, from Google's MediaPipe model repository) are
pinned by hash in hand_tracker.py and eye_tracker.py; a file that doesn't
match either is refused. Installed copies of Finger Mouse carry the models
inside the app and never download anything.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hand_tracker import MODEL_NAME as HAND_MODEL_NAME, MODEL_SHA256 as HAND_MODEL_SHA256, \
    MODEL_URL as HAND_MODEL_URL  # noqa: E402
from eye_tracker import MODEL_NAME as EYE_MODEL_NAME, MODEL_SHA256 as EYE_MODEL_SHA256, \
    MODEL_URL as EYE_MODEL_URL  # noqa: E402

MODELS = [
    (HAND_MODEL_NAME, HAND_MODEL_URL, HAND_MODEL_SHA256),
    (EYE_MODEL_NAME, EYE_MODEL_URL, EYE_MODEL_SHA256),
]


def fetch(name: str, url: str, sha256: str) -> bool:
    target = ROOT / "models" / name
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == sha256:
        print(f"Model already present: {target}")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        print(f"Refusing the download: SHA-256 {digest} is not the pinned {sha256}.", file=sys.stderr)
        return False
    temporary = target.with_suffix(".part")
    temporary.write_bytes(data)
    temporary.replace(target)
    print(f"Saved {target} ({len(data):,} bytes, SHA-256 verified)")
    return True


def main() -> int:
    ok = True
    for name, url, sha256 in MODELS:
        ok = fetch(name, url, sha256) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
