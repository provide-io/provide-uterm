#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for entry.py and state/registry.py."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Ensure cf_types fallback classes (Response, WorkerEntrypoint, DurableObject) are
# loaded before entry.py is imported — entry.py's module-level class definition
# ``class Default(WorkerEntrypoint)`` needs WorkerEntrypoint to be non-None.
import provide.terminal.cloudflare.cf_types  # noqa: F401

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_default(env_attrs: dict | None = None):
    from provide.terminal.cloudflare.entry import Default

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
    from provide.terminal.cloudflare.entry import _resolve_spa_route

    assert _resolve_spa_route("/") == ("dashboard", {})


def test_resolve_spa_route_app() -> None:
    from provide.terminal.cloudflare.entry import _resolve_spa_route

    assert _resolve_spa_route("/app") == ("dashboard", {})


def test_resolve_spa_route_app_slash() -> None:
    from provide.terminal.cloudflare.entry import _resolve_spa_route

    assert _resolve_spa_route("/app/") == ("dashboard", {})


def test_resolve_spa_route_connect() -> None:
    from provide.terminal.cloudflare.entry import _resolve_spa_route

    assert _resolve_spa_route("/app/connect") == ("connect", {})


def test_resolve_spa_route_connect_slash() -> None:
    from provide.terminal.cloudflare.entry import _resolve_spa_route

    assert _resolve_spa_route("/app/connect/") == ("connect", {})


def test_resolve_spa_route_session() -> None:
    from provide.terminal.cloudflare.entry import _resolve_spa_route

    kind, extra = _resolve_spa_route("/app/session/abc-123")  # type: ignore[misc]
    assert kind == "session"
    assert extra["session_id"] == "abc-123"
    assert extra["surface"] == "user"


def test_resolve_spa_route_operator() -> None:
    from provide.terminal.cloudflare.entry import _resolve_spa_route

    kind, extra = _resolve_spa_route("/app/operator/abc-123")  # type: ignore[misc]
    assert kind == "operator"
    assert extra["surface"] == "operator"


def test_resolve_spa_route_replay() -> None:
    from provide.terminal.cloudflare.entry import _resolve_spa_route

    kind, extra = _resolve_spa_route("/app/replay/abc-123")  # type: ignore[misc]
    assert kind == "replay"
    assert extra["surface"] == "operator"


def test_resolve_spa_route_unknown() -> None:
    from provide.terminal.cloudflare.entry import _resolve_spa_route

    assert _resolve_spa_route("/app/unknown") is None
    assert _resolve_spa_route("/random") is None


# ---------------------------------------------------------------------------
# _spa_response (lines 144-183)
# ---------------------------------------------------------------------------


def test_spa_response_dashboard() -> None:
    from provide.terminal.cloudflare.entry import _spa_response

    resp = _spa_response("dashboard")
    assert resp.status == 200
    body = resp.body
    assert "dashboard" in body
    assert "xterm" in body.lower()
    assert "server-session-page.js" in body


def test_spa_response_session_includes_hijack_js() -> None:
    from provide.terminal.cloudflare.entry import _spa_response

    resp = _spa_response("session", session_id="s1")
    body = resp.body
    assert "hijack.js" in body
    assert "server-session-page.js" in body
    assert "s1" in body


def test_spa_response_operator_includes_hijack_js() -> None:
    from provide.terminal.cloudflare.entry import _spa_response

    resp = _spa_response("operator", session_id="s1")
    assert "hijack.js" in resp.body


def test_spa_response_replay_uses_replay_script() -> None:
    from provide.terminal.cloudflare.entry import _spa_response

    resp = _spa_response("replay", session_id="r1")
    assert "server-replay-page.js" in resp.body
    assert "hijack.js" not in resp.body


def test_spa_response_connect() -> None:
    from provide.terminal.cloudflare.entry import _spa_response

    resp = _spa_response("connect")
    assert "connect" in resp.body
    assert "server-session-page.js" in resp.body


# ---------------------------------------------------------------------------
# _has_cf_service_token (lines 186-204)
# ---------------------------------------------------------------------------


def test_has_cf_service_token_with_access_suffix() -> None:
    from provide.terminal.cloudflare.entry import _has_cf_service_token

    assert _has_cf_service_token(SimpleNamespace(headers={"cf-access-client-id": "abc.access"})) is True


