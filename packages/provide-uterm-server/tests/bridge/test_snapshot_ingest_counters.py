#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Reader ingest counters must survive the whole trip to the consumer.

A consumer watching a frozen screen cannot tell "no bytes ever reached the
worker" from "bytes arrived and the emulator never reflected them". Those need
opposite fixes and look identical from the screen tail, so the worker's own
reader counts are carried on the snapshot.

They are only useful END TO END, and the hub REBUILDS each worker frame from an
explicit field list — so a field nobody forwards is silently dropped in transit
while every unit test on either side still passes. That is the failure this
module exists to catch.
"""

from __future__ import annotations

from typing import Any

from provide.uterm.server.bridge.frames import make_snapshot_frame
from provide.uterm.server.bridge.routes.websockets_worker import _build_worker_frame, _opt_int


def _worker_snapshot(**over: Any) -> dict[str, Any]:
    """A snapshot shaped as a worker puts it on the wire."""
    base: dict[str, Any] = {
        "type": "snapshot",
        "screen": "sector 1",
        "cursor": {"x": 1, "y": 2},
        "cols": 80,
        "rows": 25,
        "screen_hash": "hash-a",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": None,
        "raw_tail": "sector 1",
        "chunks_read": 61,
        "bytes_read": 9620,
        "ts": 1.0,
    }
    base.update(over)
    return base


class TestBuilderCarriesCounters:
    def test_make_snapshot_frame_emits_both(self) -> None:
        frame = make_snapshot_frame(
            screen="s",
            cursor={"x": 0, "y": 0},
            cols=80,
            rows=25,
            screen_hash="h",
            cursor_at_end=True,
            has_trailing_space=False,
            prompt_detected=None,
            ts=1.0,
            chunks_read=61,
            bytes_read=9620,
        )

        assert frame["chunks_read"] == 61
        assert frame["bytes_read"] == 9620

    def test_absent_counters_stay_none_not_zero(self) -> None:
        """A worker predating the counters must not look like one that read nothing."""
        frame = make_snapshot_frame(
            screen="s",
            cursor={"x": 0, "y": 0},
            cols=80,
            rows=25,
            screen_hash="h",
            cursor_at_end=True,
            has_trailing_space=False,
            prompt_detected=None,
            ts=1.0,
        )

        assert frame["chunks_read"] is None
        assert frame["bytes_read"] is None


class TestHubForwardsCounters:
    def test_hub_rebuild_preserves_counters(self) -> None:
        """The hub rebuilds from a field list — unlisted fields vanish here."""
        rebuilt = _build_worker_frame("snapshot", _worker_snapshot())

        assert rebuilt["chunks_read"] == 61, "the hub dropped chunks_read in transit"
        assert rebuilt["bytes_read"] == 9620, "the hub dropped bytes_read in transit"

    def test_hub_rebuild_without_counters_yields_none(self) -> None:
        snap = _worker_snapshot()
        del snap["chunks_read"]
        del snap["bytes_read"]

        rebuilt = _build_worker_frame("snapshot", snap)

        assert rebuilt["chunks_read"] is None
        assert rebuilt["bytes_read"] is None

    def test_hub_rebuild_survives_a_garbage_counter(self) -> None:
        """A malformed counter degrades to None rather than raising mid-rebuild."""
        rebuilt = _build_worker_frame("snapshot", _worker_snapshot(chunks_read="lots"))

        assert rebuilt["chunks_read"] is None
        assert rebuilt["bytes_read"] == 9620


class TestOptInt:
    def test_passes_through_an_int(self) -> None:
        assert _opt_int(61) == 61

    def test_zero_is_preserved_not_treated_as_absent(self) -> None:
        """Zero is a real reading — "read nothing" is exactly what we look for."""
        assert _opt_int(0) == 0

    def test_missing_becomes_none(self) -> None:
        assert _opt_int(None) is None

    def test_non_int_becomes_none(self) -> None:
        assert _opt_int("61") is None
        assert _opt_int(1.5) is None

    def test_bool_is_not_an_int_here(self) -> None:
        """``bool`` subclasses ``int``; a True must not read as one chunk."""
        assert _opt_int(True) is None
        assert _opt_int(False) is None
