#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for entry.py — Default.fetch() dispatch logic."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from provide.uterm.cloudflare.entry import Default
from provide.uterm.tunnel.token_hash import hash_token

# ---------------------------------------------------------------------------
# _extract_worker_id
# ---------------------------------------------------------------------------


def test_read_header_skips_individually_raising_names() -> None:
    """_read_header continues past names whose .get() itself raises.

    Pathological JS-bridged headers proxies can raise on specific keys
    depending on character encoding; the defensive try/continue guards
    that.  Without this test the ``except Exception: continue`` branch
    is uncovered.
    """
    from provide.uterm.cloudflare.entry.auth import _read_header

    class _FlakyHeaders:
        def get(self, k: str, default: object = None) -> object:
            if k == "cf-access-client-id":
                raise RuntimeError("boom")
            return "svc.access" if k == "CF-Access-Client-Id" else default

    req = SimpleNamespace(headers=_FlakyHeaders())
    # First name raises → continue; second name returns a value.
    assert _read_header(req, "cf-access-client-id", "CF-Access-Client-Id") == "svc.access"


async def test_resolve_principal_id_cf_access_service_token() -> None:
    """CF Access service token maps to a ``service:<client_id>`` principal."""
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.auth import _resolve_principal_id

    config = CloudflareConfig.from_env(
        SimpleNamespace(AUTH_MODE="jwt", JWT_ALGORITHMS="HS256", JWT_PUBLIC_KEY_PEM="k", WORKER_BEARER_TOKEN="t")
    )
    client_id = "abc123.access"
    req = SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: client_id if k.lower() == "cf-access-client-id" else d)
    )
    result = await _resolve_principal_id(req, config)
    assert result == f"service:{client_id}"


def _make_default(env_attrs: dict | None = None) -> Default:
    attrs: dict = {"AUTH_MODE": "dev"}
    if env_attrs:
        attrs.update(env_attrs)
    return Default(SimpleNamespace(**attrs))


# ---------------------------------------------------------------------------
# F3: SPA page_kind honors the URL path kind (inspect / replay)
# ---------------------------------------------------------------------------


async def test_inspect_page_with_valid_share_token_gets_inspect_kind() -> None:
    """F3: /app/inspect/{id}?token=... must render page_kind='inspect', not 'session'."""
    import json as _json

    session = {
        "share_token_hash": hash_token("shared-tok"),
        "control_token_hash": hash_token("ctrl-tok"),
        "expires_at": __import__("time").time() + 3600,
    }
    kv = SimpleNamespace(get=AsyncMock(return_value=_json.dumps(session)))
    d = _make_default({"SESSION_REGISTRY": kv})
    req = SimpleNamespace(
        url="https://x/app/inspect/tun-abc?token=shared-tok",
        headers=SimpleNamespace(get=lambda *a, **k: None),
    )
    resp = await d.fetch(req)
    assert resp.status == 200
    bootstrap = _json.loads(resp.body.split("id='app-bootstrap'>")[1].split("</script>")[0])  # type: ignore[union-attr]
    assert bootstrap["page_kind"] == "inspect"


async def test_replay_page_with_valid_share_token_gets_replay_kind() -> None:
    """F3: /app/replay/{id}?token=... must render page_kind='replay'."""
    import json as _json

    session = {
        "share_token_hash": hash_token("replay-tok"),
        "control_token_hash": hash_token("ctrl-tok"),
        "expires_at": __import__("time").time() + 3600,
    }
    kv = SimpleNamespace(get=AsyncMock(return_value=_json.dumps(session)))
    d = _make_default({"SESSION_REGISTRY": kv})
    req = SimpleNamespace(
        url="https://x/app/replay/tun-abc?token=replay-tok",
        headers=SimpleNamespace(get=lambda *a, **k: None),
    )
    resp = await d.fetch(req)
    assert resp.status == 200
    bootstrap = _json.loads(resp.body.split("id='app-bootstrap'>")[1].split("</script>")[0])  # type: ignore[union-attr]
    assert bootstrap["page_kind"] == "replay"