def test_has_cf_service_token_uppercase_header() -> None:
    from provide.terminal.cloudflare.entry import _has_cf_service_token

    assert _has_cf_service_token(SimpleNamespace(headers={"CF-Access-Client-Id": "abc.access"})) is True


def test_has_cf_service_token_without_access_suffix() -> None:
    from provide.terminal.cloudflare.entry import _has_cf_service_token

    assert _has_cf_service_token(SimpleNamespace(headers={"cf-access-client-id": "abc123"})) is False


def test_has_cf_service_token_no_header() -> None:
    from provide.terminal.cloudflare.entry import _has_cf_service_token

    assert _has_cf_service_token(SimpleNamespace(headers={})) is False


def test_has_cf_service_token_exception_handling() -> None:
    from provide.terminal.cloudflare.entry import _has_cf_service_token

    class _Bad:
        @property
        def headers(self):
            raise RuntimeError("boom")

    assert _has_cf_service_token(_Bad()) is False


# ---------------------------------------------------------------------------
# _handle_connect (lines 245-281)
# ---------------------------------------------------------------------------


async def test_handle_connect_post_creates_session() -> None:
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_connect

    async def _json():
        return {"connector_type": "telnet", "display_name": "My Session", "input_mode": "open"}

    req = SimpleNamespace(method="POST", json=_json)
    kv = AsyncMock()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    resp = await _handle_connect(req, env, CloudflareConfig())
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["connector_type"] == "telnet"
    assert data["display_name"] == "My Session"
    assert data["input_mode"] == "open"
    assert data["url"].startswith("/app/session/connect-")
    kv.put.assert_awaited_once()


async def test_handle_connect_non_post_returns_405() -> None:
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_connect

    resp = await _handle_connect(SimpleNamespace(method="GET"), SimpleNamespace(), CloudflareConfig())
    assert resp.status == 405


async def test_handle_connect_ushell_prefix() -> None:
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_connect

    async def _json():
        return {"connector_type": "ushell"}

    kv = AsyncMock()
    resp = await _handle_connect(
        SimpleNamespace(method="POST", json=_json), SimpleNamespace(SESSION_REGISTRY=kv), CloudflareConfig()
    )
    assert json.loads(resp.body)["session_id"].startswith("ushell-")


async def test_handle_connect_no_kv_returns_500() -> None:
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_connect

    async def _json():
        return {}

    resp = await _handle_connect(
        SimpleNamespace(method="POST", json=_json), SimpleNamespace(SESSION_REGISTRY=None), CloudflareConfig()
    )
    assert resp.status == 500


async def test_handle_connect_bad_json_uses_defaults() -> None:
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_connect

    async def _json():
        raise ValueError("bad json")

    kv = AsyncMock()
    resp = await _handle_connect(
        SimpleNamespace(method="POST", json=_json), SimpleNamespace(SESSION_REGISTRY=kv), CloudflareConfig()
    )
    assert json.loads(resp.body)["connector_type"] == "shell"


async def test_handle_connect_dev_mode_sets_public_no_owner() -> None:
    """In dev/none mode, quick-connect sessions must be public with no owner."""
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_connect

    async def _json():
        return {}

    kv = AsyncMock()
    cfg = CloudflareConfig()  # defaults to dev/none
    resp = await _handle_connect(
        SimpleNamespace(method="POST", json=_json, headers=SimpleNamespace(get=lambda k, d=None: None)),
        SimpleNamespace(SESSION_REGISTRY=kv),
        cfg,
    )
    data = json.loads(resp.body)
    assert data["owner"] is None
    assert data["visibility"] == "public"


# ---------------------------------------------------------------------------
# _handle_sessions DELETE (lines 227-238)
# ---------------------------------------------------------------------------


async def test_handle_sessions_delete_purges_kv() -> None:
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_sessions

    kv = AsyncMock()
    kv.list.return_value = SimpleNamespace(keys=[SimpleNamespace(name="session:abc")])
    resp = await _handle_sessions(
        SimpleNamespace(method="DELETE"), SimpleNamespace(SESSION_REGISTRY=kv), CloudflareConfig()
    )
    data = json.loads(resp.body)
    assert data["ok"] is True and data["deleted"] == 1
    # Must filter to session: prefix so profile keys are not deleted.
    kv.list.assert_awaited_once_with(prefix="session:")


