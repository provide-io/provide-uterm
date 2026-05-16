#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for InterceptGate — pause/resume state machine for HTTP interception."""

from __future__ import annotations

import asyncio
import base64

import pytest

from provide.uterm.tunnel.intercept import (
    InterceptDecision,
    InterceptGate,
    _default_decision,
    parse_action_message,
)


def _fwd() -> InterceptDecision:
    return InterceptDecision(action="forward", headers=None, body=None)


def _drop() -> InterceptDecision:
    return InterceptDecision(action="drop", headers=None, body=None)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


class TestInterceptGateBasics:
    def test_initial_enabled_false(self) -> None:
        assert InterceptGate().enabled is False

    def test_initial_inspect_enabled_true(self) -> None:
        assert InterceptGate().inspect_enabled is True

    def test_initial_pending_count_zero(self) -> None:
        assert InterceptGate().pending_count == 0

    def test_enable_toggle(self) -> None:
        g = InterceptGate()
        g.enabled = True
        assert g.enabled is True
        g.enabled = False
        assert g.enabled is False

    def test_inspect_toggle(self) -> None:
        g = InterceptGate()
        g.inspect_enabled = False
        assert g.inspect_enabled is False

    def test_default_timeout_s(self) -> None:
        assert InterceptGate().timeout_s == 30.0

    def test_custom_timeout_s(self) -> None:
        assert InterceptGate(timeout_s=10.0).timeout_s == 10.0

    def test_timeout_s_clamps_minimum(self) -> None:
        assert InterceptGate(timeout_s=0.01).timeout_s == 1.0
        assert InterceptGate(timeout_s=-5.0).timeout_s == 1.0

    def test_timeout_action_drop(self) -> None:
        assert InterceptGate(timeout_action="drop").timeout_action == "drop"

    def test_invalid_timeout_action_defaults_forward(self) -> None:
        assert InterceptGate(timeout_action="ignore").timeout_action == "forward"


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


class TestInterceptGateResolve:
    async def test_resolve_returns_decision(self) -> None:
        g = InterceptGate(timeout_s=5.0)

        async def _r() -> None:
            await asyncio.sleep(0)
            g.resolve("r1", _drop())

        t = asyncio.create_task(_r())
        result = await g.await_decision("r1")
        await t
        assert result["action"] == "drop"
        assert g.pending_count == 0

    async def test_resolve_unknown_returns_false(self) -> None:
        assert InterceptGate().resolve("nope", _fwd()) is False

    async def test_resolve_done_returns_false(self) -> None:
        g = InterceptGate(timeout_s=5.0)

        async def _r() -> None:
            await asyncio.sleep(0)
            assert g.resolve("r1", _fwd()) is True
            assert g.resolve("r1", _drop()) is False  # already done

        t = asyncio.create_task(_r())
        await g.await_decision("r1")
        await t

    async def test_resolve_modify_with_headers_body(self) -> None:
        g = InterceptGate(timeout_s=5.0)
        d = InterceptDecision(action="modify", headers={"X": "1"}, body=b"new")

        async def _r() -> None:
            await asyncio.sleep(0)
            g.resolve("r1", d)

        t = asyncio.create_task(_r())
        result = await g.await_decision("r1")
        await t
        assert result["headers"] == {"X": "1"}
        assert result["body"] == b"new"

    async def test_pending_count_during_await(self) -> None:
        g = InterceptGate(timeout_s=5.0)
        started = asyncio.Event()

        async def _w() -> None:
            started.set()
            await g.await_decision("r1")

        t = asyncio.create_task(_w())
        await started.wait()
        assert g.pending_count == 1
        g.resolve("r1", _fwd())
        await t
        assert g.pending_count == 0


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestInterceptGateTimeout:
    async def test_timeout_forward(self) -> None:
        g = InterceptGate(timeout_s=1.0, timeout_action="forward")
        assert (await g.await_decision("t1"))["action"] == "forward"

    async def test_timeout_drop(self) -> None:
        g = InterceptGate(timeout_s=1.0, timeout_action="drop")
        assert (await g.await_decision("t1"))["action"] == "drop"

    async def test_timeout_clears_pending(self) -> None:
        g = InterceptGate(timeout_s=1.0)
        await g.await_decision("t1")
        assert g.pending_count == 0


# ---------------------------------------------------------------------------
# CancelAll
# ---------------------------------------------------------------------------


class TestInterceptGateCancelAll:
    async def test_cancel_all_resolves_all(self) -> None:
        g = InterceptGate(timeout_s=30.0)
        results: list[InterceptDecision] = []

        async def _w(rid: str) -> None:
            results.append(await g.await_decision(rid))

        tasks = [asyncio.create_task(_w(f"c{i}")) for i in range(3)]
        await asyncio.sleep(0)
        assert g.cancel_all("drop") == 3
        await asyncio.gather(*tasks)
        assert all(r["action"] == "drop" for r in results)
        assert g.pending_count == 0

    async def test_cancel_all_empty_returns_zero(self) -> None:
        assert InterceptGate().cancel_all() == 0

    async def test_cancel_all_default_forward(self) -> None:
        g = InterceptGate(timeout_s=30.0)
        results: list[InterceptDecision] = []

        async def _w() -> None:
            results.append(await g.await_decision("x"))

        t = asyncio.create_task(_w())
        await asyncio.sleep(0)
        g.cancel_all()
        await t
        assert results[0]["action"] == "forward"

    async def test_cancel_all_skips_done_futures(self) -> None:
        g = InterceptGate(timeout_s=30.0)
        results: list[InterceptDecision] = []

        async def _w(rid: str) -> None:
            results.append(await g.await_decision(rid))

        tasks = [asyncio.create_task(_w(f"m{i}")) for i in range(5)]
        await asyncio.sleep(0)
        g.resolve("m0", _fwd())
        g.resolve("m1", _drop())
        await asyncio.sleep(0)
        assert g.cancel_all("drop") == 3
        await asyncio.gather(*tasks)
        assert len(results) == 5


