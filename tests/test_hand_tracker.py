import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import hand_tracker

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not (ROOT / "models" / "hand_landmarker.task").exists(), reason="model not downloaded")
def test_bundle_check_needs_no_graph():
    assert "SHA-256 ok" in hand_tracker.check_bundle()


def test_a_damaged_or_missing_model_is_refused(tmp_path):
    bad = tmp_path / "hand_landmarker.task"
    bad.write_bytes(b"not a model")
    with pytest.raises(RuntimeError, match="damaged"):
        hand_tracker.load_model_bytes(bad)
    with pytest.raises(RuntimeError, match="fetch_model"):
        hand_tracker.load_model_bytes(tmp_path / "missing.task")


@pytest.mark.skipif(not (ROOT / "models" / "hand_landmarker.task").exists(), reason="model not downloaded")
@pytest.mark.skipif(not hand_tracker.start_check()[0],
                       reason="MediaPipe can't open the hand model on this machine (e.g. a macOS CI VM)")
def test_hand_tracking_starts_without_the_portaudio_library():
    # On Linux, `import sounddevice` raises OSError when PortAudio isn't
    # installed, and MediaPipe imports it on load. Simulate that here.
    script = textwrap.dedent(f"""
        import sys, importlib.abc
        sys.path.insert(0, {str(ROOT)!r})

        class NoPortAudio(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "sounddevice":
                    raise OSError("PortAudio library not found")
                return None

        sys.meta_path.insert(0, NoPortAudio())
        import numpy as np, hand_tracker
        t = hand_tracker.HandTracker()
        assert t.process(np.zeros((240, 320, 3), np.uint8)) is None
        t.close()
        print("started", t.kind)
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert "started tasks" in result.stdout, result.stderr[-2000:]
