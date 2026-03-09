"""API contract tests — enforce parity between CF and FastAPI backends.

These tests are the *alignment mechanism*.  Any field added to the FastAPI
``SessionRuntimeStatus`` model or a hijack route response must also appear in
the corresponding TypedDict in ``contracts.py`` and be returned by the CF
implementation, or these tests will fail.

How to keep things in sync going forward:
  1. Add the new field to the TypedDict in ``contracts.py``.
  2. Update ``api/http_routes.py`` to include it (with a CF default if needed).
  3. The test here validates both automatically.

FastAPI reference:
  - GET  /api/sessions       → list[SessionRuntimeStatus]
  - POST /worker/{id}/hijack/acquire  → {ok, worker_id, hijack_id, lease_expires_at, owner}
  - POST /worker/{id}/hijack/{hid}/heartbeat → {ok, worker_id, hijack_id, lease_expires_at}
  - POST /worker/{id}/hijack/{hid}/step      → {ok, worker_id, hijack_id, lease_expires_at}
  - POST /worker/{id}/hijack/{hid}/release   → {ok, worker_id, hijack_id}
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import get_type_hints

import jwt
from provide_terminal_cloudflare.api.http_routes import route_http
from provide_terminal_cloudflare.auth.jwt import decode_jwt
from provide_terminal_cloudflare.bridge.hijack import HijackCoordinator
from provide_terminal_cloudflare.config import CloudflareConfig, JwtConfig
from provide_terminal_cloudflare.contracts import (
    HijackAcquireResponse,
    HijackEventsResponse,
    HijackHeartbeatResponse,
    HijackReleaseResponse,
    HijackSendResponse,
    HijackSnapshotResponse,
    HijackStepResponse,
    SessionStatusItem,
)

# ---------------------------------------------------------------------------
# Shared runtime mock
# ---------------------------------------------------------------------------


class _Req:
    def __init__(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
        self.url = url
        self.method = method
        self.headers = headers or {}
        self._body = "{}"

    def with_body(self, data: dict) -> _Req:
        self._body = json.dumps(data)
        return self


class _Runtime:
    def __init__(self, worker_ws: object | None = None) -> None:
        self.worker_id = "test-worker"
        self.worker_ws = worker_ws
        self.hijack = HijackCoordinator()
        self._role = "admin"
        self._persisted: list[object] = []
        self._actions: list[tuple[str, str, int]] = []
        self.last_snapshot: dict | None = None
        self.browser_hijack_owner: dict[str, str] = {}
        self.input_mode: str = "hijack"

    async def request_json(self, request: object) -> dict:
        return json.loads(getattr(request, "_body", "{}"))

    async def browser_role_for_request(self, request: object) -> str:
        return self._role

    def persist_lease(self, session: object) -> None:
        self._persisted.append(session)

    def clear_lease(self) -> None:
        pass

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        self._actions.append((action, owner, lease_s))
        return True

    async def broadcast_hijack_state(self) -> None:
        pass

    async def push_worker_input(self, data: str) -> bool:
        return bool(data)

    @property
    def store(self) -> object:
        return SimpleNamespace(
            list_events_since=lambda *_args, **_kwargs: [],
            load_session=lambda *_args, **_kwargs: None,
            current_event_seq=lambda *_args, **_kwargs: 0,
        )


def _parse(resp: object) -> dict | list:
    return json.loads(getattr(resp, "body", "{}") or "{}")


def _check_keys(actual: dict, typed_dict: type) -> None:
    """Assert *actual* contains every key declared in *typed_dict*."""
    required = set(get_type_hints(typed_dict).keys())
    missing = required - set(actual.keys())
    assert not missing, f"Response missing keys required by {typed_dict.__name__}: {missing}"


# ---------------------------------------------------------------------------
# Contract: GET /api/sessions
# ---------------------------------------------------------------------------


async def test_sessions_returns_list() -> None:
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://example.invalid/api/sessions"))
    assert resp.status == 200
    data = _parse(resp)
    assert isinstance(data, list), "GET /api/sessions must return a JSON array"


async def test_sessions_item_has_all_contract_fields() -> None:
    runtime = _Runtime(worker_ws=object())
    resp = await route_http(runtime, _Req("https://example.invalid/api/sessions"))
    items = _parse(resp)
    assert len(items) == 1
    _check_keys(items[0], SessionStatusItem)


async def test_sessions_lifecycle_state_reflects_connection() -> None:
    connected_runtime = _Runtime(worker_ws=object())
    disconnected_runtime = _Runtime(worker_ws=None)

    connected_resp = json.loads(
        (await route_http(connected_runtime, _Req("https://example.invalid/api/sessions"))).body
    )
    disconnected_resp = json.loads(
        (await route_http(disconnected_runtime, _Req("https://example.invalid/api/sessions"))).body
    )

    assert connected_resp[0]["lifecycle_state"] == "running"
    assert connected_resp[0]["connected"] is True
    assert disconnected_resp[0]["lifecycle_state"] == "idle"
    assert disconnected_resp[0]["connected"] is False


async def test_sessions_scope_header_present() -> None:
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://example.invalid/api/sessions"))
    headers = getattr(resp, "headers", {}) or {}
    assert headers.get("X-Sessions-Scope") == "local"


async def test_sessions_display_name_equals_worker_id() -> None:
    runtime = _Runtime()
    items = json.loads((await route_http(runtime, _Req("https://example.invalid/api/sessions"))).body)
    assert items[0]["display_name"] == runtime.worker_id
    assert items[0]["session_id"] == runtime.worker_id


async def test_sessions_hijacked_reflects_hijack_state() -> None:
    runtime = _Runtime()
    result = runtime.hijack.acquire("alice", 60)
    assert result.ok
    items = json.loads((await route_http(runtime, _Req("https://example.invalid/api/sessions"))).body)
    assert items[0]["hijacked"] is True


# ---------------------------------------------------------------------------
# Contract: POST /worker/{id}/hijack/acquire
# ---------------------------------------------------------------------------


async def test_acquire_response_has_contract_fields() -> None:
    runtime = _Runtime()
    req = _Req("https://example.invalid/worker/test-worker/hijack/acquire", method="POST")
    req._body = json.dumps({"owner": "alice", "lease_s": 60})
    resp = await route_http(runtime, req)
    assert resp.status == 200
    payload = _parse(resp)
    _check_keys(payload, HijackAcquireResponse)
    assert payload["ok"] is True
    assert payload["worker_id"] == "test-worker"
    assert payload["owner"] == "alice"
    assert "hijack_id" in payload
    assert "lease_expires_at" in payload


# ---------------------------------------------------------------------------
# Contract: POST /worker/{id}/hijack/{hid}/heartbeat
# ---------------------------------------------------------------------------


async def test_heartbeat_response_has_contract_fields() -> None:
    runtime = _Runtime()
    acquired = runtime.hijack.acquire("bob", 60)
    assert acquired.ok and acquired.session is not None
    hid = acquired.session.hijack_id

    req = _Req(f"https://example.invalid/worker/test-worker/hijack/{hid}/heartbeat", method="POST")
    req._body = json.dumps({"lease_s": 30})
    resp = await route_http(runtime, req)
    assert resp.status == 200
    payload = _parse(resp)
    _check_keys(payload, HijackHeartbeatResponse)
    assert payload["ok"] is True
    assert payload["worker_id"] == "test-worker"
    assert payload["hijack_id"] == hid


# ---------------------------------------------------------------------------
# Contract: POST /worker/{id}/hijack/{hid}/step
# ---------------------------------------------------------------------------


async def test_step_response_has_contract_fields() -> None:
    runtime = _Runtime()
    acquired = runtime.hijack.acquire("carol", 60)
    assert acquired.ok and acquired.session is not None
    hid = acquired.session.hijack_id

    req = _Req(f"https://example.invalid/worker/test-worker/hijack/{hid}/step", method="POST")
    req._body = "{}"
    resp = await route_http(runtime, req)
    assert resp.status == 200
    payload = _parse(resp)
    _check_keys(payload, HijackStepResponse)
    assert payload["ok"] is True
    assert payload["worker_id"] == "test-worker"
    assert payload["hijack_id"] == hid


# ---------------------------------------------------------------------------
# Contract: POST /worker/{id}/hijack/{hid}/release
# ---------------------------------------------------------------------------


async def test_release_response_has_contract_fields() -> None:
    runtime = _Runtime()
    acquired = runtime.hijack.acquire("dave", 60)
    assert acquired.ok and acquired.session is not None
    hid = acquired.session.hijack_id

    req = _Req(f"https://example.invalid/worker/test-worker/hijack/{hid}/release", method="POST")
    req._body = "{}"
    resp = await route_http(runtime, req)
    assert resp.status == 200
    payload = _parse(resp)
    _check_keys(payload, HijackReleaseResponse)
    assert payload["ok"] is True
    assert payload["worker_id"] == "test-worker"
    assert payload["hijack_id"] == hid


# ---------------------------------------------------------------------------
# Contract: GET /worker/{id}/hijack/{hid}/snapshot
# ---------------------------------------------------------------------------


async def test_snapshot_returns_none_when_no_snapshot() -> None:
    runtime = _Runtime()
    acquired = runtime.hijack.acquire("eve", 60)
    assert acquired.ok and acquired.session is not None
    hid = acquired.session.hijack_id

    req = _Req(f"https://example.invalid/worker/test-worker/hijack/{hid}/snapshot")
    resp = await route_http(runtime, req)
    assert resp.status == 200
    payload = _parse(resp)
    _check_keys(payload, HijackSnapshotResponse)
    assert payload["ok"] is True
    assert payload["snapshot"] is None


async def test_snapshot_returns_in_memory_snapshot() -> None:
    runtime = _Runtime()
    runtime.last_snapshot = {"type": "snapshot", "screen": "hello"}
    acquired = runtime.hijack.acquire("frank", 60)
    assert acquired.ok and acquired.session is not None
    hid = acquired.session.hijack_id

    req = _Req(f"https://example.invalid/worker/test-worker/hijack/{hid}/snapshot")
    resp = await route_http(runtime, req)
    payload = _parse(resp)
    assert payload["snapshot"] == {"type": "snapshot", "screen": "hello"}
    assert "prompt_id" in payload
    assert "lease_expires_at" in payload


# ---------------------------------------------------------------------------
# Contract: POST /worker/{id}/hijack/{hid}/send
# ---------------------------------------------------------------------------


async def test_send_response_has_contract_fields() -> None:
    runtime = _Runtime(worker_ws=object())
    acquired = runtime.hijack.acquire("grace", 60)
    assert acquired.ok and acquired.session is not None
    hid = acquired.session.hijack_id

    req = _Req(f"https://example.invalid/worker/test-worker/hijack/{hid}/send", method="POST")
    req._body = json.dumps({"keys": "hello"})
    resp = await route_http(runtime, req)
    assert resp.status == 200
    payload = _parse(resp)
    _check_keys(payload, HijackSendResponse)
    assert payload["ok"] is True
    assert payload["sent"] == "hello"
    assert payload["worker_id"] == "test-worker"


# ---------------------------------------------------------------------------
# Contract: GET /worker/{id}/hijack/{hid}/events
# ---------------------------------------------------------------------------


async def test_events_response_has_contract_fields() -> None:
    runtime = _Runtime()
    acquired = runtime.hijack.acquire("harry", 60)
    assert acquired.ok and acquired.session is not None
    hid = acquired.session.hijack_id

    req = _Req(f"https://example.invalid/worker/test-worker/hijack/{hid}/events")
    resp = await route_http(runtime, req)
    assert resp.status == 200
    payload = _parse(resp)
    _check_keys(payload, HijackEventsResponse)
    assert payload["ok"] is True
    assert isinstance(payload["events"], list)


# ---------------------------------------------------------------------------
# Contract: POST /worker/{id}/input_mode
# ---------------------------------------------------------------------------


async def test_input_mode_switches_to_open() -> None:
    runtime = _Runtime()
    req = _Req("https://example.invalid/worker/test-worker/input_mode", method="POST")
    req._body = json.dumps({"input_mode": "open"})
    resp = await route_http(runtime, req)
    assert resp.status == 200
    payload = _parse(resp)
    assert payload["ok"] is True
    assert payload["input_mode"] == "open"
    assert runtime.input_mode == "open"


async def test_input_mode_rejects_open_while_hijack_active() -> None:
    runtime = _Runtime()
    runtime.hijack.acquire("ivan", 60)
    req = _Req("https://example.invalid/worker/test-worker/input_mode", method="POST")
    req._body = json.dumps({"input_mode": "open"})
    resp = await route_http(runtime, req)
    assert resp.status == 409


async def test_input_mode_rejects_invalid_value() -> None:
    runtime = _Runtime()
    req = _Req("https://example.invalid/worker/test-worker/input_mode", method="POST")
    req._body = json.dumps({"input_mode": "invalid"})
    resp = await route_http(runtime, req)
    assert resp.status == 400


# ---------------------------------------------------------------------------
# Contract: POST /worker/{id}/disconnect_worker
# ---------------------------------------------------------------------------


async def test_disconnect_worker_returns_404_when_no_worker() -> None:
    runtime = _Runtime(worker_ws=None)
    req = _Req("https://example.invalid/worker/test-worker/disconnect_worker", method="POST")
    resp = await route_http(runtime, req)
    assert resp.status == 404


async def test_disconnect_worker_returns_ok_when_worker_connected() -> None:
    runtime = _Runtime(worker_ws=object())
    req = _Req("https://example.invalid/worker/test-worker/disconnect_worker", method="POST")
    resp = await route_http(runtime, req)
    assert resp.status == 200
    payload = _parse(resp)
    assert payload["ok"] is True
    assert payload["worker_id"] == "test-worker"


# ---------------------------------------------------------------------------
# Contract: JWT roles_claim parity with FastAPI AuthConfig
# ---------------------------------------------------------------------------


async def test_jwt_roles_claim_custom_key() -> None:
    """Custom roles claim key matches FastAPI jwt_roles_claim behaviour."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "my_roles": ["admin"], "iat": now, "nbf": now, "exp": now + 600},
        "secret",
        algorithm="HS256",
    )
    cfg = JwtConfig(mode="jwt", public_key_pem="secret", algorithms=("HS256",), jwt_roles_claim="my_roles")
    principal = await decode_jwt(token, cfg)
    assert "admin" in principal.roles


