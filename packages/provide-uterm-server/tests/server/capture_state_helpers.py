#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared helpers for the capture-state suites.

A plain module rather than a conftest: the suites call these with arguments,
and mutmut runs them with ``--import-mode=importlib``, under which a relative
import of a sibling module works and an import of ``tests.server`` does not.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, Any

from provide.uterm.server.capture_state import CaptureRecord, CaptureStateStore
from provide.uterm.server.config_schema import SessionDefinition

if TYPE_CHECKING:
    from pathlib import Path

SID = "pam-suokki-capture-4242-777"
DAY = 86400.0


class Clock:
    """A settable wall clock for a store."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def definition(session_id: str = SID, *, owner: str | None = "alice", **fields: Any) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name="suokki (pts/3) [live]",
        connector_type="pty_capture",
        connector_config={"socket_path": "/run/uterm-capture/uterm-cap-4242.sock"},
        input_mode="hijack",
        tags=["pam", "capture", "suokki"],
        owner=owner,
        visibility="private",
        **fields,
    )


def store(
    tmp_path: Path, *, persist: bool = True, keep_days: int = 7, lines: int = 1000, **kw: Any
) -> CaptureStateStore:
    return CaptureStateStore(tmp_path / "state", persist=persist, keep_days=keep_days, scrollback_lines=lines, **kw)


def snapshot(screen: str = "$ ls\nfile", cols: int = 80, rows: int = 24) -> dict[str, Any]:
    return {"type": "snapshot", "screen": screen, "cols": cols, "rows": rows}


def live_path(tmp_path: Path, session_id: str = SID) -> Path:
    return tmp_path / "state" / "live" / f"{session_id}.json"


def previous_path(tmp_path: Path, session_id: str = SID) -> Path:
    return tmp_path / "state" / "previous" / f"{session_id}.json"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def private_dir(path: Path) -> Path:
    """*path* and its missing parents, each 0700, as the store makes its own."""
    for parent in reversed([path, *path.parents]):
        if not parent.exists():
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
    return path


def valid(kind: str = "live", **changes: Any) -> dict[str, Any]:
    """A record of *kind* as the store writes it, with *changes* over it."""
    record = CaptureRecord(
        definition=definition(),
        recording_instance="inst",
        screen="s",
        cols=80,
        rows=24,
        scrollback=["a"],
        saved_at=5.0,
        ended_at=None if kind == "live" else 6.0,
    )
    return record.to_json() | changes
