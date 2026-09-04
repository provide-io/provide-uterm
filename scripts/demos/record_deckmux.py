# Split shim to keep file size below 500 LOC; the implementation lives in
# record_deckmux_impl.py. Re-export ``record`` so the orchestrator
# (record_all_demos.py) can ``import scripts.demos.record_deckmux`` and call
# ``.record()``, while ``python record_deckmux.py [--run-demo]`` still works.
from __future__ import annotations

from scripts.demos.record_deckmux_impl import (
    DESCRIPTION,
    FEATURE,
    HIGHLIGHT_DURATION_S,
    HIGHLIGHT_START_S,
    PRIMARY_VIDEO,
    SUBTITLE,
    TITLE,
    record,
)

# The site manifest is harvested by importing THIS module and reading these
# constants off it (build_site_manifest.py::_feature_metadata). Re-exporting
# only ``record`` left every one of them missing, so a regenerated manifest
# silently described the demo as title "Deckmux", no subtitle, no description,
# 0.0 duration -- and the manifest on disk only looked right because it had not
# been regenerated since this file was split for the LOC limit.
__all__ = [
    "DESCRIPTION",
    "FEATURE",
    "HIGHLIGHT_DURATION_S",
    "HIGHLIGHT_START_S",
    "PRIMARY_VIDEO",
    "SUBTITLE",
    "TITLE",
    "record",
]

if __name__ == "__main__":
    import runpy
    from pathlib import Path

    runpy.run_path(str(Path(__file__).with_name("record_deckmux_impl.py")), run_name="__main__")
