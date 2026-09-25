"""Turn pytest failures into GitHub Actions annotations.

    python tools/ci_annotate.py pytest.log

Annotations show on the workflow run page and through the public API, so a
failing test's name and error can be read without opening the full log
(which GitHub only shows to signed-in users).
"""

import re
import sys


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main(path: str) -> None:
    log = open(path, encoding="utf-8", errors="replace").read()
    summary = log.split("short test summary info", 1)[-1] if "short test summary info" in log else ""
    found = False
    for line in summary.splitlines():
        m = re.match(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$", line.strip())
        if not m:
            continue
        found = True
        kind, test, message = m.groups()
        # The traceback lines pytest prints for this test ("E   ..."), briefly.
        name = test.split("::")[-1]
        block = log.split(f"_ {name} _", 1)[-1] if f"_ {name} _" in log else ""
        details = [l for l in block.splitlines() if l.startswith("E ")][:15]
        body = (message or "") + ("\n" + "\n".join(details) if details else "")
        print(f"::error title={kind} {escape(test)}::{escape(body[:3000])}")
    if not found:
        lines = log.strip().splitlines()
        # A crash: show where it started (which thread, which test), not just the end.
        start = next((i for i, l in enumerate(lines) if "Fatal Python error" in l), None)
        excerpt = lines[start:start + 45] if start is not None else lines[-40:]
        print(f"::error title=pytest crashed::{escape(chr(10).join(excerpt)[:4000])}")
        before = [l for l in lines[:start or 0] if "Check failed" in l or l.startswith("F0")][-5:]
        before += lines[max(0, (start or 0) - 12):start or 0]
        if before:
            print(f"::error title=pytest output before the crash::{escape(chr(10).join(before)[:2000])}")


if __name__ == "__main__":
    main(sys.argv[1])
