"""Thread subclasses must not reuse threading.Thread's private names.

Python 3.12's Thread.join() calls self._stop(); a subclass that stores a
flag called _stop breaks join() there (and only there — 3.13 renamed it),
which is how every thread in the app once failed on 3.12 but not 3.14.
"""

import threading

import camera
import diagnostics
import pointer_output
import tracking_engine

THREAD_CLASSES = [camera.FrameGrabber, diagnostics.Watchdog, pointer_output.PointerOutput,
                  tracking_engine.TrackingEngine]
RESERVED = {"_stop", "_started", "_is_stopped", "_tstate_lock", "_target", "_args", "_kwargs",
            "_name", "_daemonic", "_ident", "_native_id", "_initialized", "_handle", "_invoke_excepthook"}


def test_no_thread_subclass_shadows_thread_internals():
    import ast
    import inspect

    for cls in THREAD_CLASSES:
        assert issubclass(cls, threading.Thread)
        source = inspect.getsource(cls)
        assigned = {node.attr for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                    and isinstance(node.value, ast.Name) and node.value.id == "self"}
        assert not (assigned & RESERVED), f"{cls.__name__} assigns {assigned & RESERVED}"


def test_watchdog_starts_and_joins():
    w = diagnostics.Watchdog(interval=0.01)
    w.start()
    w.stop()
    w.join(2)
    assert not w.is_alive()
