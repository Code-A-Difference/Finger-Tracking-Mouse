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
        tail = "\n".join(log.strip().splitlines()[-40:])
        print(f"::error title=pytest::{escape(tail[:3000])}")


if __name__ == "__main__":
    main(sys.argv[1])
