#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for :mod:`provide.uterm.tunnel.intercept`.

``test_intercept_gate.py`` exercises the decision flow but never asserts the
*content* of the two ``logger.warning`` calls (the denylisted-header drop and
the invalid-body-b64 notice), nor the ``not fut.done()`` guard / ``timeout_s``
wiring — leaving those mutants unbound. ``intercept`` logs through the stdlib
``logging`` module (not structlog), so an exact ``getMessage()`` assertion
cleanly pins the format string + every arg, and any ``"XX…XX"`` wrap or
dropped/None arg makes the message differ (or ``getMessage()`` raise).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from provide.uterm.tunnel.intercept import InterceptGate, parse_action_message

_LOGGER = "provide.uterm.tunnel.intercept"


# == log-message content =====================================================


def test_invalid_body_b64_warns_with_request_id(caplog: pytest.LogCaptureFixture) -> None:
    """A modify decision with un-decodable body_b64 logs the exact warning + request id."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        decision = parse_action_message({"action": "modify", "body_b64": "!!not-base64!!", "id": "req-42"})
    assert decision["body"] is None  # validate=True ⇒ rejected (kills validate=True→False)
    assert any(r.getMessage() == "intercept_invalid_body_b64 id=req-42" for r in caplog.records)


def test_denylisted_headers_warn_with_sorted_names(caplog: pytest.LogCaptureFixture) -> None:
    """A modify decision carrying a denylisted header drops it and logs the exact name."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        decision = parse_action_message({"action": "modify", "headers": {"Authorization": "Bearer x"}})
    assert decision["headers"] == {}  # the denylisted header was stripped
    assert any(r.getMessage() == "intercept_headers_denylisted dropped=['Authorization']" for r in caplog.records)


# == cancel_all: the not-done guard ==========================================


async def test_cancel_all_resolves_pending_and_counts() -> None:
    """cancel_all resolves every *pending* future and returns the count.

    Pins ``if not fut.done()``: the inverted guard would skip pending futures
    (count 0, nothing resolved) and try to re-resolve done ones.
    """
    gate = InterceptGate()
    loop = asyncio.get_running_loop()
    f1: asyncio.Future = loop.create_future()
    f2: asyncio.Future = loop.create_future()
    gate._pending["a"] = f1
    gate._pending["b"] = f2

    count = gate.cancel_all("drop")
    assert count == 2
    assert f1.done() and f2.done()
    assert f1.result()["action"] == "drop"  # the passed action reaches the decision
    assert gate.pending_count == 0  # _pending cleared


# == await_decision: timeout wiring ==========================================


async def test_await_decision_times_out_to_default_decision() -> None:
    """An unresolved request returns the timeout decision after ``timeout_s``.

    Pins ``timeout=self.timeout_s`` in the inner ``wait_for``: a ``None`` timeout
    would wait forever, so the outer 2s guard would fire instead of the gate's
    own 0.05s deadline.
    """
    gate = InterceptGate(timeout_action="drop")
    gate.timeout_s = 0.05  # tiny, so the inner wait_for resolves quickly
    decision = await asyncio.wait_for(gate.await_decision("r1"), timeout=2.0)
    assert decision["action"] == "drop"  # _default_decision(self.timeout_action)
    assert gate.pending_count == 0  # finally-pop ran
