from __future__ import annotations

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from provide_terminal_cloudflare.api.http_routes import route_http
from provide_terminal_cloudflare.auth.jwt import JwtValidationError, decode_jwt
from provide_terminal_cloudflare.bridge.hijack import HijackCoordinator
from provide_terminal_cloudflare.config import CloudflareConfig, JwtConfig
from provide_terminal_cloudflare.do.session_runtime import SessionRuntime


class _Req:
    def __init__(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        self.url = url
        self.method = method
        self.headers = headers or {}


def test_auth_mode_defaults_to_jwt() -> None:
    cfg = CloudflareConfig.from_env({})
    assert cfg.jwt.mode == "jwt"


def test_production_rejects_dev_mode() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE"):
        CloudflareConfig.from_env({"ENVIRONMENT": "production", "AUTH_MODE": "dev"})


def test_query_token_disabled_in_production_by_default() -> None:
    cfg = CloudflareConfig.from_env({"ENVIRONMENT": "production"})
    assert cfg.jwt.allow_query_token is False


def test_session_runtime_extract_token_respects_query_policy() -> None:
    runtime = object.__new__(SessionRuntime)
    runtime.config = SimpleNamespace(jwt=SimpleNamespace(allow_query_token=False))
    request = _Req("https://example.invalid/ws/browser/bot1/term?token=abc123")
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
