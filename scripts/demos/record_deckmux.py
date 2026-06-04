# Split shim to keep file size below 500 LOC; the implementation lives in
# record_deckmux_impl.py. Re-export ``record`` so the orchestrator
# (record_all_demos.py) can ``import scripts.demos.record_deckmux`` and call
# ``.record()``, while ``python record_deckmux.py [--run-demo]`` still works.
from __future__ import annotations

from scripts.demos.record_deckmux_impl import record

__all__ = ["record"]

if __name__ == "__main__":
    import runpy
    from pathlib import Path

    runpy.run_path(str(Path(__file__).with_name("record_deckmux_impl.py")), run_name="__main__")
