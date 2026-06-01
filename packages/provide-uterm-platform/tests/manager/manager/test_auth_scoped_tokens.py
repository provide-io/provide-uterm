#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the two-static-token scoping in manager/auth.py (item 5b).

A low-privilege ``worker_token`` authorizes ONLY the worker-self-report
routes (``POST /agent/{id}/status`` and ``POST /agent/{id}/register``); the
operator token authorizes everything. The middleware classifies the request
by path + method.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.manager.auth import TokenAuthMiddleware, setup_auth

OPERATOR = "operator-secret"
WORKER = "worker-secret"

# Routes that the low-priv worker token is allowed to reach.
_SELF_REPORT = (
    ("POST", "/agent/agent_000/status"),
    ("POST", "/agent/agent_000/register"),
)

# A representative spread of operator-only routes (spawn / kill / delete /
# kill-all / prune / restart / GET reads). The worker token must be REJECTED
# on every one of these.
_OPERATOR_ONLY = (
    ("DELETE", "/agent/agent_000"),
    ("POST", "/agent/agent_000/restart"),
    ("POST", "/agent/agent_000/pause"),
    ("POST", "/agent/agent_000/set-goal"),
    ("POST", "/swarm/spawn"),
    ("POST", "/swarm/kill-all"),
    ("POST", "/swarm/prune"),
    ("GET", "/agents"),
    ("GET", "/agent/agent_000/status"),
    ("GET", "/agent/agent_000/details"),
)


def _http_scope(method: str, path: str, token: str) -> dict:
    return {
        "type": "http",
        "path": path,
        "method": method,
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }


async def _call(mw: TokenAuthMiddleware, scope: dict) -> tuple[AsyncMock, list]:
    inner = mw._app  # type: ignore[attr-defined]
    sent: list = []

    async def fake_send(msg):
        sent.append(msg)

    await mw(scope, AsyncMock(), fake_send)
    return inner, sent


def _make_mw(worker_token: str | None = WORKER) -> TokenAuthMiddleware:
    return TokenAuthMiddleware(AsyncMock(), OPERATOR, worker_token=worker_token)


# ---------------------------------------------------------------------------
# _is_worker_self_report_route classification
# ---------------------------------------------------------------------------


class TestSelfReportClassification:
    @pytest.mark.parametrize(
        "method,path,expected",
        [
            # Self-report routes (POST status + POST register).
            ("POST", "/agent/agent_000/status", True),
            ("POST", "/agent/x/status", True),
            ("POST", "/agent/a-b_c/status", True),
            ("POST", "/agent/agent_000/register", True),
            # Wrong method on the self-report path → not self-report.
            ("GET", "/agent/agent_000/status", False),
            ("DELETE", "/agent/agent_000/status", False),
            ("PUT", "/agent/agent_000/register", False),
            # Near-miss / trailing-content paths must NOT match.
            ("POST", "/agent/agent_000/statusfoo", False),
            ("POST", "/agent/agent_000/status/", False),
            ("POST", "/agent/agent_000/details", False),
            ("POST", "/agent/agent_000/registerx", False),
            ("POST", "/agent/agent_000", False),
            ("POST", "/agent//status", False),  # empty id is not [^/]+
            ("POST", "/agent/a/b/status", False),  # id can't contain a slash
            ("POST", "/prefix/agent/x/status", False),  # not anchored at start
            ("POST", "/swarm/spawn", False),
        ],
    )
    def test_classification(self, method: str, path: str, expected: bool) -> None:
        mw = _make_mw()
        assert mw._is_worker_self_report_route(path, method) is expected


# ---------------------------------------------------------------------------
# Worker token: accepted on self-report, rejected elsewhere
# ---------------------------------------------------------------------------


class TestWorkerTokenScope:
    @pytest.mark.parametrize("method,path", _SELF_REPORT)
    async def test_worker_token_accepted_on_self_report(self, method: str, path: str) -> None:
        mw = _make_mw(worker_token=WORKER)
        inner, _ = await _call(mw, _http_scope(method, path, WORKER))
        inner.assert_awaited_once()

    @pytest.mark.parametrize("method,path", _OPERATOR_ONLY)
    async def test_worker_token_rejected_on_operator_routes(self, method: str, path: str) -> None:
        mw = _make_mw(worker_token=WORKER)
        inner, sent = await _call(mw, _http_scope(method, path, WORKER))
        inner.assert_not_awaited()
        status_msg = next((m for m in sent if m.get("type") == "http.response.start"), None)
        assert status_msg is not None
        assert status_msg["status"] == 401


# ---------------------------------------------------------------------------
# Operator token: authorizes EVERYTHING
# ---------------------------------------------------------------------------


class TestOperatorTokenScope:
    @pytest.mark.parametrize("method,path", (*_SELF_REPORT, *_OPERATOR_ONLY))
    async def test_operator_token_accepted_everywhere(self, method: str, path: str) -> None:
        mw = _make_mw(worker_token=WORKER)
        inner, _ = await _call(mw, _http_scope(method, path, OPERATOR))
        inner.assert_awaited_once()


