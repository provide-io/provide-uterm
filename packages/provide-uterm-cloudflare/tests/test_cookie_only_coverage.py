from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.tunnel.token_hash import hash_token


def _make_dev_default() -> object:
    """Build a Default configured for the legacy open-access path.

    from_env only accepts jwt mode now; build a valid jwt config and override the
    in-memory mode to ``dev`` (reachable only via direct config mutation).
    """
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry import Default

    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM="test-secret-key-32-bytes-minimum!",
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
    )
    d = Default(env)
    d._config = CloudflareConfig.from_env(env)
    d._config.jwt.mode = "dev"
    return d


class _Headers:
    def __init__(self, *, cookie: str | None = None, ip: str | None = None, raise_ip: bool = False) -> None:
        self.cookie = cookie
        self.ip = ip
        self.raise_ip = raise_ip

    def get(self, key: str, default=None):
        if key in {"cookie", "Cookie"}:
            return self.cookie if key == "cookie" else default
        if key in {"CF-Connecting-IP", "cf-connecting-ip"}:
            if self.raise_ip:
                raise RuntimeError("ip header unavailable")
            return self.ip or default
        return default


async def test_revoke_returns_none_when_kv_get_raises() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_revoke_tokens

    kv = MagicMock()
    kv.get = AsyncMock(side_effect=RuntimeError("kv unavailable"))
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await handle_tunnel_revoke_tokens(MagicMock(), env, "tunnel-abc") is None


async def test_rotate_returns_none_when_kv_get_raises() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import handle_tunnel_rotate_tokens

    kv = MagicMock()
    kv.get = AsyncMock(side_effect=RuntimeError("kv unavailable"))
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await handle_tunnel_rotate_tokens(MagicMock(), env, "tunnel-abc") is None


async def test_resolve_share_context_ip_header_exception_rejects_bound_cookie() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import resolve_share_context

    session = {
        "share_token_hash": hash_token("share-tok"),
        "control_token_hash": hash_token("control-tok"),
        "issued_ip": "203.0.113.10",
        "expires_at": time.time() + 3600,
    }
    kv = MagicMock()
    kv.get = AsyncMock(return_value=json.dumps(session))
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    request = SimpleNamespace(
        url="https://example.invalid/app/session/tunnel-abc",
        headers=_Headers(cookie="uterm_tunnel_tunnel-abc=share-tok", raise_ip=True),
    )
    config = SimpleNamespace(tunnel_ip_binding=True)

    assert await resolve_share_context(request, env, "tunnel-abc", config) is None


async def test_consume_tunnel_invite_rejects_bad_url() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    class _BadUrl:
        def __str__(self) -> str:
            raise RuntimeError("bad url")

    request = SimpleNamespace(url=_BadUrl())
    env = SimpleNamespace(SESSION_REGISTRY=MagicMock())

    assert await consume_tunnel_invite(request, env, "tunnel-abc") is None


async def test_consume_tunnel_invite_rejects_missing_kv() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=abc")
    env = SimpleNamespace(SESSION_REGISTRY=None)

    assert await consume_tunnel_invite(request, env, "tunnel-abc") is None


async def test_consume_tunnel_invite_rejects_missing_entry() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    kv = MagicMock()
    kv.get = AsyncMock(return_value=None)
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=abc")
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await consume_tunnel_invite(request, env, "tunnel-abc") is None


async def test_consume_tunnel_invite_rejects_corrupt_entry() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    kv = MagicMock()
    kv.get = AsyncMock(return_value="{not-json")
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=abc")
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await consume_tunnel_invite(request, env, "tunnel-abc") is None


async def test_consume_tunnel_invite_rejects_revoked_entry() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    kv = MagicMock()
    kv.get = AsyncMock(return_value=json.dumps({"revoked": True}))
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=abc")
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await consume_tunnel_invite(request, env, "tunnel-abc") is None


async def test_consume_tunnel_invite_rejects_expired_tunnel() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    kv = MagicMock()
    kv.get = AsyncMock(return_value=json.dumps({"expires_at": time.time() - 1}))
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=abc")
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await consume_tunnel_invite(request, env, "tunnel-abc") is None


async def test_consume_tunnel_invite_skips_missing_invite_shape_without_put() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    kv = MagicMock()
    kv.get = AsyncMock(return_value=json.dumps({"expires_at": time.time() + 3600}))
    kv.put = AsyncMock()
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=abc")
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await consume_tunnel_invite(request, env, "tunnel-abc") is None
    kv.put.assert_not_called()


