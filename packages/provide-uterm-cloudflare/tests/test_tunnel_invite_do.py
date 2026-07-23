"""One-time tunnel invites are serialized by their session Durable Object."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

from provide.uterm.tunnel.token_hash import hash_token

_KEY = "test-secret-key-32-bytes-minimum!"
_INTERNAL_HEADER = "X-Provide-Uterm-Internal"
_INTERNAL_VALUE = "worker-invite-redemption-v1"


class _Request:
    def __init__(self, invite: str, *, provenance: bool = True) -> None:
        self.url = "https://worker.invalid/_internal/tunnel-invite/redeem"
        self.method = "POST"
        self.headers = {
            _INTERNAL_HEADER: _INTERNAL_VALUE if provenance else "forged",
            "content-type": "application/json",
        }
        self._body = json.dumps({"invite": invite})

    async def text(self) -> str:
        return self._body


def _runtime(entry: dict[str, object]) -> tuple[SessionRuntime, SimpleNamespace]:
    connection = sqlite3.connect(":memory:")
    ctx = SimpleNamespace(
        storage=SimpleNamespace(sql=SimpleNamespace(exec=connection.execute), setAlarm=lambda _ms: None),
        id=SimpleNamespace(name=lambda: "tunnel-serial"),
        getWebSockets=list,
    )
    kv = SimpleNamespace(get=AsyncMock(return_value=json.dumps(entry)), put=AsyncMock())
    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=_KEY,
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
        SESSION_REGISTRY=kv,
    )
    return SessionRuntime(ctx, env), kv


def _entry() -> dict[str, object]:
    return {
        "share_token_hash": hash_token("share-token"),
        "share_invite_hash": hash_token("one-time-invite"),
        "share_invite_token": "share-token",
        "share_invite_expires_at": time.time() + 300,
        "expires_at": time.time() + 3600,
        "share_page": "session",
    }


async def test_two_concurrent_invite_redemptions_have_exactly_one_winner() -> None:
    runtime, kv = _runtime(_entry())

    first, second = await asyncio.gather(
        runtime.fetch(_Request("one-time-invite")),
        runtime.fetch(_Request("one-time-invite")),
    )

    responses = [json.loads(response.body) for response in (first, second)]
    assert sorted(response.status for response in (first, second)) == [200, 404]
    assert sum(response.get("token") == "share-token" for response in responses) == 1
    assert kv.put.await_count == 0
    durable_state = runtime.store.load_tunnel_invite_state("tunnel-serial")
    assert durable_state is not None
    assert "share_invite_hash" not in durable_state
    assert "share_invite_token" not in durable_state


async def test_invite_redemption_rejects_non_worker_provenance() -> None:
    runtime, kv = _runtime(_entry())

    response = await runtime.fetch(_Request("one-time-invite", provenance=False))

    assert response.status == 404
    assert kv.get.await_count == 0


async def test_redemption_does_not_restore_kv_after_concurrent_revoke() -> None:
    runtime, kv = _runtime(_entry())
    current = _entry()

    async def _get_then_revoke(_key: str) -> str:
        # Model a revoke occurring after redemption reads KV but before it
        # persists its local consumed-invite digest.
        snapshot = json.dumps(current)
        current.clear()
        current.update({"revoked": True, "share_token_hash": None, "control_token_hash": None})
        return snapshot

    kv.get = AsyncMock(side_effect=_get_then_revoke)
    response = await runtime.fetch(_Request("one-time-invite"))

    assert response.status == 200
    assert current["revoked"] is True
    assert kv.put.await_count == 0


async def test_redemption_does_not_restore_kv_after_concurrent_rotation() -> None:
    runtime, kv = _runtime(_entry())
    current = _entry()

    async def _get_then_rotate(_key: str) -> str:
        # The returned value is the old snapshot; ``current`` becomes the newer
        # KV value written by token rotation while redemption is in flight.
        snapshot = json.dumps(current)
        current.update(
            {
                "share_token_hash": hash_token("rotated-share-token"),
                "control_token_hash": hash_token("rotated-control-token"),
                "share_invite_hash": hash_token("rotated-invite"),
            }
        )
        return snapshot

    kv.get = AsyncMock(side_effect=_get_then_rotate)
    response = await runtime.fetch(_Request("one-time-invite"))

    assert response.status == 200
    assert current["share_token_hash"] == hash_token("rotated-share-token")
    assert current["control_token_hash"] == hash_token("rotated-control-token")
    assert current["share_invite_hash"] == hash_token("rotated-invite")
    assert kv.put.await_count == 0
