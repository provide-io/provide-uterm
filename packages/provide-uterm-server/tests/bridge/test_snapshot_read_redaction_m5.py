#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""M5 (verify): the snapshot READ paths must honour the output-redaction policy.

The live broadcast path redacts ``snapshot`` frames per recipient role, but the
two snapshot READ paths shipped the stored ``last_snapshot`` verbatim:

* REST ``GET /sessions/{id}/snapshot`` → ``get_last_snapshot`` → raw screen.
* WS ``initial_snapshot`` at browser connect → raw screen to the browser.

These tests pin both reads to the SAME role-scoped redaction the broadcast path
uses, without mutating the stored ``last_snapshot`` (redaction is applied to a
copy). With no gate configured, the read paths return the raw snapshot
unchanged (a session viewer legitimately sees the screen).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import OutputPolicyGate, RedactionRule
from provide.uterm.server.bridge.identity import Principal

_STRIPE_RULES = [RedactionRule(pattern=r"sk_live_\w+", replacement="[REDACTED]")]


class MockOutputPolicy(OutputPolicyGate):
    def __init__(self, rules: list[RedactionRule]) -> None:
        self.rules = rules

    async def get_redaction_rules(self, _context: Any) -> list[RedactionRule]:
        return self.rules


def _secret_snapshot() -> dict[str, Any]:
    return {
        "type": "snapshot",
        "screen": "key sk_live_SECRET active",
        "raw_tail": "sk_live_SECRET",
        "cursor": {"x": 0, "y": 0},
        "cols": 80,
        "rows": 25,
        "screen_hash": "h1",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"matched": "login sk_live_SECRET", "name": "login"},
        "ts": 1.0,
    }


class _PrincipalWS:
    def __init__(self, subject_id: str = "viewer1") -> None:
        self.state = type(
            "S",
            (),
            {"uterm_principal": Principal(subject_id=subject_id, roles=frozenset({"viewer"}), scopes=frozenset())},
        )()


# ---------------------------------------------------------------------------
# get_last_snapshot recipient redaction (REST read path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_last_snapshot_redacts_for_recipient_under_policy() -> None:
    """With a gate, get_last_snapshot(recipient=...) returns a redacted copy."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-read"

    await hub.register_worker(worker_id, AsyncMock())
    await hub.update_last_snapshot(worker_id, _secret_snapshot())

    recipient = _PrincipalWS()
    snap = await hub.get_last_snapshot(worker_id, recipient=recipient)
    assert snap is not None
    assert "sk_live_" not in snap["screen"], f"screen not redacted: {snap['screen']!r}"
    assert "[REDACTED]" in snap["screen"]
    assert "sk_live_" not in str(snap["raw_tail"]), "raw_tail not redacted"
    assert "sk_live_" not in str(snap["prompt_detected"]), "prompt_detected not redacted"


@pytest.mark.asyncio
async def test_get_last_snapshot_does_not_mutate_stored_snapshot() -> None:
    """Redaction is applied to a copy; the stored last_snapshot stays raw."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-read-copy"

    await hub.register_worker(worker_id, AsyncMock())
    await hub.update_last_snapshot(worker_id, _secret_snapshot())

    recipient = _PrincipalWS()
    await hub.get_last_snapshot(worker_id, recipient=recipient)

    # The stored snapshot is unchanged → a second read still has the raw secret,
    # and broadcasting from the stored copy is unaffected.
    raw = await hub.get_last_snapshot(worker_id)  # no recipient → raw
    assert raw is not None
    assert raw["screen"] == "key sk_live_SECRET active", "stored snapshot was mutated!"
    assert raw["raw_tail"] == "sk_live_SECRET"


@pytest.mark.asyncio
async def test_get_last_snapshot_no_gate_returns_raw() -> None:
    """With no gate, get_last_snapshot(recipient=...) returns the raw snapshot."""
    hub = TermHub()  # no gate
    worker_id = "w-read-nogate"

    await hub.register_worker(worker_id, AsyncMock())
    await hub.update_last_snapshot(worker_id, _secret_snapshot())

    recipient = _PrincipalWS()
    snap = await hub.get_last_snapshot(worker_id, recipient=recipient)
    assert snap is not None
    assert snap["screen"] == "key sk_live_SECRET active", "no-gate read must not redact"