async def test_handle_sessions_delete_preserves_profile_keys() -> None:
    """Bulk delete must only remove session:* keys, not profile:* keys."""
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_sessions

    deleted: list[str] = []

    class _FakeKV:
        async def list(self, *, prefix: str = "") -> object:
            # Simulate registry containing both session and profile keys;
            # only return keys matching the given prefix.
            all_keys = ["session:s1", "session:s2", "profile:p1"]
            return SimpleNamespace(keys=[SimpleNamespace(name=k) for k in all_keys if k.startswith(prefix)])

        async def delete(self, key: str) -> None:
            deleted.append(key)

    await _handle_sessions(
        SimpleNamespace(method="DELETE"), SimpleNamespace(SESSION_REGISTRY=_FakeKV()), CloudflareConfig()
    )
    assert sorted(deleted) == ["session:s1", "session:s2"]
    assert "profile:p1" not in deleted


async def test_handle_sessions_delete_no_kv_returns_500() -> None:
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_sessions

    resp = await _handle_sessions(SimpleNamespace(method="DELETE"), SimpleNamespace(), CloudflareConfig())
    assert resp.status == 500


async def test_handle_sessions_delete_non_admin_returns_403() -> None:
    """Bulk DELETE requires admin role; non-admin principals receive 403."""
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_sessions

    with patch(
        "provide.terminal.cloudflare.entry._decode_jwt_principal",
        new=AsyncMock(return_value=SimpleNamespace(subject_id="bob", roles=("viewer",))),
    ):
        resp = await _handle_sessions(SimpleNamespace(method="DELETE"), SimpleNamespace(), CloudflareConfig())
    assert resp.status == 403
    assert "admin role required" in json.loads(resp.body)["error"]


# ---------------------------------------------------------------------------
# _handle_session_delete (lines 284-294)
# ---------------------------------------------------------------------------


async def test_handle_session_delete_forwards_to_do() -> None:
    from provide.terminal.cloudflare.cf_types import Response
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_session_delete

    stub = SimpleNamespace(fetch=AsyncMock(return_value=Response(body='{"ok":true}', status=200)))
    ns = SimpleNamespace(idFromName=lambda wid: "sid", get=lambda sid: stub)
    env = SimpleNamespace(SESSION_REGISTRY=AsyncMock(), SESSION_RUNTIME=ns)
    with patch("provide.terminal.cloudflare.entry.delete_kv_session", new=AsyncMock()) as mock_del:
        with patch("provide.terminal.cloudflare.entry.get_kv_session", new=AsyncMock(return_value=None)):
            resp = await _handle_session_delete(SimpleNamespace(method="DELETE"), env, "sess-123", CloudflareConfig())
    mock_del.assert_awaited_once_with(env, "sess-123")
    assert json.loads(resp.body)["deleted"] is True


async def test_handle_session_delete_no_do_binding() -> None:
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_session_delete

    with patch("provide.terminal.cloudflare.entry.delete_kv_session", new=AsyncMock()):
        with patch("provide.terminal.cloudflare.entry.get_kv_session", new=AsyncMock(return_value=None)):
            resp = await _handle_session_delete(
                SimpleNamespace(method="DELETE"),
                SimpleNamespace(SESSION_REGISTRY=AsyncMock()),
                "s1",
                CloudflareConfig(),
            )
    assert resp.status == 200


async def test_handle_session_delete_do_exception_returns_500() -> None:
    """DO cleanup failure returns 500 and does NOT delete the KV entry."""
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_session_delete

    async def _bad_fetch(req):
        raise RuntimeError("DO down")

    stub = SimpleNamespace(fetch=_bad_fetch)
    ns = SimpleNamespace(idFromName=lambda wid: "sid", get=lambda sid: stub)
    mock_del = AsyncMock()
    with patch("provide.terminal.cloudflare.entry.delete_kv_session", new=mock_del):
        with patch("provide.terminal.cloudflare.entry.get_kv_session", new=AsyncMock(return_value=None)):
            resp = await _handle_session_delete(
                SimpleNamespace(),
                SimpleNamespace(SESSION_REGISTRY=AsyncMock(), SESSION_RUNTIME=ns),
                "s1",
                CloudflareConfig(),
            )
    assert resp.status == 500
    assert "do_cleanup_failed" in json.loads(resp.body)["error"]
    mock_del.assert_not_awaited()