async def test_consume_tunnel_invite_clears_expired_invite_then_no_match() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    session = {
        "expires_at": time.time() + 3600,
        "share_token_hash": hash_token("share-tok"),
        "share_invite_hash": hash_token("invite-tok"),
        "share_invite_token": "share-tok",
        "share_invite_expires_at": time.time() - 1,
    }
    kv = MagicMock()
    kv.get = AsyncMock(return_value=json.dumps(session))
    kv.put = AsyncMock()
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=invite-tok")
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await consume_tunnel_invite(request, env, "tunnel-abc") is None
    kv.put.assert_not_called()


async def test_consume_tunnel_invite_accepts_control_invite() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    session = {
        "expires_at": time.time() + 3600,
        "control_token_hash": hash_token("control-tok"),
        "control_invite_hash": hash_token("control-invite"),
        "control_invite_token": "control-tok",
        "control_invite_expires_at": time.time() + 60,
    }
    kv = MagicMock()
    kv.get = AsyncMock(return_value=json.dumps(session))
    kv.put = AsyncMock()
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=control-invite")
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await consume_tunnel_invite(request, env, "tunnel-abc") == ("operator", "operator", "control-tok")
    stored = json.loads(kv.put.call_args[0][1])
    assert "control_invite_hash" not in stored


async def test_consume_tunnel_invite_skips_non_matching_control_then_matches_share() -> None:
    from provide.uterm.cloudflare.api._tunnel_api import consume_tunnel_invite

    session = {
        "expires_at": time.time() + 3600,
        "control_token_hash": hash_token("control-tok"),
        "control_invite_hash": hash_token("other-invite"),
        "control_invite_token": "control-tok",
        "control_invite_expires_at": time.time() + 60,
        "share_token_hash": hash_token("share-tok"),
        "share_invite_hash": hash_token("share-invite"),
        "share_invite_token": "share-tok",
        "share_invite_expires_at": time.time() + 60,
    }
    kv = MagicMock()
    kv.get = AsyncMock(return_value=json.dumps(session))
    kv.put = AsyncMock()
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=share-invite")
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    assert await consume_tunnel_invite(request, env, "tunnel-abc") == ("session", "viewer", "share-tok")


async def test_short_share_invite_without_cookie_header_still_redirects() -> None:
    d = _make_dev_default()
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc?invite=abc", headers={})
    with (
        patch(
            "provide.uterm.cloudflare.api._tunnel_api.consume_tunnel_invite",
            new=AsyncMock(return_value=("session", "viewer", "share-tok")),
        ),
        patch("provide.uterm.cloudflare.entry.handlers._share_token_cookie_header", return_value=None),
    ):
        resp = await d.fetch(request)

    assert resp.status == 302
    assert dict(resp.headers)["location"] == "/app/session/tunnel-abc"
    assert "Set-Cookie" not in dict(resp.headers)


async def test_short_share_existing_cookie_redirects_without_invite() -> None:
    d = _make_dev_default()
    request = SimpleNamespace(url="https://example.invalid/s/tunnel-abc", headers={})
    with (
        patch(
            "provide.uterm.cloudflare.api._tunnel_api.consume_tunnel_invite",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "provide.uterm.cloudflare.api._tunnel_api.resolve_share_context",
            new=AsyncMock(return_value=("operator", "operator")),
        ),
    ):
        resp = await d.fetch(request)

    assert resp.status == 302
    assert dict(resp.headers)["location"] == "/app/operator/tunnel-abc"


def test_runtime_share_role_wrong_cookie_without_share_hash_returns_none() -> None:
    from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

    runtime = object.__new__(SessionRuntime)
    runtime.worker_id = "tunnel-abc"
    runtime.config = SimpleNamespace(tunnel_ip_binding=False)
    runtime._control_token_hash = hash_token("control-tok")
    runtime._share_token_hash = None
    runtime._issued_ip = None
    runtime._session_expires_at = None
    request = SimpleNamespace(headers=_Headers(cookie="uterm_tunnel_tunnel-abc=wrong"))

    assert runtime._share_role_for_request(request) is None


def test_runtime_share_role_ip_header_exception_rejects_bound_cookie() -> None:
    from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

    runtime = object.__new__(SessionRuntime)
    runtime.worker_id = "tunnel-abc"
    runtime.config = SimpleNamespace(tunnel_ip_binding=True)
    runtime._control_token_hash = None
    runtime._share_token_hash = hash_token("share-tok")
    runtime._issued_ip = "203.0.113.10"
    runtime._session_expires_at = None
    request = SimpleNamespace(headers=_Headers(cookie="uterm_tunnel_tunnel-abc=share-tok", raise_ip=True))

    assert runtime._share_role_for_request(request) is None
