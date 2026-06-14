from __future__ import annotations

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from provide.uterm.cloudflare.api.http_routes import route_http
from provide.uterm.cloudflare.auth.jwt import JwtValidationError, decode_jwt
from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator
from provide.uterm.cloudflare.config import CloudflareConfig, JwtConfig
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime


class _Req:
    def __init__(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        self.url = url
        self.method = method
        self.headers = headers or {}


def test_auth_mode_defaults_to_jwt() -> None:
    cfg = CloudflareConfig.from_env({"WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz"})
    assert cfg.jwt.mode == "jwt"


def test_production_rejects_dev_mode() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE"):
        CloudflareConfig.from_env({"ENVIRONMENT": "production", "AUTH_MODE": "dev"})


def test_query_token_env_knob_removed() -> None:
    cfg = CloudflareConfig.from_env(
        {"AUTH_ALLOW_QUERY_TOKEN": "1", "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz"}
    )
    assert not hasattr(cfg.jwt, "allow_query_token")


def test_session_runtime_extract_token_ignores_query_token() -> None:
    runtime = object.__new__(SessionRuntime)
    runtime.config = SimpleNamespace(jwt=SimpleNamespace())
    request = _Req("https://example.invalid/ws/browser/agent1/term?token=abc123")
    assert runtime._extract_token(request) is None


async def test_decode_jwt_requires_sub_and_exp() -> None:
    # Token without exp must be rejected (matches FastAPI behaviour: require=[sub, exp]).
    token = jwt.encode({"sub": "u1", "roles": ["viewer"]}, "uterm-test-secret-32-byte-minimum-key", algorithm="HS256")
    with pytest.raises(JwtValidationError):
        await decode_jwt(
            token, JwtConfig(mode="jwt", public_key_pem="uterm-test-secret-32-byte-minimum-key", algorithms=("HS256",))
        )


async def test_decode_jwt_accepts_token_without_iat_nbf() -> None:
    # iat/nbf no longer required — Auth0/Google/Azure AD tokens may omit them.
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "exp": now + 600, "roles": ["viewer"]}, "uterm-test-secret-32-byte-minimum-key", algorithm="HS256"
    )
    principal = await decode_jwt(
        token, JwtConfig(mode="jwt", public_key_pem="uterm-test-secret-32-byte-minimum-key", algorithms=("HS256",))
    )
    assert principal.subject_id == "u1"


async def test_decode_jwt_rejects_future_nbf_outside_skew() -> None:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "iat": now, "nbf": now + 120, "exp": now + 3600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
    )
    with pytest.raises(JwtValidationError):
        await decode_jwt(
            token,
            JwtConfig(
                mode="jwt",
                public_key_pem="uterm-test-secret-32-byte-minimum-key",
                algorithms=("HS256",),
                clock_skew_seconds=10,
            ),
        )


class _Runtime:
    def __init__(self) -> None:
        self.worker_id = "w1"
        self.meta: dict = {
            "display_name": self.worker_id,
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }
        self.worker_ws = object()
        self.hijack = HijackCoordinator()
        self.persisted: list[float] = []
        self.actions: list[tuple[str, str, int]] = []
        self._role = "admin"
        self._subject: str | None = None
        self.last_snapshot: dict | None = None
        self.browser_hijack_owner: dict[str, str] = {}
        self.lifecycle_state = "stopped"
        self.input_mode: str = "hijack"

    async def request_json(self, request: object) -> dict[str, object]:
        return json.loads(getattr(request, "_body", "{}"))

    async def browser_role_for_request(self, request: object) -> str:
        return self._role

    async def browser_subject_for_request(self, request: object) -> str | None:
        return self._subject

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
    # from_env only accepts jwt mode now; build a valid jwt config, then override
    # the in-memory mode/bearer token for tests exercising the legacy open-access
    # branches (reachable only via direct config construction).
    env_kwargs: dict = {
        "AUTH_MODE": "jwt",
        "JWT_ALGORITHMS": "HS256",
        "JWT_PUBLIC_KEY_PEM": "test-secret-key-32-bytes-minimum!",
        "WORKER_BEARER_TOKEN": token if token is not None else "test-worker-token-padded-to-32xyz",
    }
    rt = SessionRuntime(ctx, SimpleNamespace(**env_kwargs))
    rt.config.jwt.mode = mode
    if token is None:
        rt.config.worker_bearer_token = None
    return rt