async def test_handle_session_delete_forbidden_for_non_owner() -> None:
    """In JWT mode, non-owner non-admin callers must receive 403."""

    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_session_delete

    session_data = {"owner": "alice", "visibility": "private"}
    cfg = CloudflareConfig()

    async def _fake_get_kv(env, sid):
        return session_data

    with patch("provide.terminal.cloudflare.entry.get_kv_session", new=_fake_get_kv):
        with patch(
            "provide.terminal.cloudflare.entry._decode_jwt_principal",
            new=AsyncMock(return_value=SimpleNamespace(subject_id="bob", roles=("viewer",))),
        ):
            resp = await _handle_session_delete(SimpleNamespace(), SimpleNamespace(), "s1", cfg)
    assert resp.status == 403


# ---------------------------------------------------------------------------
# _match_api_route (lines 328-341)
# ---------------------------------------------------------------------------


def test_match_api_route_sessions() -> None:
    from provide.terminal.cloudflare.entry import _match_api_route

    assert _match_api_route("/api/sessions", _req("/api/sessions")) is not None


def test_match_api_route_connect() -> None:
    from provide.terminal.cloudflare.entry import _match_api_route

    assert _match_api_route("/api/connect", _req("/api/connect")) is not None


def test_match_api_route_session_delete() -> None:
    from provide.terminal.cloudflare.entry import _match_api_route

    assert _match_api_route("/api/sessions/abc-123", _req("/api/sessions/abc-123", method="DELETE")) is not None


def test_match_api_route_session_get_no_match() -> None:
    from provide.terminal.cloudflare.entry import _match_api_route

    assert _match_api_route("/api/sessions/abc-123", _req("/api/sessions/abc-123", method="GET")) is None


def test_match_api_route_spa_routes() -> None:
    from provide.terminal.cloudflare.entry import _match_api_route

    assert _match_api_route("/", _req("/")) is not None
    assert _match_api_route("/app/connect", _req("/app/connect")) is not None


def test_match_api_route_unknown() -> None:
    from provide.terminal.cloudflare.entry import _match_api_route

    assert _match_api_route("/api/unknown", _req("/api/unknown")) is None


def test_match_api_route_tunnel_revoke() -> None:
    from provide.terminal.cloudflare.entry import _match_api_route

    assert (
        _match_api_route("/api/tunnels/tunnel-abc/tokens", _req("/api/tunnels/tunnel-abc/tokens", method="DELETE"))
        is not None
    )


def test_match_api_route_tunnel_revoke_wrong_method() -> None:
    from provide.terminal.cloudflare.entry import _match_api_route

    assert (
        _match_api_route("/api/tunnels/tunnel-abc/tokens", _req("/api/tunnels/tunnel-abc/tokens", method="GET")) is None
    )


