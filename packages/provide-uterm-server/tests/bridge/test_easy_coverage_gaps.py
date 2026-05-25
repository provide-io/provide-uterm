#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted unit tests covering single-line gaps surfaced by the coverage
report (the "easy wins" set).

Each test docstring names the file:line it covers so future report-vs-test
correlation is obvious.
"""

from __future__ import annotations

import logging

import pytest


# ---------------------------------------------------------------------------
# bridge/hub/core.py:180 — set_worker_hello_mode rejects invalid input mode
# ---------------------------------------------------------------------------


async def test_set_worker_hello_mode_rejects_invalid_mode() -> None:
    from provide.uterm.bridge.hub import TermHub

    hub = TermHub()
    with pytest.raises(ValueError, match="invalid input mode"):
        await hub.set_worker_hello_mode("nobody", "bogus-mode")


# ---------------------------------------------------------------------------
# bridge/hub/connections.py:205 — legacy protocol_version warning log
# ---------------------------------------------------------------------------


async def test_worker_hello_logs_warning_for_legacy_protocol(caplog: pytest.LogCaptureFixture) -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub import TermHub
    from provide.uterm.bridge.models import WorkerTermState

    hub = TermHub()
    hub._workers["w1"] = WorkerTermState(worker_ws=AsyncMock())
    caplog.set_level(logging.WARNING, logger="provide.uterm.bridge.hub.connections")
    await hub.set_worker_hello("w1", "open", protocol_version=0)
    assert any("worker_hello_legacy_protocol" in r.getMessage() for r in caplog.records), (
        "set_worker_hello with protocol_version<1 must log worker_hello_legacy_protocol warning"
    )


# ---------------------------------------------------------------------------
# bridge/hub/semantics.py:16 — CommandSplitter.split("") -> []
# ---------------------------------------------------------------------------


def test_command_splitter_empty_returns_empty_list() -> None:
    from provide.uterm.bridge.hub.semantics import CommandSplitter

    assert CommandSplitter().split("") == []


# ---------------------------------------------------------------------------
# server/connectors/__init__.py:38 — __getattr__ raises AttributeError for unknown
# ---------------------------------------------------------------------------


def test_connectors_module_getattr_unknown_raises() -> None:
    from provide.uterm.server import connectors

    with pytest.raises(AttributeError, match="has no attribute"):
        _ = connectors.NoSuchSymbol  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# server/discovery.py:33 — NoOpDiscoveryProvider.announce is a no-op
# ---------------------------------------------------------------------------


async def test_noop_discovery_provider_announce_is_noop() -> None:
    from provide.uterm.server.discovery import NoOpDiscoveryProvider, NodeStatus

    provider = NoOpDiscoveryProvider()
    status = NodeStatus(node_id="n1", active_sessions=0, worker_count=0, timestamp=0.0)
    # Must accept the argument and return None without side effects or raise.
    assert await provider.announce(status) is None


# ---------------------------------------------------------------------------
# server/app/auth.py:114 — placeholder jwt_public_key_pem raises in jwt mode
# ---------------------------------------------------------------------------


def test_validate_auth_config_rejects_placeholder_jwt_public_key() -> None:
    from provide.uterm.server.app.auth import _validate_auth_config
    from provide.uterm.server.models import AuthConfig, ServerBindConfig, ServerConfig

    # Placeholder check only runs in "production-like" mode (non-loopback
    # host or require_jwt_in_production=True); 127.0.0.1 would short-circuit.
    config = ServerConfig(
        server=ServerBindConfig(host="0.0.0.0"),  # noqa: S104 — non-loopback to trip prod-mode validation
        auth=AuthConfig(
            mode="jwt",
            jwt_public_key_pem="changeme",  # known placeholder marker
            jwt_algorithms=["HS256"],
            worker_bearer_token="real-bearer-token-32-chars-long-x",
        ),
    )
    with pytest.raises(ValueError, match="placeholder value"):
        _validate_auth_config(config)


# ---------------------------------------------------------------------------
# server/runtime.py:206 — get_recording_path delegates to the recording store
# ---------------------------------------------------------------------------


async def test_hosted_runtime_get_recording_path_returns_store_result(tmp_path) -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.server.models import SessionDefinition
    from provide.uterm.server.runtime import HostedSessionRuntime

    session = SessionDefinition(session_id="s1", display_name="s1", connector_type="shell")
    runtime = HostedSessionRuntime.__new__(HostedSessionRuntime)
    runtime.definition = session
    fake_path = tmp_path / "s1.jsonl"
    runtime._recording_store = AsyncMock()  # type: ignore[attr-defined]
    runtime._recording_store.get_path = AsyncMock(return_value=fake_path)
    result = await runtime.get_recording_path()
    assert result == fake_path
    runtime._recording_store.get_path.assert_awaited_once_with("s1")


# ---------------------------------------------------------------------------
# bridge/hub/ownership.py:57-60 — peek_expiry returns (browser, rest) tuple
# ---------------------------------------------------------------------------


def test_ownership_compute_lease_expirations_reports_both_expired() -> None:
    import time as _time
    from unittest.mock import AsyncMock

    from provide.uterm.bridge.hub.ownership import _OwnershipMixin
    from provide.uterm.bridge.models import HijackSession, WorkerTermState

    now = _time.monotonic()
    state = WorkerTermState(worker_ws=AsyncMock())
    state.hijack_session = HijackSession(
        hijack_id="h1",
        owner="o",
        acquired_at=now - 100,
        lease_expires_at=now - 1,  # rest expired
        last_heartbeat=now,
    )
    state.hijack_owner = AsyncMock()
    state.hijack_owner_expires_at = now - 1  # browser expired
    browser_expired, rest_expired = _OwnershipMixin._compute_lease_expirations(state, now)
    assert browser_expired is True
    assert rest_expired is True
