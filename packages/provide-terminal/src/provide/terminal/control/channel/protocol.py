from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class BrowserControlMessage:
    type: str
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerControlMessage:
    type: str
    payload: Mapping[str, object] = field(default_factory=dict)
