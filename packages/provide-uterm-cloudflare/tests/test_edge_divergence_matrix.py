#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Edge-only regression tests for ``docs/cloudflare-divergence-matrix.md``.

Every test in this module pins exactly one row of that table.  If a shared
protocol change alters edge-runtime behavior, one of these tests goes red and
the matrix row must be updated in the same commit.

Nothing here needs ``REAL_CF=1``, a live Worker, or ``pywrangler``: the Durable
Object runtime is driven directly with in-memory SQLite and WebSocket stubs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from provide.uterm.cloudflare.api.http_routes._dispatch import route_http
from provide.uterm.cloudflare.config import CloudflareConfig
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime
from provide.uterm.cloudflare.entry.registry import _extract_worker_id

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder

_KEY = "test-secret-key-32-bytes-minimum!"
# 32 random-looking characters because the Worker's entropy floor is one of the
# divergences under test here: a shorter or placeholder-shaped literal is
# rejected by config, which is exactly what row 2 asserts.
_VALID_BEARER = "0PqL7nJ2vXsD4hB6yTgWmA9cRfKzE1uY"  # pragma: allowlist secret

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _PACKAGE_ROOT.parents[1]
_LIFECYCLE_CONTRACT = _REPO_ROOT / "spec" / "session_lifecycle_security_scenarios.json"


# ---------------------------------------------------------------------------
# Shared fixtures/stubs (mirrors test_session_runtime_unit_2.py)
# ---------------------------------------------------------------------------


def _make_ctx(worker_id: str = "divergence-worker") -> SimpleNamespace:
    conn = sqlite3.connect(":memory:")
    return SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,
    )


def _make_env(**extra: object) -> SimpleNamespace:
    env = SimpleNamespace(AUTH_MODE="jwt", **extra)
    env.JWT_ALGORITHMS = "HS256"
    env.JWT_PUBLIC_KEY_PEM = _KEY
    if not hasattr(env, "WORKER_BEARER_TOKEN"):
        env.WORKER_BEARER_TOKEN = "test-worker-token-padded-to-32xyz"
    return env


def _make_runtime(worker_id: str = "divergence-worker", mode: str = "dev") -> SessionRuntime:
    runtime = SessionRuntime(_make_ctx(worker_id), _make_env())
    runtime.config.jwt.mode = mode
    return runtime


class _AsyncWs:
    """Async-send WebSocket stub with a Cloudflare-style attachment."""

    def __init__(self, attachment: object = None) -> None:
        self._attachment = attachment
        self.sent: list[str] = []

    def deserializeAttachment(self) -> object:  # noqa: N802 - Cloudflare WebSocket API
        return self._attachment

    def serializeAttachment(self, attachment: object) -> None:  # noqa: N802 - Cloudflare WebSocket API
        self._attachment = attachment

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _Req:
    def __init__(self, path: str, *, method: str = "GET") -> None:
        self.url = f"https://example.invalid{path}"
        self.method = method
        self.headers = SimpleNamespace(get=lambda _key, default=None: default)


def _bearer_env(token: str, *, environment: str = "development") -> dict[str, str]:
    return {
        "AUTH_MODE": "jwt",
        "ENVIRONMENT": environment,
        "JWT_ALGORITHMS": "HS256",
        "JWT_PUBLIC_KEY_PEM": _KEY,
        "WORKER_BEARER_TOKEN": token,
    }


def _decode_control_frames(messages: list[str]) -> list[dict]:
    decoded: list[dict] = []
    for raw in messages:
        decoder = ControlFrameDecoder()
        events = decoder.feed(raw)
        events.extend(decoder.finish())
        decoded.extend(event.control for event in events if isinstance(event, ControlChunk))
    return decoded


def _body(response: object) -> dict:
    return json.loads(getattr(response, "body", "{}") or "{}")


async def _browser_hello(runtime: SessionRuntime) -> dict:
    ws = _AsyncWs(attachment=f"browser:admin:{runtime.worker_id}")
    await runtime.webSocketOpen(ws)  # type: ignore[arg-type]
    hellos = [frame for frame in _decode_control_frames(ws.sent) if frame.get("type") == "hello"]
    assert len(hellos) == 1, ws.sent
    return hellos[0]


def _cf_source_files() -> list[Path]:
    import provide.uterm.cloudflare as cf_pkg

    root = Path(next(iter(cf_pkg.__path__)))
    return sorted(root.rglob("*.py"))


# ---------------------------------------------------------------------------
# Row: Auth modes — the edge accepts ``jwt`` and nothing else
# ---------------------------------------------------------------------------