async def test_jwt_roles_claim_default_is_roles() -> None:
    """Default claim key is 'roles', matching FastAPI default."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "roles": ["operator"], "iat": now, "nbf": now, "exp": now + 600},
        "secret",
        algorithm="HS256",
    )
    cfg = JwtConfig(mode="jwt", public_key_pem="secret", algorithms=("HS256",))
    principal = await decode_jwt(token, cfg)
    assert "operator" in principal.roles


async def test_jwt_scopes_claim_fallback() -> None:
    """Space-separated scopes used as role fallback when roles claim absent."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "scope": "read:sessions role:admin", "iat": now, "nbf": now, "exp": now + 600},
        "secret",
        algorithm="HS256",
    )
    cfg = JwtConfig(mode="jwt", public_key_pem="secret", algorithms=("HS256",))
    principal = await decode_jwt(token, cfg)
    assert "role:admin" in principal.roles or "read:sessions" in principal.roles


async def test_jwt_scopes_claim_custom_key() -> None:
    """Custom scopes claim key matches FastAPI jwt_scopes_claim behaviour."""
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u1", "permissions": "admin write", "iat": now, "nbf": now, "exp": now + 600},
        "secret",
        algorithm="HS256",
    )
    cfg = JwtConfig(
        mode="jwt",
        public_key_pem="secret",
        algorithms=("HS256",),
        jwt_scopes_claim="permissions",
    )
    principal = await decode_jwt(token, cfg)
    assert "admin" in principal.roles


