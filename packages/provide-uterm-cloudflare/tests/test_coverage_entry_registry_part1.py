#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for entry.py and state/registry.py."""

from __future__ import annotations

from types import SimpleNamespace

# Ensure cf_types fallback classes (Response, WorkerEntrypoint, DurableObject) are
# loaded before entry.py is imported — entry.py's module-level class definition
# ``class Default(WorkerEntrypoint)`` needs WorkerEntrypoint to be non-None.
import provide.uterm.cloudflare.cf_types  # noqa: F401

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_default(env_attrs: dict | None = None):
    from provide.uterm.cloudflare.entry import Default

    attrs: dict = {"AUTH_MODE": "dev"}
    if env_attrs:
        attrs.update(env_attrs)
    return Default(SimpleNamespace(**attrs))


def _req(path: str, method: str = "GET", headers: dict | None = None) -> SimpleNamespace:
    hdr = headers or {}

    def _get(k, default=None):
        return hdr.get(k, default)

    return SimpleNamespace(url=f"https://x{path}", method=method, headers=SimpleNamespace(get=_get))


# ---------------------------------------------------------------------------
# _resolve_spa_route (lines 129-141)
# ---------------------------------------------------------------------------


def test_resolve_spa_route_root() -> None:
    from provide.uterm.cloudflare.entry.spa import _resolve_spa_route

    assert _resolve_spa_route("/") == ("dashboard", {})


def test_resolve_spa_route_app() -> None:
    from provide.uterm.cloudflare.entry.spa import _resolve_spa_route

    assert _resolve_spa_route("/app") == ("dashboard", {})


def test_resolve_spa_route_app_slash() -> None:
    from provide.uterm.cloudflare.entry.spa import _resolve_spa_route

    assert _resolve_spa_route("/app/") == ("dashboard", {})


def test_resolve_spa_route_connect() -> None:
    from provide.uterm.cloudflare.entry.spa import _resolve_spa_route

    assert _resolve_spa_route("/app/connect") == ("connect", {})


def test_resolve_spa_route_connect_slash() -> None:
    from provide.uterm.cloudflare.entry.spa import _resolve_spa_route

    assert _resolve_spa_route("/app/connect/") == ("connect", {})


def test_resolve_spa_route_session() -> None:
    from provide.uterm.cloudflare.entry.spa import _resolve_spa_route

    kind, extra = _resolve_spa_route("/app/session/abc-123")  # type: ignore[misc]
    assert kind == "session"
    assert extra["session_id"] == "abc-123"
    assert extra["surface"] == "user"


def test_resolve_spa_route_operator() -> None:
    from provide.uterm.cloudflare.entry.spa import _resolve_spa_route

    kind, extra = _resolve_spa_route("/app/operator/abc-123")  # type: ignore[misc]
    assert kind == "operator"
    assert extra["surface"] == "operator"


def test_resolve_spa_route_replay() -> None:
    from provide.uterm.cloudflare.entry.spa import _resolve_spa_route

    kind, extra = _resolve_spa_route("/app/replay/abc-123")  # type: ignore[misc]
    assert kind == "replay"
    assert extra["surface"] == "operator"


def test_resolve_spa_route_unknown() -> None:
    from provide.uterm.cloudflare.entry.spa import _resolve_spa_route

    assert _resolve_spa_route("/app/unknown") is None
    assert _resolve_spa_route("/random") is None


# ---------------------------------------------------------------------------
# _spa_response (lines 144-183)
# ---------------------------------------------------------------------------


def test_spa_response_dashboard() -> None:
    from provide.uterm.cloudflare.entry.spa import _spa_response

    resp = _spa_response("dashboard")
    assert resp.status == 200
    body = resp.body
    assert "dashboard" in body
    assert "xterm" in body.lower()
    assert "server-session-page.js" in body


def test_spa_response_session_includes_hijack_js() -> None:
    from provide.uterm.cloudflare.entry.spa import _spa_response

    resp = _spa_response("session", session_id="s1")
    body = resp.body
    assert "hijack.js" in body
    assert "server-session-page.js" in body
    assert "s1" in body


def test_spa_response_operator_includes_hijack_js() -> None:
    from provide.uterm.cloudflare.entry.spa import _spa_response

    resp = _spa_response("operator", session_id="s1")
    assert "hijack.js" in resp.body


def test_spa_response_replay_uses_replay_script() -> None:
    from provide.uterm.cloudflare.entry.spa import _spa_response

    resp = _spa_response("replay", session_id="r1")
    assert "server-replay-page.js" in resp.body
    assert "hijack.js" not in resp.body


def test_spa_response_connect() -> None:
    from provide.uterm.cloudflare.entry.spa import _spa_response

    resp = _spa_response("connect")
    assert "connect" in resp.body
    assert "server-session-page.js" in resp.body


# ---------------------------------------------------------------------------
# _has_cf_service_token (header trust disabled)
# ---------------------------------------------------------------------------


def test_has_cf_service_token_with_access_suffix() -> None:
    from provide.uterm.cloudflare.entry.auth import _has_cf_service_token

    assert _has_cf_service_token(SimpleNamespace(headers={"cf-access-client-id": "abc.access"})) is False


def test_has_cf_service_token_uppercase_header() -> None:
    from provide.uterm.cloudflare.entry.auth import _has_cf_service_token

    assert _has_cf_service_token(SimpleNamespace(headers={"CF-Access-Client-Id": "abc.access"})) is False


def test_has_cf_service_token_without_access_suffix() -> None:
    from provide.uterm.cloudflare.entry.auth import _has_cf_service_token

    assert _has_cf_service_token(SimpleNamespace(headers={"cf-access-client-id": "abc123"})) is False


def test_has_cf_service_token_no_header() -> None:
    from provide.uterm.cloudflare.entry.auth import _has_cf_service_token

    assert _has_cf_service_token(SimpleNamespace(headers={})) is False