def test_match_api_route_tunnel_rotate() -> None:
    from provide.terminal.cloudflare.entry import _match_api_route

    assert (
        _match_api_route(
            "/api/tunnels/tunnel-abc/tokens/rotate", _req("/api/tunnels/tunnel-abc/tokens/rotate", method="POST")
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Default.fetch() integration — exercises _api_connect/_api_sessions wrappers
# ---------------------------------------------------------------------------


async def test_default_fetch_api_connect_post() -> None:
    """POST /api/connect through Default.fetch() exercises _api_connect (line 349)."""
    kv = AsyncMock()
    kv.put = AsyncMock()
    d = _make_default({"SESSION_REGISTRY": kv})

    async def _json():
        return {"connector_type": "telnet", "display_name": "Test"}

    req = SimpleNamespace(
        url="https://x/api/connect",
        method="POST",
        json=_json,
        headers=SimpleNamespace(get=lambda k, default=None: None),
    )
    resp = await d.fetch(req)
    assert resp.status == 200
    assert json.loads(resp.body)["connector_type"] == "telnet"


async def test_default_fetch_session_delete() -> None:
    """DELETE /api/sessions/{id} through Default.fetch()."""
    d = _make_default()
    req = SimpleNamespace(
        url="https://x/api/sessions/test-sess",
        method="DELETE",
        headers=SimpleNamespace(get=lambda k, default=None: None),
    )
    with patch("provide.terminal.cloudflare.entry.delete_kv_session", new=AsyncMock()):
        resp = await d.fetch(req)
    assert resp.status == 200 and json.loads(resp.body)["deleted"] is True


async def test_route_request_cf_service_token_bypasses_jwt() -> None:
    """CF Access service token (.access suffix) bypasses JWT auth."""
    from provide.terminal.cloudflare.entry import Default

    d = Default(
        SimpleNamespace(
            AUTH_MODE="jwt",
            JWT_ALGORITHMS="HS256",
            JWT_PUBLIC_KEY_PEM="uterm-test-secret-32-byte-minimum-key",
            WORKER_BEARER_TOKEN="tok",
        )
    )

    def _get(k, default=None):
        if k in ("cf-access-client-id", "CF-Access-Client-Id"):
            return "my-client.access"
        return None

    req = SimpleNamespace(url="https://x/api/sessions", method="GET", headers=SimpleNamespace(get=_get))
    with patch("provide.terminal.cloudflare.entry.list_kv_sessions", new=AsyncMock(return_value=[])):
        resp = await d.fetch(req)
    assert resp.status == 200


# ---------------------------------------------------------------------------
# state/registry.py — delete_kv_session (lines 74-87)
# ---------------------------------------------------------------------------


async def test_delete_kv_session_deletes_key() -> None:
    from provide.terminal.cloudflare.state.registry import delete_kv_session

    kv = AsyncMock()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    await delete_kv_session(env, "my-worker")
    kv.delete.assert_awaited_once_with("session:my-worker")


async def test_delete_kv_session_no_kv_noop() -> None:
    from provide.terminal.cloudflare.state.registry import delete_kv_session

    await delete_kv_session(SimpleNamespace(), "my-worker")


async def test_delete_kv_session_exception_suppressed() -> None:
    from provide.terminal.cloudflare.state.registry import delete_kv_session

    kv = AsyncMock()
    kv.delete.side_effect = RuntimeError("kv error")
    await delete_kv_session(SimpleNamespace(SESSION_REGISTRY=kv), "my-worker")


# ---------------------------------------------------------------------------
# _decode_jwt_principal — JwtValidationError path (entry.py:251-254)
# ---------------------------------------------------------------------------


async def test_decode_jwt_principal_bad_token_returns_none() -> None:
    """_decode_jwt_principal swallows JwtValidationError and returns None."""
    import time

    import jwt as pyjwt
    from provide.terminal.cloudflare.config import CloudflareConfig, JwtConfig
    from provide.terminal.cloudflare.entry import _decode_jwt_principal

    good_key = "test-secret-key-32-bytes-minimum!"
    wrong_key = "wrong-secret-key-32-bytes-minimum"
    now = int(time.time())
    # Token signed with wrong_key — will fail validation against good_key
    token = pyjwt.encode({"sub": "u1", "exp": now + 600}, wrong_key, algorithm="HS256")

    cfg = CloudflareConfig(jwt=JwtConfig(mode="jwt", public_key_pem=good_key, algorithms=("HS256",)))
    req = SimpleNamespace(
        headers=SimpleNamespace(
            # Only return the bearer for the authorization header — other header
            # names (CF Access) should return default so the bearer-fallback path
            # runs and hits JwtValidationError.
            get=lambda k, default=None: f"Bearer {token}" if k.lower() == "authorization" else default
        )
    )
    result = await _decode_jwt_principal(req, cfg)
    assert result is None


async def test_decode_jwt_principal_cf_access_email_returns_viewer_principal() -> None:
    """CF Access authenticated-user-email maps to a viewer-role Principal.

    Regression guard for the service-token/CF-Access principal-collapse
    bug: before this fix, _decode_jwt_principal returned None for a CF
    Access authenticated request, and downstream handlers treated that as
    anonymous open-access.
    """
    from provide.terminal.cloudflare.config import CloudflareConfig, JwtConfig
    from provide.terminal.cloudflare.entry import _decode_jwt_principal

    cfg = CloudflareConfig(jwt=JwtConfig(mode="jwt", public_key_pem="k", algorithms=("HS256",)))
    req = SimpleNamespace(
        headers=SimpleNamespace(
            get=lambda k, default=None: (
                "alice@example.com" if k.lower() == "cf-access-authenticated-user-email" else default
            )
        )
    )
    result = await _decode_jwt_principal(req, cfg)
    assert result is not None
    assert result.subject_id == "alice@example.com"
    assert "viewer" in result.roles


async def test_decode_jwt_principal_cf_access_service_token_returns_admin_principal() -> None:
    """CF Access service token maps to a service:<client_id> admin Principal.

    Service tokens are machine-to-machine; they get admin role so
    downstream ownership/capability checks enforce authorization while
    still allowing the service caller the broad access a server-to-server
    integration typically needs.
    """
    from provide.terminal.cloudflare.config import CloudflareConfig, JwtConfig
    from provide.terminal.cloudflare.entry import _decode_jwt_principal

    cfg = CloudflareConfig(jwt=JwtConfig(mode="jwt", public_key_pem="k", algorithms=("HS256",)))
    client_id = "svc123.access"
    req = SimpleNamespace(
        headers=SimpleNamespace(
            get=lambda k, default=None: client_id if k.lower() == "cf-access-client-id" else default
        )
    )
    result = await _decode_jwt_principal(req, cfg)
    assert result is not None
    assert result.subject_id == f"service:{client_id}"
    assert "admin" in result.roles


# ---------------------------------------------------------------------------
# _handle_session_delete — session not in KV skips auth (entry.py:327->333)
# ---------------------------------------------------------------------------


async def test_handle_session_delete_session_not_in_kv_returns_404() -> None:
    """When session is absent from KV and principal is present, delete fails closed with 404."""
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_session_delete

    mock_del = AsyncMock()
    with patch("provide.terminal.cloudflare.entry.delete_kv_session", new=mock_del):
        with patch(
            "provide.terminal.cloudflare.entry._decode_jwt_principal",
            new=AsyncMock(return_value=SimpleNamespace(subject_id="bob", roles=("viewer",))),
        ):
            with patch("provide.terminal.cloudflare.entry.get_kv_session", new=AsyncMock(return_value=None)):
                resp = await _handle_session_delete(SimpleNamespace(), SimpleNamespace(), "s1", CloudflareConfig())
    # KV is the auth source — missing row must deny (fail closed), not proceed.
    assert resp.status == 404
    mock_del.assert_not_awaited()


# ---------------------------------------------------------------------------
# _handle_session_delete — admin/owner is authorized (entry.py:331->333)
# ---------------------------------------------------------------------------


async def test_handle_session_delete_admin_allowed() -> None:
    """Admin callers can delete any session."""
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.entry import _handle_session_delete

    session_data = {"owner": "alice", "visibility": "private"}

    with patch("provide.terminal.cloudflare.entry.delete_kv_session", new=AsyncMock()):
        with patch(
            "provide.terminal.cloudflare.entry._decode_jwt_principal",
            new=AsyncMock(return_value=SimpleNamespace(subject_id="bob", roles=("admin",))),
        ):
            with patch(
                "provide.terminal.cloudflare.entry.get_kv_session",
                new=AsyncMock(return_value=session_data),
            ):
                resp = await _handle_session_delete(SimpleNamespace(), SimpleNamespace(), "s1", CloudflareConfig())
    assert resp.status == 200
    assert json.loads(resp.body)["deleted"] is True


# ---------------------------------------------------------------------------
# state/registry.py — get_kv_session exception handler (lines 86-88)
# ---------------------------------------------------------------------------


async def test_get_kv_session_kv_exception_returns_none() -> None:
    """get_kv_session catches KV errors and returns None."""
    from provide.terminal.cloudflare.state.registry import get_kv_session

    kv = AsyncMock()
    kv.get.side_effect = RuntimeError("kv unavailable")
    result = await get_kv_session(SimpleNamespace(SESSION_REGISTRY=kv), "w1")
    assert result is None