@pytest.mark.asyncio
async def test_get_last_snapshot_no_recipient_returns_raw_even_with_gate() -> None:
    """Without a recipient there is no role to scope to → raw snapshot (broadcast source)."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-read-norec"

    await hub.register_worker(worker_id, AsyncMock())
    await hub.update_last_snapshot(worker_id, _secret_snapshot())

    snap = await hub.get_last_snapshot(worker_id)
    assert snap is not None
    assert snap["screen"] == "key sk_live_SECRET active"


@pytest.mark.asyncio
async def test_get_last_snapshot_missing_worker_returns_none() -> None:
    """An unknown worker returns None regardless of recipient/gate."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    assert await hub.get_last_snapshot("ghost", recipient=_PrincipalWS()) is None


@pytest.mark.asyncio
async def test_get_last_snapshot_none_snapshot_with_recipient() -> None:
    """A registered worker with no snapshot yet returns None (no redaction crash)."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-read-empty"
    await hub.register_worker(worker_id, AsyncMock())
    assert await hub.get_last_snapshot(worker_id, recipient=_PrincipalWS()) is None


@pytest.mark.asyncio
async def test_get_last_snapshot_empty_rules_returns_raw() -> None:
    """An active gate that returns no rules → raw snapshot (empty-rules fall-through)."""
    gate = MockOutputPolicy([])  # active gate, no rules
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-read-emptyrules"
    await hub.register_worker(worker_id, AsyncMock())
    await hub.update_last_snapshot(worker_id, _secret_snapshot())

    snap = await hub.get_last_snapshot(worker_id, recipient=_PrincipalWS())
    assert snap is not None
    assert snap["screen"] == "key sk_live_SECRET active", "empty rules must not redact"


# ---------------------------------------------------------------------------
# WS register_browser initial_snapshot redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_browser_initial_snapshot_redacted_under_policy() -> None:
    """register_browser returns a REDACTED initial_snapshot when a gate is active."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-reg-snap"

    await hub.register_worker(worker_id, AsyncMock())
    await hub.update_last_snapshot(worker_id, _secret_snapshot())

    browser_ws = _PrincipalWS()
    state = await hub.register_browser(worker_id, browser_ws, "viewer", defer_broadcast=True)
    snap = state["initial_snapshot"]
    assert snap is not None
    assert "sk_live_" not in snap["screen"], f"initial_snapshot screen not redacted: {snap['screen']!r}"
    assert "[REDACTED]" in snap["screen"]
    assert "sk_live_" not in str(snap["raw_tail"]), "initial_snapshot raw_tail not redacted"

    # Stored snapshot is NOT mutated.
    raw = await hub.get_last_snapshot(worker_id)
    assert raw is not None
    assert raw["screen"] == "key sk_live_SECRET active", "stored snapshot mutated by register_browser!"


@pytest.mark.asyncio
async def test_register_browser_initial_snapshot_no_gate_raw() -> None:
    """register_browser returns the raw initial_snapshot when no gate is configured."""
    hub = TermHub()  # no gate
    worker_id = "w-reg-snap-nogate"

    await hub.register_worker(worker_id, AsyncMock())
    await hub.update_last_snapshot(worker_id, _secret_snapshot())

    browser_ws = _PrincipalWS()
    state = await hub.register_browser(worker_id, browser_ws, "viewer", defer_broadcast=True)
    snap = state["initial_snapshot"]
    assert snap is not None
    assert snap["screen"] == "key sk_live_SECRET active", "no-gate register must not redact"


@pytest.mark.asyncio
async def test_register_browser_no_snapshot_initial_none() -> None:
    """When there is no stored snapshot, initial_snapshot is None (no redaction crash)."""
    gate = MockOutputPolicy(_STRIPE_RULES)
    hub = TermHub(output_policy_gate=gate)
    worker_id = "w-reg-nosnap"
    await hub.register_worker(worker_id, AsyncMock())

    browser_ws = _PrincipalWS()
    state = await hub.register_browser(worker_id, browser_ws, "viewer", defer_broadcast=True)
    assert state["initial_snapshot"] is None