# ---------------------------------------------------------------------------
# Backward compat: worker_token unset
# ---------------------------------------------------------------------------


class TestWorkerTokenUnset:
    @pytest.mark.parametrize("method,path", _SELF_REPORT)
    async def test_operator_token_still_works_on_self_report(self, method: str, path: str) -> None:
        mw = _make_mw(worker_token=None)
        inner, _ = await _call(mw, _http_scope(method, path, OPERATOR))
        inner.assert_awaited_once()

    @pytest.mark.parametrize("method,path", _SELF_REPORT)
    async def test_worker_value_rejected_when_unset(self, method: str, path: str) -> None:
        """With no worker token configured, only the operator token works."""
        mw = _make_mw(worker_token=None)
        inner, sent = await _call(mw, _http_scope(method, path, WORKER))
        inner.assert_not_awaited()
        status_msg = next((m for m in sent if m.get("type") == "http.response.start"), None)
        assert status_msg is not None
        assert status_msg["status"] == 401

    @pytest.mark.parametrize("method,path", _OPERATOR_ONLY)
    async def test_operator_token_works_on_operator_routes(self, method: str, path: str) -> None:
        mw = _make_mw(worker_token=None)
        inner, _ = await _call(mw, _http_scope(method, path, OPERATOR))
        inner.assert_awaited_once()


# ---------------------------------------------------------------------------
# Bad / empty token → 401 on all routes
# ---------------------------------------------------------------------------


class TestBadTokens:
    @pytest.mark.parametrize("method,path", (*_SELF_REPORT, *_OPERATOR_ONLY))
    @pytest.mark.parametrize("bad", ["", "totally-wrong"])
    async def test_bad_token_rejected_everywhere(self, method: str, path: str, bad: str) -> None:
        mw = _make_mw(worker_token=WORKER)
        inner, sent = await _call(mw, _http_scope(method, path, bad))
        inner.assert_not_awaited()
        status_msg = next((m for m in sent if m.get("type") == "http.response.start"), None)
        assert status_msg is not None
        assert status_msg["status"] == 401


# ---------------------------------------------------------------------------
# OPTIONS + public-path bypass unchanged with worker token configured
# ---------------------------------------------------------------------------


class TestBypassUnchanged:
    async def test_options_self_report_passes_through(self) -> None:
        mw = _make_mw(worker_token=WORKER)
        scope = {"type": "http", "path": "/agent/x/status", "method": "OPTIONS", "headers": []}
        inner, _ = await _call(mw, scope)
        inner.assert_awaited_once()

    async def test_public_path_bypass(self) -> None:
        mw = TokenAuthMiddleware(AsyncMock(), OPERATOR, worker_token=WORKER, public_paths=frozenset({"/dashboard"}))
        scope = {"type": "http", "path": "/dashboard", "method": "GET", "headers": []}
        inner, _ = await _call(mw, scope)
        inner.assert_awaited_once()


# ---------------------------------------------------------------------------
# setup_auth wiring of the worker token env var
# ---------------------------------------------------------------------------


class TestSetupAuthWorkerToken:
    def test_worker_token_env_var_wires_into_middleware(self) -> None:
        app = MagicMock()
        with patch.dict(
            os.environ,
            {"UTERM_MANAGER_API_TOKEN": "op-tok", "UTERM_MANAGER_WORKER_TOKEN": "wk-tok"},
        ):
            setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN")
        call = app.add_middleware.call_args
        assert call.kwargs["token"] == "op-tok"
        assert call.kwargs["worker_token"] == "wk-tok"

    def test_worker_token_unset_passes_none(self) -> None:
        app = MagicMock()
        with patch.dict(os.environ, {"UTERM_MANAGER_API_TOKEN": "op-tok"}, clear=False):
            os.environ.pop("UTERM_MANAGER_WORKER_TOKEN", None)
            setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN")
        call = app.add_middleware.call_args
        assert call.kwargs["worker_token"] is None

    def test_worker_token_whitespace_treated_as_unset(self) -> None:
        app = MagicMock()
        with patch.dict(
            os.environ,
            {"UTERM_MANAGER_API_TOKEN": "op-tok", "UTERM_MANAGER_WORKER_TOKEN": "   "},
        ):
            setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN")
        call = app.add_middleware.call_args
        assert call.kwargs["worker_token"] is None

    def test_custom_worker_env_var_from_config(self) -> None:
        app = MagicMock()
        config = MagicMock()
        config.auth_public_paths = []
        config.auth_public_prefixes = []
        config.auth_worker_token_env_var = "MY_WORKER_TOK"
        config.host = "127.0.0.1"
        with patch.dict(
            os.environ,
            {"UTERM_MANAGER_API_TOKEN": "op-tok", "MY_WORKER_TOK": "wk-tok"},
        ):
            setup_auth(app, env_var="UTERM_MANAGER_API_TOKEN", config=config)
        call = app.add_middleware.call_args
        assert call.kwargs["worker_token"] == "wk-tok"
