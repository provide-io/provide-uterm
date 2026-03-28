from __future__ import annotations

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from provide.terminal.cloudflare.api.http_routes import route_http
from provide.terminal.cloudflare.auth.jwt import JwtValidationError, decode_jwt
from provide.terminal.cloudflare.bridge.hijack import HijackCoordinator
from provide.terminal.cloudflare.config import CloudflareConfig, JwtConfig
from provide.terminal.cloudflare.do.session_runtime import SessionRuntime


class _Req:
    def __init__(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        self.url = url
        self.method = method
        self.headers = headers or {}


def test_auth_mode_defaults_to_jwt() -> None:
    cfg = CloudflareConfig.from_env({"WORKER_BEARER_TOKEN": "t"})
    assert cfg.jwt.mode == "jwt"


def test_production_rejects_dev_mode() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE"):
        CloudflareConfig.from_env({"ENVIRONMENT": "production", "AUTH_MODE": "dev"})


def test_query_token_disabled_in_production_by_default() -> None:
    cfg = CloudflareConfig.from_env({"ENVIRONMENT": "production", "WORKER_BEARER_TOKEN": "t"})
    assert cfg.jwt.allow_query_token is False


def test_session_runtime_extract_token_respects_query_policy() -> None:
    runtime = object.__new__(SessionRuntime)
    runtime.config = SimpleNamespace(jwt=SimpleNamespace(allow_query_token=False))
    request = _Req("https://example.invalid/ws/browser/agent1/term?token=abc123")
    assert runtime._extract_token(request) is None


async def test_decode_jwt_requires_sub_and_exp() -> None:
    # Token without exp must be rejected (matches FastAPI behaviour: require=[sub, exp]).
    token = jwt.encode({"sub": "u1", "roles": ["viewer"]}, "secret", algorithm="HS256")
    with pytest.raises(JwtValidationError):
        await decode_jwt(token, JwtConfig(mode="jwt", public_key_pem="secret", algorithms=("HS256",)))


async def test_decode_jwt_accepts_token_without_iat_nbf() -> None:
    # iat/nbf no longer required — Auth0/Google/Azure AD tokens may omit them.
    now = int(time.time())
    token = jwt.encode({"sub": "u1", "exp": now + 600, "roles": ["viewer"]}, "secret", algorithm="HS256")
    principal = await decode_jwt(token, JwtConfig(mode="jwt", public_key_pem="secret", algorithms=("HS256",)))
    assert principal.subject_id == "u1"


async def test_decode_jwt_rejects_future_nbf_outside_skew() -> None:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "iat": now, "nbf": now + 120, "exp": now + 3600},
        "secret",
        algorithm="HS256",
    )
    with pytest.raises(JwtValidationError):
        await decode_jwt(
            token,
            JwtConfig(mode="jwt", public_key_pem="secret", algorithms=("HS256",), clock_skew_seconds=10),
        )


class _Runtime:
    def __init__(self) -> None:
        self.worker_id = "w1"
        self.worker_ws = object()
        self.hijack = HijackCoordinator()
        self.persisted: list[float] = []
        self.actions: list[tuple[str, str, int]] = []
        self._role = "admin"
        self.last_snapshot: dict | None = None
        self.browser_hijack_owner: dict[str, str] = {}
        self.input_mode: str = "hijack"

    async def request_json(self, request: object) -> dict[str, object]:
        return json.loads(getattr(request, "_body", "{}"))

    async def browser_role_for_request(self, request: object) -> str:
        return self._role

    def persist_lease(self, session: object) -> None:
        if session is not None:
            self.persisted.append(float(session.lease_expires_at))

    def clear_lease(self) -> None:
        return

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        self.actions.append((action, owner, lease_s))
        return True

    async def broadcast_hijack_state(self) -> None:
        return

    async def push_worker_input(self, data: str) -> bool:
        return bool(data)

    @property
    def store(self) -> object:
        return SimpleNamespace(
            list_events_since=lambda *_args, **_kwargs: [],
            load_session=lambda *_args, **_kwargs: None,
            current_event_seq=lambda *_args, **_kwargs: 0,
            min_event_seq=lambda *_args, **_kwargs: 0,
            save_input_mode=lambda *_args, **_kwargs: None,
        )


