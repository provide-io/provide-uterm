# Split shim to keep file size below 500 LOC while preserving import paths.
from __future__ import annotations

from importlib import import_module

_impl = import_module(".websockets_impl", __package__)
for _name, _value in vars(_impl).items():
    if _name in {"__name__", "__package__", "__loader__", "__spec__", "__file__", "__cached__"}:
        continue
    globals()[_name] = _value
