#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Share/control token issue-time expiry in the DO's verify_token path.

The Default Worker's HTTP share-page route (``resolve_share_context``) already
rejects a share/control cookie once the tunnel's ``expires_at`` elapses, but the
Durable Object's per-request share-role resolution
(``_share_role_for_request``) historically validated the cookie by hash only —
so a cookie kept authorizing WS/fetch terminal traffic forever. These tests pin
the DO-side expiry gate: ``expires_at`` is loaded from KV (issuance/rotation set
it to ``now + ttl_s``, configurable per-tunnel) and enforced at auth time.
"""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

from provide.uterm.tunnel.token_hash import hash_token

_KEY = "test-secret-key-32-bytes-minimum!"


def _make_runtime(worker_id: str = "tunnel-ttl") -> SessionRuntime:
    conn = sqlite3.connect(":memory:")
    ctx = SimpleNamespace(
        storage=SimpleNamespace(sql=SimpleNamespace(exec=conn.execute), setAlarm=lambda ms: None),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,
    )
    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=_KEY,
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
    )
    return SessionRuntime(ctx, env)


def _cookie_request(worker_id: str, token: str) -> SimpleNamespace:
    return SimpleNamespace(
        url=f"https://x/app/session/{worker_id}",
        headers=SimpleNamespace(get=lambda k, d=None: f"uterm_tunnel_{worker_id}={token}" if k == "cookie" else d),
    )


def _make_fake_kv(payload: str | None) -> SimpleNamespace:
    async def _get(_key: str) -> str | None:
        return payload

    return SimpleNamespace(get=_get)


# ---------------------------------------------------------------------------
# Enforcement in _share_role_for_request
# ---------------------------------------------------------------------------


def test_share_token_rejected_after_expiry() -> None:
    """A valid share-token cookie no longer authorizes once expires_at has passed."""
    rt = _make_runtime("tunnel-exp")
    rt._share_token_hash = hash_token("share-tok")
    rt._control_token_hash = None
    rt._session_expires_at = time.time() - 100  # already expired

    assert rt._share_role_for_request(_cookie_request("tunnel-exp", "share-tok")) is None


def test_control_token_rejected_after_expiry() -> None:
    """The operator (control) cookie is gated by the same issue-time expiry."""
    rt = _make_runtime("tunnel-exp")
    rt._share_token_hash = None
    rt._control_token_hash = hash_token("control-tok")
    rt._session_expires_at = time.time() - 1  # expired

    assert rt._share_role_for_request(_cookie_request("tunnel-exp", "control-tok")) is None


def test_share_token_accepted_before_expiry() -> None:
    """A valid share-token cookie still authorizes while expires_at is in the future."""
    rt = _make_runtime("tunnel-live")
    rt._share_token_hash = hash_token("share-tok")
    rt._control_token_hash = None
    rt._session_expires_at = time.time() + 3600  # not yet expired

    assert rt._share_role_for_request(_cookie_request("tunnel-live", "share-tok")) == "viewer"


def test_share_token_accepted_when_no_expiry_set() -> None:
    """When the session has no expires_at, the cookie authorizes (no TTL gate)."""
    rt = _make_runtime("tunnel-noexp")
    rt._share_token_hash = hash_token("share-tok")
    rt._control_token_hash = None
    rt._session_expires_at = None

    assert rt._share_role_for_request(_cookie_request("tunnel-noexp", "share-tok")) == "viewer"


# ---------------------------------------------------------------------------
# Loading expires_at from KV
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_meta_loads_expires_at() -> None:
    """_ensure_meta populates _session_expires_at from a numeric KV expires_at."""
    rt = _make_runtime("w-meta")
    rt._meta_loaded = False
    rt.env.SESSION_REGISTRY = _make_fake_kv(json.dumps({"display_name": "t", "expires_at": 1234.5}))

    await rt._ensure_meta()

    assert rt._session_expires_at == 1234.5


@pytest.mark.asyncio
async def test_ensure_meta_non_numeric_expires_at_is_none() -> None:
    """A missing/non-numeric expires_at leaves _session_expires_at as None."""
    rt = _make_runtime("w-meta2")
    rt._meta_loaded = False
    rt.env.SESSION_REGISTRY = _make_fake_kv(json.dumps({"display_name": "t", "expires_at": "soon"}))

    await rt._ensure_meta()

    assert rt._session_expires_at is None


@pytest.mark.asyncio
async def test_ensure_credentials_loads_expires_at() -> None:
    """_ensure_credentials refreshes _session_expires_at on its TTL (rotation propagation)."""
    rt = _make_runtime("w-cred")
    rt._credentials_loaded_at = None
    rt.env.SESSION_REGISTRY = _make_fake_kv(json.dumps({"expires_at": 9999.0}))

    await rt._ensure_credentials()

    assert rt._session_expires_at == 9999.0


@pytest.mark.asyncio
async def test_ensure_credentials_non_numeric_expires_at_is_none() -> None:
    """A non-numeric expires_at from KV resolves to None in the credential refresh."""
    rt = _make_runtime("w-cred2")
    rt._credentials_loaded_at = None
    rt.env.SESSION_REGISTRY = _make_fake_kv(json.dumps({"expires_at": None}))

    await rt._ensure_credentials()

    assert rt._session_expires_at is None