@pytest.mark.asyncio
async def test_hijack_acquire_rejects_invalid_lease() -> None:
    runtime = _Runtime()
    req = _Req("https://example.invalid/worker/w1/hijack/acquire", method="POST")
    req._body = json.dumps({"owner": "alice", "lease_s": "oops"})
    resp = await route_http(runtime, req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_hijack_acquire_clamps_lease_bounds() -> None:
    runtime = _Runtime()
    req = _Req("https://example.invalid/worker/w1/hijack/acquire", method="POST")
    req._body = json.dumps({"owner": "alice", "lease_s": 0})
    first = await route_http(runtime, req)
    assert first.status == 200
    assert runtime.actions[-1] == ("pause", "alice", 1)

    hid = runtime.hijack.session.hijack_id if runtime.hijack.session is not None else ""
    hb = _Req(f"https://example.invalid/worker/w1/hijack/{hid}/heartbeat", method="POST")
    hb._body = json.dumps({"lease_s": 999999})
    before = time.time()
    second = await route_http(runtime, hb)
    assert second.status == 200
    payload = json.loads(second.body or "{}")
    expires = float(payload["lease_expires_at"])
    assert 3595 <= (expires - before) <= 3605


@pytest.mark.asyncio
async def test_hijack_step_rest_route_sends_worker_control() -> None:
    runtime = _Runtime()
    acquired = runtime.hijack.acquire("alice", 60)
    assert acquired.ok and acquired.session is not None
    hid = acquired.session.hijack_id
    req = _Req(f"https://example.invalid/worker/w1/hijack/{hid}/step", method="POST")
    req._body = "{}"
    resp = await route_http(runtime, req)
    assert resp.status == 200
    assert runtime.actions[-1][0] == "step"


# ---------------------------------------------------------------------------
# Worker bearer token auth for CF worker WS
# ---------------------------------------------------------------------------


def _make_runtime_with_token(token: str | None = None, mode: str = "dev"):
    """Create a real SessionRuntime with optional worker_bearer_token."""
    import sqlite3

    ctx = SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=sqlite3.connect(":memory:").execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: "test-worker"),
        getWebSockets=list,
        acceptWebSocket=lambda ws: None,
    )
    env_kwargs: dict = {"AUTH_MODE": mode}
    if token is not None:
        env_kwargs["WORKER_BEARER_TOKEN"] = token
    if mode == "jwt":
        env_kwargs["JWT_ALGORITHMS"] = "HS256"
        env_kwargs["JWT_PUBLIC_KEY_PEM"] = "test-secret-key-32-bytes-minimum!"
    return SessionRuntime(ctx, SimpleNamespace(**env_kwargs))


def test_config_reads_worker_bearer_token_from_env() -> None:
    cfg = CloudflareConfig.from_env({"WORKER_BEARER_TOKEN": "my-secret-token"})
    assert cfg.worker_bearer_token == "my-secret-token"


def test_config_worker_bearer_token_defaults_to_none_in_dev_mode() -> None:
    cfg = CloudflareConfig.from_env({"AUTH_MODE": "dev"})
    assert cfg.worker_bearer_token is None


@pytest.mark.asyncio
async def test_worker_ws_rejected_without_bearer_token() -> None:
    runtime = _make_runtime_with_token(token="correct-token", mode="dev")
    req = _Req(
        "https://example.invalid/ws/worker/test-worker/term",
        headers={"Upgrade": "websocket"},
    )
    resp = await runtime.fetch(req)
    assert resp.status == 403
    body = json.loads(resp.body)
    assert "worker authentication required" in body["error"]


@pytest.mark.asyncio
async def test_worker_ws_rejected_with_wrong_bearer_token() -> None:
    runtime = _make_runtime_with_token(token="correct-token", mode="dev")
    req = _Req(
        "https://example.invalid/ws/worker/test-worker/term",
        headers={"Upgrade": "websocket", "Authorization": "Bearer wrong-token"},
    )
    resp = await runtime.fetch(req)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_worker_ws_accepted_with_correct_bearer_token() -> None:
    """Correct bearer token passes auth and reaches the WS upgrade path."""
    import sys
    from types import ModuleType
    from unittest.mock import MagicMock

    runtime = _make_runtime_with_token(token="correct-token", mode="dev")
    req = _Req(
        "https://example.invalid/ws/worker/test-worker/term",
        headers={"Upgrade": "websocket", "Authorization": "Bearer correct-token"},
    )
    # Mock js.WebSocketPair so we don't crash on import
    fake_js = ModuleType("js")
    pair = MagicMock()
    pair.new.return_value = MagicMock(object_values=MagicMock(return_value=(MagicMock(), MagicMock())))
    fake_js.WebSocketPair = pair  # type: ignore[attr-defined]
    sys.modules["js"] = fake_js
    try:
        resp = await runtime.fetch(req)
        assert resp.status == 101
    finally:
        sys.modules.pop("js", None)


def test_jwt_mode_requires_worker_bearer_token() -> None:
    """CloudflareConfig.from_env must raise ValueError when AUTH_MODE=jwt and no WORKER_BEARER_TOKEN."""
    with pytest.raises(ValueError, match="WORKER_BEARER_TOKEN is required"):
        CloudflareConfig.from_env(
            {
                "AUTH_MODE": "jwt",
                "JWT_ALGORITHMS": "HS256",
                "JWT_PUBLIC_KEY_PEM": "test-key",
            }
        )


def test_jwt_mode_accepts_worker_bearer_token() -> None:
    """CloudflareConfig.from_env must succeed when JWT mode has a WORKER_BEARER_TOKEN."""
    cfg = CloudflareConfig.from_env(
        {
            "AUTH_MODE": "jwt",
            "JWT_ALGORITHMS": "HS256",
            "JWT_PUBLIC_KEY_PEM": "test-key",
            "WORKER_BEARER_TOKEN": "my-token",
        }
    )
    assert cfg.worker_bearer_token == "my-token"
    assert cfg.jwt.mode == "jwt"


