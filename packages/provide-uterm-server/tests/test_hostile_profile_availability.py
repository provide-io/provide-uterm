#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for the hostile probe's authenticated availability lane.

``scripts/hostile_profile.py`` is a CI script (outside any package's coverage
perimeter), so it is loaded here via importlib and its pure logic is exercised
with mocks — no live server. Coverage targets:
  - ``_read_dev_token``: env/explicit path resolution + absent/empty handling.
  - ``_authenticated_session_once``: COMPLETED on a hello frame, REFUSED on a
    403 handshake rejection, HUNG when the latency budget is exceeded, ERROR
    when the stream closes without a hello.
  - ``_run_availability`` verdict: PASS only when the server stays healthy,
    refuses every hostile attempt, AND serves every authenticated client;
    FAIL on auth-bypass / starvation / health loss; exit 2 with no token.

Each test fails if the corresponding behaviour regresses (they assert the
outcome/exit code, not merely the absence of an exception).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import InvalidStatus

from provide.uterm.control_channel import encode_control

# Load the standalone CI script as a module (it lives outside the package tree).
_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "hostile_profile.py"
_spec = importlib.util.spec_from_file_location("hostile_profile", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConnect:
    """Stand-in for ``websockets.connect(...)`` as an async CM + async iterator."""

    def __init__(self, *, frames: list[str] | None = None, exc: BaseException | None = None) -> None:
        self._frames = frames or []
        self._exc = exc

    async def __aenter__(self) -> _FakeConnect:
        if self._exc is not None:
            raise self._exc
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for frame in self._frames:
            yield frame


def _hello_wire() -> str:
    return encode_control({"type": "hello", "role": "admin", "worker_online": True})


def _ns(**overrides: object) -> argparse.Namespace:
    base = {
        "base_url": "http://127.0.0.1:8780",
        "worker_id": "provide-shell",
        "mode": "availability",
        "iterations": 3,
        "concurrency": 5,
        "timeout_s": 5.0,
        "auth_sessions": 2,
        "latency_budget_s": 5.0,
        "token_path": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# _read_dev_token
# ---------------------------------------------------------------------------


class TestReadDevToken:
    def test_explicit_path_returns_stripped_token(self, tmp_path: Path) -> None:
        token_file = tmp_path / "tok"
        token_file.write_text("  jwt-value\n")
        assert hp._read_dev_token(str(token_file)) == "jwt-value"

    def test_env_path_used_when_no_explicit_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        token_file = tmp_path / "envtok"
        token_file.write_text("env-jwt")
        monkeypatch.setenv("UTERM_DEV_TOKEN_PATH", str(token_file))
        assert hp._read_dev_token() == "env-jwt"

    def test_explicit_path_overrides_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "env").write_text("env-jwt")
        explicit = tmp_path / "explicit"
        explicit.write_text("explicit-jwt")
        monkeypatch.setenv("UTERM_DEV_TOKEN_PATH", str(tmp_path / "env"))
        assert hp._read_dev_token(str(explicit)) == "explicit-jwt"

    def test_no_path_configured_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UTERM_DEV_TOKEN_PATH", raising=False)
        assert hp._read_dev_token() is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert hp._read_dev_token(str(tmp_path / "does-not-exist")) is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.write_text("   \n")
        assert hp._read_dev_token(str(empty)) is None


# ---------------------------------------------------------------------------
# _authenticated_session_once
# ---------------------------------------------------------------------------


class TestAuthenticatedSessionOnce:
    async def test_hello_frame_yields_completed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hp.websockets, "connect", lambda *a, **k: _FakeConnect(frames=[_hello_wire()]))
        outcome, latency = await hp._authenticated_session_once("ws://x/term", "tok", budget_s=5.0, timeout_s=5.0)
        assert outcome == hp.COMPLETED
        assert latency >= 0.0

    async def test_403_handshake_rejection_yields_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exc = InvalidStatus(types.SimpleNamespace(status_code=403))
        monkeypatch.setattr(hp.websockets, "connect", lambda *a, **k: _FakeConnect(exc=exc))
        outcome, _latency = await hp._authenticated_session_once("ws://x/term", "tok", budget_s=5.0, timeout_s=5.0)
        assert outcome == hp.REFUSED

    async def test_budget_exceeded_yields_hung(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _slow(*_a: object, **_k: object) -> str:
            await asyncio.sleep(1.0)
            return hp.COMPLETED

        monkeypatch.setattr(hp, "_await_hello", _slow)
        outcome, _latency = await hp._authenticated_session_once("ws://x/term", "tok", budget_s=0.01, timeout_s=5.0)
        assert outcome == hp.HUNG

    async def test_stream_closes_without_hello_yields_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        other = encode_control({"type": "hijack_state"})
        monkeypatch.setattr(hp.websockets, "connect", lambda *a, **k: _FakeConnect(frames=[other]))
        outcome, _latency = await hp._authenticated_session_once("ws://x/term", "tok", budget_s=5.0, timeout_s=5.0)
        assert outcome == hp.ERROR


# ---------------------------------------------------------------------------
# _run_availability verdict (via run() to also cover dispatch + pre-health)
# ---------------------------------------------------------------------------


class TestRunAvailabilityVerdict:
    def _patch_common(self, monkeypatch: pytest.MonkeyPatch, *, health: object = True) -> None:
        monkeypatch.setattr(hp, "_read_dev_token", lambda *a, **k: "tok")
        if isinstance(health, list):
            monkeypatch.setattr(hp, "_health", AsyncMock(side_effect=health))
        else:
            monkeypatch.setattr(hp, "_health", AsyncMock(return_value=health))

    async def test_pass_when_refused_and_all_authed_complete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_common(monkeypatch)
        monkeypatch.setattr(hp, "_burst_ws_once", AsyncMock(return_value=hp.REFUSED))
        monkeypatch.setattr(hp, "_authenticated_session_once", AsyncMock(return_value=(hp.COMPLETED, 0.1)))
        assert await hp.run(_ns()) == 0

    async def test_no_token_returns_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hp, "_health", AsyncMock(return_value=True))
        monkeypatch.setattr(hp, "_read_dev_token", lambda *a, **k: None)
        assert await hp.run(_ns()) == 2

    async def test_auth_bypass_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_common(monkeypatch)
        # One hostile attempt COMPLETES a handshake -> auth bypass -> FAIL.
        monkeypatch.setattr(hp, "_burst_ws_once", AsyncMock(side_effect=[hp.REFUSED, hp.COMPLETED, hp.REFUSED]))
        monkeypatch.setattr(hp, "_authenticated_session_once", AsyncMock(return_value=(hp.COMPLETED, 0.1)))
        assert await hp.run(_ns()) == 1

    async def test_legit_client_starvation_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_common(monkeypatch)
        monkeypatch.setattr(hp, "_burst_ws_once", AsyncMock(return_value=hp.REFUSED))
        # One legitimate client hangs past its budget -> starvation -> FAIL.
        monkeypatch.setattr(
            hp, "_authenticated_session_once", AsyncMock(side_effect=[(hp.COMPLETED, 0.1), (hp.HUNG, 5.0)])
        )
        assert await hp.run(_ns()) == 1

    async def test_health_loss_after_flood_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # before=True (pre-probe), after=False (server unhealthy after the flood).
        self._patch_common(monkeypatch, health=[True, False])
        monkeypatch.setattr(hp, "_burst_ws_once", AsyncMock(return_value=hp.REFUSED))
        monkeypatch.setattr(hp, "_authenticated_session_once", AsyncMock(return_value=(hp.COMPLETED, 0.1)))
        assert await hp.run(_ns()) == 1

    async def test_hostile_error_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_common(monkeypatch)
        monkeypatch.setattr(hp, "_burst_ws_once", AsyncMock(side_effect=[hp.REFUSED, hp.ERROR, hp.REFUSED]))
        monkeypatch.setattr(hp, "_authenticated_session_once", AsyncMock(return_value=(hp.COMPLETED, 0.1)))
        assert await hp.run(_ns()) == 1

    async def test_hostile_hung_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_common(monkeypatch)
        # A hostile attempt that HANGS (no timely refusal) is a liveness/DoS
        # signal distinct from a completed bypass — it must also fail the probe.
        monkeypatch.setattr(hp, "_burst_ws_once", AsyncMock(side_effect=[hp.REFUSED, hp.HUNG, hp.REFUSED]))
        monkeypatch.setattr(hp, "_authenticated_session_once", AsyncMock(return_value=(hp.COMPLETED, 0.1)))
        assert await hp.run(_ns()) == 1