async def test_jwt_roles_claim_takes_priority_over_scopes() -> None:
    """Explicit roles claim takes priority over scope fallback."""
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "u1",
            "roles": ["viewer"],
            "scope": "admin superuser",
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        },
        "secret",
        algorithm="HS256",
    )
    cfg = JwtConfig(mode="jwt", public_key_pem="secret", algorithms=("HS256",))
    principal = await decode_jwt(token, cfg)
    assert principal.roles == ("viewer",)


# ---------------------------------------------------------------------------
# Contract: config.from_env reads JWT_ROLES_CLAIM / JWT_SCOPES_CLAIM
# ---------------------------------------------------------------------------


def test_config_from_env_reads_jwt_roles_claim() -> None:
    cfg = CloudflareConfig.from_env({"AUTH_MODE": "none", "JWT_ROLES_CLAIM": "https://myapp.com/roles"})
    assert cfg.jwt.jwt_roles_claim == "https://myapp.com/roles"


def test_config_from_env_reads_jwt_scopes_claim() -> None:
    cfg = CloudflareConfig.from_env({"AUTH_MODE": "none", "JWT_SCOPES_CLAIM": "permissions"})
    assert cfg.jwt.jwt_scopes_claim == "permissions"


def test_config_from_env_jwt_claims_default_values() -> None:
    cfg = CloudflareConfig.from_env({"AUTH_MODE": "none"})
    assert cfg.jwt.jwt_roles_claim == "roles"
    assert cfg.jwt.jwt_scopes_claim == "scope"


# ---------------------------------------------------------------------------
# Contract: assets — local static/ files must not shadow provide.terminal
# ---------------------------------------------------------------------------


def test_no_local_static_overrides() -> None:
    """ui/static/ must be empty so provide.terminal is the single source of truth.

    If this test fails, a duplicate asset file was added to
    src/provide_terminal_cloudflare/ui/static/.  Delete it and rely on the
    provide.terminal package fallback in assets.py instead.
    """
    import importlib.resources

    try:
        static_root = importlib.resources.files("provide_terminal_cloudflare.ui") / "static"
        static_files = [p for p in static_root.iterdir() if p.is_file()]  # type: ignore[union-attr]
    except (ModuleNotFoundError, TypeError, NotImplementedError, FileNotFoundError):
        static_files = []

    assert static_files == [], (
        f"Found local static overrides that shadow provide.terminal: {[str(f) for f in static_files]}. "
        "Delete them and use the provide.terminal package as the single source of truth."
    )