# ---------------------------------------------------------------------------
# ParseActionMessage
# ---------------------------------------------------------------------------


class TestParseActionMessage:
    def test_forward(self) -> None:
        r = parse_action_message({"action": "forward"})
        assert r == {"action": "forward", "headers": None, "body": None}

    def test_drop(self) -> None:
        assert parse_action_message({"action": "drop"})["action"] == "drop"

    def test_missing_action_defaults_forward(self) -> None:
        assert parse_action_message({})["action"] == "forward"

    def test_unknown_action_defaults_forward(self) -> None:
        assert parse_action_message({"action": "explode"})["action"] == "forward"

    def test_modify_headers_and_body(self) -> None:
        b = base64.b64encode(b"hello").decode()
        r = parse_action_message({"action": "modify", "headers": {"K": "V"}, "body_b64": b})
        assert r["headers"] == {"K": "V"}
        assert r["body"] == b"hello"

    def test_modify_invalid_b64(self) -> None:
        # Use a string that actually fails base64 decoding
        assert parse_action_message({"action": "modify", "body_b64": "\x00\x01\x02"})["body"] is None

    def test_modify_non_dict_headers(self) -> None:
        assert parse_action_message({"action": "modify", "headers": "bad"})["headers"] is None

    def test_modify_no_extras(self) -> None:
        r = parse_action_message({"action": "modify"})
        assert r["headers"] is None and r["body"] is None

    def test_modify_coerces_header_values(self) -> None:
        r = parse_action_message({"action": "modify", "headers": {"N": 42}})
        assert r["headers"] == {"N": "42"}

    def test_non_str_body_b64_ignored(self) -> None:
        assert parse_action_message({"action": "modify", "body_b64": 999})["body"] is None


# ---------------------------------------------------------------------------
# DefaultDecision
# ---------------------------------------------------------------------------


class TestDefaultDecision:
    def test_forward(self) -> None:
        assert _default_decision("forward") == {"action": "forward", "headers": None, "body": None}

    def test_drop(self) -> None:
        assert _default_decision("drop")["action"] == "drop"


# ---------------------------------------------------------------------------
# Concurrent + Stress
# ---------------------------------------------------------------------------


class TestConcurrentRequests:
    async def test_ten_concurrent(self) -> None:
        g = InterceptGate(timeout_s=5.0)
        results: dict[str, str] = {}

        async def _w(i: int) -> None:
            results[f"r{i}"] = (await g.await_decision(f"r{i}"))["action"]

        tasks = [asyncio.create_task(_w(i)) for i in range(10)]
        await asyncio.sleep(0)
        for i in range(10):
            g.resolve(f"r{i}", _fwd() if i % 2 == 0 else _drop())
        await asyncio.gather(*tasks)
        assert results["r0"] == "forward"
        assert results["r1"] == "drop"

    async def test_resolve_unknown_no_side_effects(self) -> None:
        g = InterceptGate(timeout_s=5.0)

        async def _w() -> InterceptDecision:
            return await g.await_decision("real")

        t = asyncio.create_task(_w())
        await asyncio.sleep(0)
        assert g.resolve("phantom", _fwd()) is False
        g.resolve("real", _fwd())
        assert (await t)["action"] == "forward"


class TestInterceptGateStress:
    @pytest.mark.timeout(10)
    async def test_fifty_concurrent(self) -> None:
        g = InterceptGate(timeout_s=10.0)
        results: list[str] = []

        async def _w(rid: str) -> None:
            results.append((await g.await_decision(rid))["action"])

        tasks = [asyncio.create_task(_w(f"s{i}")) for i in range(50)]
        await asyncio.sleep(0)
        assert g.pending_count == 50
        for i in range(50):
            g.resolve(f"s{i}", _fwd())
        await asyncio.gather(*tasks)
        assert len(results) == 50
        assert g.pending_count == 0

    @pytest.mark.timeout(10)
    async def test_rapid_toggle_cancel(self) -> None:
        g = InterceptGate(timeout_s=30.0)
        g.enabled = True
        results: list[str] = []

        async def _w(rid: str) -> None:
            results.append((await g.await_decision(rid))["action"])

        tasks = [asyncio.create_task(_w(f"t{i}")) for i in range(10)]
        await asyncio.sleep(0)
        g.enabled = False
        g.cancel_all("forward")
        await asyncio.gather(*tasks)
        assert all(r == "forward" for r in results)