# Every mode the FastAPI backend still accepts, plus the two it removed. The
# edge must refuse all of them: a Worker has no loopback bind, so the
# proxy-trust and stub-IdP modes have no safe deployment shape there.
_NON_JWT_AUTH_MODES = ("dev_token", "header", "api_key", "webhook", "dev", "none")


@pytest.mark.parametrize("mode", _NON_JWT_AUTH_MODES)
def test_edge_rejects_every_auth_mode_except_jwt(mode: str) -> None:
    """Matrix row "Auth modes": AUTH_MODE != jwt is a hard config failure."""
    with pytest.raises(ValueError, match="AUTH_MODE must be 'jwt'"):
        CloudflareConfig.from_env({**_bearer_env(_VALID_BEARER), "AUTH_MODE": mode})


def test_edge_accepts_jwt_auth_mode_case_insensitively() -> None:
    """The single accepted mode is normalized, so `` JWT `` still loads."""
    config = CloudflareConfig.from_env({**_bearer_env(_VALID_BEARER), "AUTH_MODE": "  JWT "})
    assert config.jwt.mode == "jwt"


def test_edge_defaults_to_jwt_when_auth_mode_is_unset() -> None:
    """An absent AUTH_MODE must not fall back to an open-access mode."""
    env = _bearer_env(_VALID_BEARER)
    del env["AUTH_MODE"]
    assert CloudflareConfig.from_env(env).jwt.mode == "jwt"


# ---------------------------------------------------------------------------
# Row: Worker bearer-token entropy floor is unconditional at the edge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
def test_edge_bearer_token_floor_applies_in_every_environment(environment: str) -> None:
    """Matrix row "Bearer floor": no ENVIRONMENT relaxes the 32-char floor.

    The FastAPI backend gates the same floor on a *production-like* deployment
    (``require_jwt_in_production`` or a non-loopback bind). A Worker is always
    internet-facing, so the edge applies it unconditionally.
    """
    with pytest.raises(ValueError, match="at least 32 characters"):
        CloudflareConfig.from_env(_bearer_env("short-token", environment=environment))


@pytest.mark.parametrize("environment", ["development", "production"])
def test_edge_bearer_token_placeholder_rejected_in_every_environment(environment: str) -> None:
    """A long-but-placeholder token is refused regardless of ENVIRONMENT."""
    with pytest.raises(ValueError, match="placeholder"):
        CloudflareConfig.from_env(_bearer_env("replace-me-with-a-real-token-value", environment=environment))


# ---------------------------------------------------------------------------
# Row: hello.hijack_control is "ws" (parity — docs/protocol-matrix.md is stale)
# ---------------------------------------------------------------------------


async def test_edge_hello_advertises_ws_hijack_control() -> None:
    """Matrix row "Hijack control": the edge advertises WS hijack, not REST.

    ``docs/protocol-matrix.md`` claims ``hijack_control=rest`` plus a
    ``use_rest_hijack_api`` refusal. Neither exists in the Worker: all three
    hello emitters send ``ws`` and the WS hijack frames are served. This test
    pins the real behavior so the stale doc row cannot be "fixed" by
    regressing the code to match it.
    """
    hello = await _browser_hello(_make_runtime())

    assert hello["hijack_control"] == "ws"
    assert hello["hijack_step_supported"] is True


def test_edge_never_emits_a_rest_hijack_refusal_code() -> None:
    """No Worker source path produces the documented ``use_rest_hijack_api``."""
    offenders = [path for path in _cf_source_files() if "use_rest_hijack_api" in path.read_text(encoding="utf-8")]
    assert offenders == []


# ---------------------------------------------------------------------------
# Row: hello omits mcp_supported / vnc_supported
# ---------------------------------------------------------------------------


async def test_edge_hello_omits_mcp_and_vnc_capability_flags() -> None:
    """Matrix row "hello capability flags": both keys are absent at the edge.

    The FastAPI backend defaults both to ``True``. The Worker hosts neither an
    MCP server nor an RFB relay, so it omits the keys entirely rather than
    advertising ``false`` — clients must treat "absent" as "unsupported".
    """
    from provide.uterm.server.bridge.frames import make_hello_frame

    hello = await _browser_hello(_make_runtime())

    assert "mcp_supported" not in hello
    assert "vnc_supported" not in hello
    # The FastAPI side of the same row, asserted from the shared builder.
    fastapi_hello = make_hello_frame()
    assert fastapi_hello["mcp_supported"] is True
    assert fastapi_hello["vnc_supported"] is True


# ---------------------------------------------------------------------------
# Row: no VNC / RFB browser relay at the edge
# ---------------------------------------------------------------------------


async def test_edge_has_no_vnc_rfb_relay_route() -> None:
    """Matrix row "VNC relay": the FastAPI ``/gui/vnc`` path has no edge peer."""
    runtime = _make_runtime(worker_id="vnc-worker")

    response = await route_http(
        runtime,
        _Req("/worker/vnc-worker/hijack/lease-1/gui/vnc"),
    )

    assert response.status == 404
    assert _body(response)["error"] == "not_found"


