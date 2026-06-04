#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the :class:`WorkerRegistry` service.

These exercises target the explicit accessor surface that the hub
mixins do not yet use directly — they will be the primary API as the
remaining phases of the refactor migrate call sites off the raw
``_workers`` dict.
"""

from __future__ import annotations

import pytest

from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.models import WorkerTermState


def _state() -> WorkerTermState:
    return WorkerTermState()


def test_registry_starts_empty() -> None:
    r = WorkerRegistry()
    assert len(r) == 0
    assert r.get("nope") is None
    assert not r.contains("nope")
    assert "nope" not in r
    assert r.all() == []
    assert r.keys() == []
    assert r.items() == []
    assert list(iter(r)) == []


def test_registry_put_get_pop_roundtrip() -> None:
    r = WorkerRegistry()
    st = _state()
    r.put("w1", st)
    assert r.get("w1") is st
    assert r.contains("w1")
    assert "w1" in r
    assert len(r) == 1
    assert r.keys() == ["w1"]
    assert r.all() == [st]
    assert r.items() == [("w1", st)]
    assert list(iter(r)) == ["w1"]
    popped = r.pop("w1")
    assert popped is st
    assert r.get("w1") is None
    assert r.pop("w1") is None  # absent -> None


def test_registry_setdefault_keeps_existing() -> None:
    r = WorkerRegistry()
    first = _state()
    second = _state()
    assert r.setdefault("w1", first) is first
    assert r.setdefault("w1", second) is first  # not replaced
    assert r.get("w1") is first


def test_registry_discard_returns_truth() -> None:
    r = WorkerRegistry()
    r.put("w1", _state())
    assert r.discard("w1") is True
    assert r.discard("w1") is False


def test_registry_require_raises_on_missing() -> None:
    r = WorkerRegistry()
    st = _state()
    r.put("w1", st)
    assert r.require("w1") is st
    with pytest.raises(KeyError) as excinfo:
        r.require("missing")
    # The KeyError names the missing worker (pins KeyError(worker_id) vs KeyError(None)).
    assert excinfo.value.args == ("missing",)