def test_config_reads_worker_bearer_token_from_env() -> None:
    cfg = CloudflareConfig.from_env({"WORKER_BEARER_TOKEN": "uterm-cf-worker-bearer-32-byte-min!"})
    assert cfg.worker_bearer_token == "uterm-cf-worker-bearer-32-byte-min!"


# ---------------------------------------------------------------------------
# Worker bearer-token entropy / placeholder floor (CF-local, unconditional)
# ---------------------------------------------------------------------------

_VALID_BEARER = "uterm-cf-worker-bearer-32-byte-min!"  # 35 chars, no placeholder marker


def _bearer_env(token: str) -> dict[str, str]:
    return {
        "AUTH_MODE": "jwt",
        "JWT_ALGORITHMS": "HS256",
        "JWT_PUBLIC_KEY_PEM": "uterm-cf-hs256-shared-secret-32b!",
        "WORKER_BEARER_TOKEN": token,
    }


class TestWorkerBearerTokenFloor:
    """CF Workers are always internet-facing: a weak worker bearer token is an
    edge auth bypass. ``from_env`` must reject short / placeholder tokens
    unconditionally (no loopback concept exists at the CF edge)."""

    def test_short_token_rejected(self) -> None:
        with pytest.raises(ValueError, match="32"):
            CloudflareConfig.from_env(_bearer_env("test-worker-token"))  # 17 chars

    def test_placeholder_token_rejected(self) -> None:
        with pytest.raises(ValueError, match="placeholder"):
            # 32+ chars so length passes; rejected purely on the placeholder marker.
            CloudflareConfig.from_env(_bearer_env("changeme-aaaaaaaaaaaaaaaaaaaaaaaaa"))

    def test_exact_placeholder_word_rejected(self) -> None:
        with pytest.raises(ValueError, match="placeholder"):
            CloudflareConfig.from_env(_bearer_env("token"))

    def test_valid_high_entropy_token_accepted(self) -> None:
        cfg = CloudflareConfig.from_env(_bearer_env(_VALID_BEARER))
        assert cfg.worker_bearer_token == _VALID_BEARER

    def test_helper_rejects_short(self) -> None:
        from provide.uterm.cloudflare.config import _reject_weak_bearer_token

        with pytest.raises(ValueError, match="32"):
            _reject_weak_bearer_token("short")

    def test_helper_rejects_placeholder(self) -> None:
        from provide.uterm.cloudflare.config import _reject_weak_bearer_token

        with pytest.raises(ValueError, match="placeholder"):
            _reject_weak_bearer_token("replace-me-with-a-real-runtime-tok")

    def test_helper_accepts_valid(self) -> None:
        from provide.uterm.cloudflare.config import _reject_weak_bearer_token

        _reject_weak_bearer_token(_VALID_BEARER)  # must not raise


def test_config_rejects_dev_mode_in_all_environments() -> None:
    # dev/none modes are removed; from_env must reject them regardless of ENVIRONMENT.
    with pytest.raises(ValueError, match="AUTH_MODE"):
        CloudflareConfig.from_env({"AUTH_MODE": "dev"})


@pytest.mark.asyncio
async def test_worker_ws_rejected_without_bearer_token() -> None:
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="dev")
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
    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="dev")
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

    runtime = _make_runtime_with_token(token="test-worker-token-padded-to-32xyz", mode="dev")
    req = _Req(
        "https://example.invalid/ws/worker/test-worker/term",
        headers={"Upgrade": "websocket", "Authorization": "Bearer test-worker-token-padded-to-32xyz"},
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
            "WORKER_BEARER_TOKEN": "test-worker-token-padded-to-32xyz",
        }
    )
    assert cfg.worker_bearer_token == "test-worker-token-padded-to-32xyz"
    assert cfg.jwt.mode == "jwt"


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
