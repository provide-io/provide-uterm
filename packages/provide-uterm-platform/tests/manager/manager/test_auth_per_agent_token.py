#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Per-agent worker-token binding for manager/auth.py (finding M7).

The fleet-shared ``worker_token`` is FLEET-WIDE: every worker holds the same
value, so worker A can self-report (or register) AS agent B — impersonation.

The fix derives a per-agent token from the configured worker secret:
``agent_token = "sha256=" + HMAC-SHA256(worker_secret, agent_id)``. A worker
holding ``HMAC(secret, A)`` cannot compute ``HMAC(secret, B)``, so on the
``/agent/{agent_id}/...`` self-report routes the middleware can verify the
presented token is bound to the agent_id in the path.

Two modes:

* ``enforce_per_agent_worker_token=False`` (default, backward compatible): the
  raw fleet-shared ``worker_token`` is STILL accepted on self-report routes,
  but the derived per-agent token is ALSO accepted and is path-bound (a token
  for A is rejected on B's route).
* ``enforce_per_agent_worker_token=True`` (hardened): the raw fleet token is
  REJECTED on self-report routes; only the correct per-agent derived token
  works, fully blocking cross-agent impersonation.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock

import pytest

from provide.uterm.manager.auth import (
    TokenAuthMiddleware,
    _extract_self_report_agent_id,
    derive_agent_token,
)

OPERATOR = "operator-secret"
SECRET = "worker-fleet-secret"  # pragma: allowlist secret


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


def _status(sent: list) -> int | None:
    msg = next((m for m in sent if m.get("type") == "http.response.start"), None)
    return msg["status"] if msg is not None else None


def _make_mw(*, enforce: bool = False, worker_token: str | None = SECRET) -> TokenAuthMiddleware:
    return TokenAuthMiddleware(
        AsyncMock(),
        OPERATOR,
        worker_token=worker_token,
        worker_secret=SECRET,
        enforce_per_agent_worker_token=enforce,
    )


# ---------------------------------------------------------------------------
# derive_agent_token: deterministic, per-agent, stable format
# ---------------------------------------------------------------------------


class TestDeriveAgentToken:
    def test_format_matches_documented_hmac(self) -> None:
        expected = "sha256=" + hmac.new(SECRET.encode(), b"agent_000", hashlib.sha256).hexdigest()
        assert derive_agent_token(SECRET, "agent_000") == expected

    def test_deterministic(self) -> None:
        assert derive_agent_token(SECRET, "agent_007") == derive_agent_token(SECRET, "agent_007")

    def test_differs_per_agent(self) -> None:
        assert derive_agent_token(SECRET, "agent_A") != derive_agent_token(SECRET, "agent_B")

    def test_differs_per_secret(self) -> None:
        assert derive_agent_token(SECRET, "agent_A") != derive_agent_token("other-secret", "agent_A")

    def test_has_sha256_prefix(self) -> None:
        assert derive_agent_token(SECRET, "agent_A").startswith("sha256=")


# ---------------------------------------------------------------------------
# _extract_self_report_agent_id: anchored extraction, path-bound
# ---------------------------------------------------------------------------


class TestExtractAgentId:
    @pytest.mark.parametrize(
        "method,path,expected",
        [
            ("POST", "/agent/agent_000/status", "agent_000"),
            ("POST", "/agent/x/status", "x"),
            ("POST", "/agent/a-b_c/register", "a-b_c"),
            ("POST", "/agent/agent_000/register", "agent_000"),
            # Wrong method → not a self-report route → no agent_id.
            ("GET", "/agent/agent_000/status", None),
            ("DELETE", "/agent/agent_000/status", None),
            # Near-miss paths → no agent_id.
            ("POST", "/agent/agent_000/statusfoo", None),
            ("POST", "/agent/agent_000/status/", None),
            ("POST", "/agent/agent_000", None),
            ("POST", "/agent//status", None),
            ("POST", "/agent/a/b/status", None),
            ("POST", "/prefix/agent/x/status", None),
            ("POST", "/swarm/spawn", None),
        ],
    )
    def test_extract(self, method: str, path: str, expected: str | None) -> None:
        assert _extract_self_report_agent_id(path, method) == expected


# ---------------------------------------------------------------------------
# ENFORCED mode: only the correct per-agent token works; raw fleet token
# is rejected; cross-agent impersonation blocked.
# ---------------------------------------------------------------------------


class TestEnforcedMode:
    async def test_derived_token_accepted_on_own_status(self) -> None:
        mw = _make_mw(enforce=True)
        tok = derive_agent_token(SECRET, "agent_A")
        inner, _ = await _call(mw, _http_scope("POST", "/agent/agent_A/status", tok))
        inner.assert_awaited_once()

    async def test_derived_token_accepted_on_own_register(self) -> None:
        mw = _make_mw(enforce=True)
        tok = derive_agent_token(SECRET, "agent_A")
        inner, _ = await _call(mw, _http_scope("POST", "/agent/agent_A/register", tok))
        inner.assert_awaited_once()

    async def test_cross_agent_impersonation_blocked(self) -> None:
        """Token derived for A is REJECTED on B's status route (the core fix)."""
        mw = _make_mw(enforce=True)
        tok_a = derive_agent_token(SECRET, "agent_A")
        inner, sent = await _call(mw, _http_scope("POST", "/agent/agent_B/status", tok_a))
        inner.assert_not_awaited()
        assert _status(sent) == 401

    async def test_raw_fleet_token_rejected_on_self_report(self) -> None:
        """In enforce mode the raw fleet secret is NOT a valid self-report token."""
        mw = _make_mw(enforce=True)
        inner, sent = await _call(mw, _http_scope("POST", "/agent/agent_A/status", SECRET))
        inner.assert_not_awaited()
        assert _status(sent) == 401

    async def test_operator_token_authorizes_self_report(self) -> None:
        mw = _make_mw(enforce=True)
        inner, _ = await _call(mw, _http_scope("POST", "/agent/agent_A/status", OPERATOR))
        inner.assert_awaited_once()

    async def test_operator_token_authorizes_operator_route(self) -> None:
        mw = _make_mw(enforce=True)
        inner, _ = await _call(mw, _http_scope("POST", "/swarm/spawn", OPERATOR))
        inner.assert_awaited_once()

    async def test_derived_token_rejected_on_operator_route(self) -> None:
        mw = _make_mw(enforce=True)
        tok = derive_agent_token(SECRET, "agent_A")
        inner, sent = await _call(mw, _http_scope("POST", "/swarm/spawn", tok))
        inner.assert_not_awaited()
        assert _status(sent) == 401


# ---------------------------------------------------------------------------
# NON-ENFORCED mode (default): raw fleet token still accepted (backward
# compat) AND the derived per-agent token works and is STILL path-bound.
# ---------------------------------------------------------------------------


class TestNonEnforcedMode:
    async def test_raw_fleet_token_accepted_on_self_report(self) -> None:
        """Backward compat: un-migrated workers presenting the raw fleet token still work."""
        mw = _make_mw(enforce=False)
        inner, _ = await _call(mw, _http_scope("POST", "/agent/agent_A/status", SECRET))
        inner.assert_awaited_once()

    async def test_derived_token_accepted_on_own_route(self) -> None:
        mw = _make_mw(enforce=False)
        tok = derive_agent_token(SECRET, "agent_A")
        inner, _ = await _call(mw, _http_scope("POST", "/agent/agent_A/status", tok))
        inner.assert_awaited_once()

    async def test_derived_token_still_path_bound(self) -> None:
        """Even un-enforced, a per-agent token for A is rejected on B's route."""
        mw = _make_mw(enforce=False)
        tok_a = derive_agent_token(SECRET, "agent_A")
        # The raw fleet token would be accepted, but tok_a is neither the fleet
        # token nor B's derived token, so it must be rejected.
        inner, sent = await _call(mw, _http_scope("POST", "/agent/agent_B/status", tok_a))
        inner.assert_not_awaited()
        assert _status(sent) == 401

    async def test_raw_fleet_token_rejected_on_operator_route(self) -> None:
        mw = _make_mw(enforce=False)
        inner, sent = await _call(mw, _http_scope("POST", "/swarm/spawn", SECRET))
        inner.assert_not_awaited()
        assert _status(sent) == 401


# ---------------------------------------------------------------------------
# No worker secret / token configured → operator-only (unchanged).
# ---------------------------------------------------------------------------


class TestNoWorkerSecret:
    async def test_no_secret_self_report_requires_operator(self) -> None:
        mw = TokenAuthMiddleware(AsyncMock(), OPERATOR)
        inner, sent = await _call(mw, _http_scope("POST", "/agent/agent_A/status", "anything"))
        inner.assert_not_awaited()
        assert _status(sent) == 401

    async def test_no_secret_operator_token_works(self) -> None:
        mw = TokenAuthMiddleware(AsyncMock(), OPERATOR)
        inner, _ = await _call(mw, _http_scope("POST", "/agent/agent_A/status", OPERATOR))
        inner.assert_awaited_once()

    async def test_secret_but_no_fleet_token_enforced_implicitly(self) -> None:
        """worker_secret set, worker_token None: derived works, no raw token to accept."""
        mw = TokenAuthMiddleware(
            AsyncMock(),
            OPERATOR,
            worker_token=None,
            worker_secret=SECRET,
            enforce_per_agent_worker_token=False,
        )
        tok = derive_agent_token(SECRET, "agent_A")
        inner, _ = await _call(mw, _http_scope("POST", "/agent/agent_A/status", tok))
        inner.assert_awaited_once()


# ---------------------------------------------------------------------------
# Bad / empty token → 401 in every mode.
# ---------------------------------------------------------------------------


class TestBadTokens:
    @pytest.mark.parametrize("enforce", [True, False])
    @pytest.mark.parametrize("bad", ["", "totally-wrong"])
    async def test_bad_token_rejected_on_self_report(self, enforce: bool, bad: str) -> None:
        mw = _make_mw(enforce=enforce)
        inner, sent = await _call(mw, _http_scope("POST", "/agent/agent_A/status", bad))
        inner.assert_not_awaited()
        assert _status(sent) == 401
