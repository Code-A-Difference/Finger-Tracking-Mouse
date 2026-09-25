import sys
from pathlib import Path

# Tests import the app's modules straight from the repository root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