@pytest.mark.asyncio
async def test_page_routes_require_jwt_in_jwt_mode() -> None:
    """In JWT mode, /app must return 401 when no token is provided."""
    from provide.terminal.cloudflare.entry import Default

    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM="test-secret-key-32-bytes-minimum!",
        WORKER_BEARER_TOKEN="worker-token",
        SESSION_RUNTIME=None,
        SESSION_REGISTRY=None,
    )
    worker = object.__new__(Default)
    worker.env = env
    worker._config = CloudflareConfig.from_env(env)
    req = _Req("https://example.invalid/app")
    resp = await worker.fetch(req)
    assert resp.status == 401
    body = json.loads(resp.body)
    assert body["error"] == "authentication required"


@pytest.mark.asyncio
async def test_page_routes_accessible_in_dev_mode() -> None:
    """In dev mode, /app must return 200 (no auth required)."""
    from provide.terminal.cloudflare.entry import Default

    env = SimpleNamespace(
        AUTH_MODE="dev",
        SESSION_RUNTIME=None,
        SESSION_REGISTRY=None,
    )
    worker = object.__new__(Default)
    worker.env = env
    worker._config = CloudflareConfig.from_env(env)
    req = _Req("https://example.invalid/app")
    resp = await worker.fetch(req)
    # 200 if terminal.html asset exists, or 404 from serve_asset; either way, NOT 401
    assert resp.status != 401


@pytest.mark.asyncio
async def test_page_routes_invalid_jwt_returns_401() -> None:
    """In JWT mode, /app with an invalid token returns 401 with 'invalid token'."""
    from provide.terminal.cloudflare.entry import Default

    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM="test-secret-key-32-bytes-minimum!",
        WORKER_BEARER_TOKEN="worker-token",
        SESSION_RUNTIME=None,
        SESSION_REGISTRY=None,
    )
    worker = object.__new__(Default)
    worker.env = env
    worker._config = CloudflareConfig.from_env(env)
    req = _Req(
        "https://example.invalid/app",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    resp = await worker.fetch(req)
    assert resp.status == 401
    body = json.loads(resp.body)
    assert body["error"] == "invalid token"
    assert "detail" in body
    assert body["detail"] != "None"  # must contain actual error, not str(None)


@pytest.mark.asyncio
async def test_page_routes_valid_jwt_returns_non_401() -> None:
    """In JWT mode, /app with a valid token must NOT return 401."""
    from provide.terminal.cloudflare.entry import Default

    signing_key = "test-secret-key-32-bytes-minimum!"
    now = int(time.time())
    token = jwt.encode({"sub": "u1", "exp": now + 600}, signing_key, algorithm="HS256")

    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=signing_key,
        WORKER_BEARER_TOKEN="worker-token",
        SESSION_RUNTIME=None,
        SESSION_REGISTRY=None,
    )
    worker = object.__new__(Default)
    worker.env = env
    worker._config = CloudflareConfig.from_env(env)
    req = _Req(
        "https://example.invalid/app",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = await worker.fetch(req)
    # Valid token → auth passes → 200 or 404 (asset may not exist), but NOT 401
    assert resp.status != 401


@pytest.mark.asyncio
async def test_root_page_requires_jwt_in_jwt_mode() -> None:
    """In JWT mode, / must return 401 when no token is provided."""
    from provide.terminal.cloudflare.entry import Default

    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM="test-secret-key-32-bytes-minimum!",
        WORKER_BEARER_TOKEN="worker-token",
        SESSION_RUNTIME=None,
        SESSION_REGISTRY=None,
    )
    worker = object.__new__(Default)
    worker.env = env
    worker._config = CloudflareConfig.from_env(env)
    req = _Req("https://example.invalid/")
    resp = await worker.fetch(req)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_assets_accessible_without_jwt() -> None:
    """Static assets (/assets/*.js) must be accessible even in JWT mode."""
    from provide.terminal.cloudflare.entry import Default

    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM="test-secret-key-32-bytes-minimum!",
        WORKER_BEARER_TOKEN="worker-token",
        SESSION_RUNTIME=None,
        SESSION_REGISTRY=None,
    )
    worker = object.__new__(Default)
    worker.env = env
    worker._config = CloudflareConfig.from_env(env)
    req = _Req("https://example.invalid/assets/hijack.js")
    resp = await worker.fetch(req)
    # Should NOT be 401 — assets are public (may be 200 or 404 depending on file presence)
    assert resp.status != 401


@pytest.mark.asyncio
async def test_worker_ws_accepted_in_dev_mode_without_token() -> None:
    """When no worker_bearer_token is configured, worker WS falls through to normal auth."""
    runtime = _make_runtime_with_token(token=None, mode="dev")
    req = _Req(
        "https://example.invalid/ws/worker/test-worker/term",
        headers={"Upgrade": "websocket"},
    )
    # In dev mode with no worker_bearer_token, falls through to _resolve_principal
    # which returns (None, None) in dev mode, then hits the WS upgrade path.
    try:
        resp = await runtime.fetch(req)
        assert resp.status != 403
    except ImportError:
        # ImportError from js.WebSocketPair is expected in test env — means auth passed
        pass
