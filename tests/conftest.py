import sys
from pathlib import Path

# Tests import the app's modules straight from the repository root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_collection_modifyitems(session, config, items):
    # The whole-app end-to-end test runs last: if native code in it ever
    # crashes the process, every other result has already been reported.
    items.sort(key=lambda item: item.fspath.basename == "test_app_smoke.py")
