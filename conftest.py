"""Root conftest — copied by mutmut to mutants/conftest.py.

When mutmut runs pytest from mutants/, this file ensures that
source imports resolve to the mutated copies rather than the
editable install in .venv.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if os.environ.get("MUTANT_UNDER_TEST"):
    _here = Path(__file__).resolve().parent  # mutants/ when run by mutmut
    _mutated_src = _here / "src"
    if _mutated_src.exists():
        # Prepend mutants/src so mutated copies take priority over the editable install.
        sys.path.insert(0, str(_mutated_src))