def test_edge_ships_no_rfb_implementation() -> None:
    """No Worker module references the RFB/VNC relay surface at all."""
    offenders = [
        path.name
        for path in _cf_source_files()
        if any(marker in path.read_text(encoding="utf-8") for marker in ("gui/vnc", "RFB", "rfb_"))
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# Row: /api/health payload is minimal at the edge
# ---------------------------------------------------------------------------

# Keys the FastAPI backend adds and the Worker cannot compute: a Durable Object
# has no process uptime, no cross-session registry, and no control-plane backend
# choice.
_FASTAPI_ONLY_HEALTH_KEYS = ("version", "uptime_s", "active_sessions", "control_plane_backend", "ready", "status")


async def test_edge_worker_health_payload_is_minimal() -> None:
    """Matrix row "/api/health": the Worker entrypoint returns 3 keys."""
    from provide.uterm.cloudflare.entry.handlers import _route_request

    config = CloudflareConfig()
    config.jwt.mode = "dev"
    response = await _route_request(_Req("/api/health"), SimpleNamespace(), config)

    payload = _body(response)
    assert response.status == 200
    assert set(payload) == {"ok", "service", "environment"}
    for key in _FASTAPI_ONLY_HEALTH_KEYS:
        assert key not in payload


async def test_edge_durable_object_health_payload_is_minimal() -> None:
    """The Durable Object's own /api/health is smaller still — no environment."""
    response = await route_http(_make_runtime(), _Req("/api/health"))

    payload = _body(response)
    assert response.status == 200
    assert set(payload) == {"ok", "service"}
    for key in _FASTAPI_ONLY_HEALTH_KEYS:
        assert key not in payload


# ---------------------------------------------------------------------------
# Row: tunnel transport has no multiplexed WebSocket route at the edge
# ---------------------------------------------------------------------------


def test_edge_tunnel_path_upgrades_as_a_plain_worker_socket() -> None:
    """Matrix row "Tunnel transport": ``/tunnel/{id}`` is a worker term socket.

    FastAPI's ``/tunnel/{worker_id}`` registers a *tunnel* worker
    (``is_tunnel_worker=True``) that speaks the binary multiplexed channel
    framing. The Worker routes the same path to the Durable Object and
    classifies it as an ordinary worker socket, so the mux transport — and the
    fragmentation semantics layered on it — is unserved at the edge.
    """
    assert _extract_worker_id("/tunnel/tunnel-worker") == "tunnel-worker"

    offenders = [path.name for path in _cf_source_files() if "is_tunnel_worker" in path.read_text(encoding="utf-8")]
    assert offenders == []


def test_edge_tunnel_fragmentation_is_declared_unserved_in_the_shared_contract() -> None:
    """The shared lifecycle contract records the same refusal this row states."""
    contract = json.loads(_LIFECYCLE_CONTRACT.read_text(encoding="utf-8"))
    scenarios = {item["id"]: item for item in contract["scenarios"]}

    tunnel = scenarios["fragmented_tunnel_websocket_message"]["backends"]["cloudflare"]
    assert tunnel == {"status": "unserved", "expected": {"error": "no_tunnel_websocket_route"}}
    # The browser/worker transports are *not* divergent — only the tunnel one.
    for served in ("fragmented_browser_websocket_message", "fragmented_worker_websocket_message"):
        assert scenarios[served]["backends"]["cloudflare"]["status"] == "served"


# ---------------------------------------------------------------------------
# Row: lifecycle capability refusals (browser quota + governance)
# ---------------------------------------------------------------------------


def test_edge_lifecycle_refusals_match_the_shared_contract() -> None:
    """The 501 routes and the contract's ``unsupported`` cells agree.

    ``test_worker_route_defs.py`` asserts the HTTP behavior; this pins the
    contract side so the two cannot drift apart silently.
    """
    contract = json.loads(_LIFECYCLE_CONTRACT.read_text(encoding="utf-8"))
    refusals = {
        item["input"]["operation"]: item["backends"]["cloudflare"]
        for item in contract["scenarios"]
        if item["input"]["operation"] in {"browser_quota", "governed_input"}
    }

    assert set(refusals) == {"browser_quota", "governed_input"}
    for operation, cell in refusals.items():
        assert cell["status"] == "unsupported", operation
        assert cell["expected"]["status_code"] == 501, operation
    assert refusals["browser_quota"]["expected"]["error"] == "per_principal_browser_quota_unsupported"
    assert refusals["governed_input"]["expected"]["error"] == "unsupported_governance"
