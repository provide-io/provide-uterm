# Auto-split wrapper to keep file size below 500 LOC.
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_part(name: str) -> None:
    part_path = _HERE / name
    module_name = f"{__package__}.{part_path.stem}" if __package__ else f"{__name__}.{part_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, part_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load split test module: {part_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    for key, value in vars(module).items():
        if key.startswith("__") and key not in {"__doc__"}:
            continue
        globals().setdefault(key, value)


_load_part("test_ssh_part1.py")
_load_part("test_ssh_part2.py")
