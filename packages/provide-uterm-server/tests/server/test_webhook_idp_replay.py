#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""L9: replay protection for the webhook-IdP signed response.

Three layers, all proven here:

  1. Always-on bounded replay cache — a repeated ``(signature, timestamp)``
     seen again within the signature-freshness window is rejected.
  2. Optional nonce binding — a per-request ``nonce`` is sent (header +
     signed payload); if the IdP echoes it, the provider verifies the echo
     matches, cryptographically binding the response to this request.
  3. Enforce flag — ``require_response_nonce=True`` rejects any response that
     does not echo the matching nonce (HA / strict request-binding).
"""

from __future__ import annotations

import json

import httpx2
import pytest

from provide.uterm.server.auth import WebhookIdentityProvider, _BoundedReplayCache
from provide.uterm.server.webhook_signing import build_webhook_signature
from tests.helpers import http_mock

_SECRET = "uterm-test-secret-32-byte-minimum-key"  # pragma: allowlist secret
_URL = "https://auth.example.com/resolve"


class _Conn:
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}


def _signed_response(
    *,
    now: float,
    body_obj: dict | None = None,
    nonce: str | None = None,
) -> httpx2.Response:
    """Build a signed IdP response. If ``nonce`` is set, echo it in the body."""
    data: dict = {"subject_id": "user-1", "roles": ["viewer"]}
    if body_obj is not None:
        data = dict(body_obj)
    if nonce is not None:
        data["nonce"] = nonce
    body = json.dumps(data, separators=(",", ":")).encode()
    ts = str(now)
    sig = build_webhook_signature(_SECRET, body, ts)
    return httpx2.Response(
        200,
        content=body,
        headers={"X-Uterm-Signature": sig, "X-Uterm-Timestamp": ts},
    )


# ---------------------------------------------------------------------------
# Layer 1: bounded replay cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@http_mock.mock
async def test_replayed_response_within_window_rejected(monkeypatch) -> None:
    """RED: a captured signed response replayed verbatim within the freshness
    window must be rejected the second time (lands in on_failure → None)."""
    import provide.uterm.server.auth as auth_mod

    frozen = 1_000_000.0
    monkeypatch.setattr(auth_mod.time, "time", lambda: frozen)

    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True)

    resp = _signed_response(now=frozen)
    http_mock.post(_URL).mock(return_value=resp)

    # First delivery is accepted.
    first = await idp.resolve_principal(_Conn())
    assert first is not None
    assert first.subject_id == "user-1"

    # Identical (signature, timestamp) replayed within the window → rejected.
    replayed = await idp.resolve_principal(_Conn())
    assert replayed is None


@pytest.mark.asyncio
@http_mock.mock
async def test_distinct_fresh_signatures_not_blocked(monkeypatch) -> None:
    """A legitimate IdP signs each response with a fresh timestamp → distinct
    signature → no false replay hit."""
    import provide.uterm.server.auth as auth_mod

    frozen = {"t": 1_000_000.0}
    monkeypatch.setattr(auth_mod.time, "time", lambda: frozen["t"])

    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True)

    route = http_mock.post(_URL)
    route.mock(return_value=_signed_response(now=frozen["t"]))
    first = await idp.resolve_principal(_Conn())
    assert first is not None

    # Advance the clock a little; the IdP re-signs with the new timestamp.
    frozen["t"] += 1.0
    route.mock(return_value=_signed_response(now=frozen["t"]))
    second = await idp.resolve_principal(_Conn())
    assert second is not None


def test_replay_cache_evicts_when_entry_ages_out() -> None:
    """The cache no longer blocks a signature once its recorded timestamp is
    older than the freshness window."""
    cache = _BoundedReplayCache(max_age_s=300.0, max_entries=128)
    assert cache.seen_or_record("sig-A", now=1000.0) is False
    # Same signature, within window → seen.
    assert cache.seen_or_record("sig-A", now=1200.0) is True
    # Same signature, window elapsed → no longer blocked (stale entry evicted).
    assert cache.seen_or_record("sig-A", now=1000.0 + 301.0) is False


def test_replay_cache_is_bounded() -> None:
    """Inserting many entries keeps the cache size capped."""
    cache = _BoundedReplayCache(max_age_s=300.0, max_entries=8)
    for i in range(100):
        cache.seen_or_record(f"sig-{i}", now=1000.0 + i)
    assert len(cache) <= 8


def test_replay_cache_eviction_purges_stale_on_insert() -> None:
    """Stale entries are purged on insert so the cache doesn't keep dead keys."""
    cache = _BoundedReplayCache(max_age_s=10.0, max_entries=128)
    cache.seen_or_record("old", now=1000.0)
    # Insert a fresh entry well past the window; the stale "old" key is purged.
    cache.seen_or_record("new", now=1000.0 + 50.0)
    assert "old" not in cache
    assert "new" in cache


# ---------------------------------------------------------------------------
# Layer 2 / 3: nonce binding + enforce flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@http_mock.mock
async def test_request_carries_nonce_header_and_payload(monkeypatch) -> None:
    """RED: the outgoing request carries X-Uterm-Nonce AND payload['nonce']
    (so the request signature covers it), and they match."""
    import provide.uterm.server.auth as auth_mod

    frozen = 1_000_000.0
    monkeypatch.setattr(auth_mod.time, "time", lambda: frozen)

    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True)
    route = http_mock.post(_URL).mock(return_value=_signed_response(now=frozen))

    await idp.resolve_principal(_Conn())

    req = route.calls.last.request
    header_nonce = req.headers.get("X-Uterm-Nonce")
    assert header_nonce
    payload = json.loads(req.content)
    assert payload["nonce"] == header_nonce


@pytest.mark.asyncio
@http_mock.mock
async def test_nonce_echo_matches_accepted(monkeypatch) -> None:
    """A response that echoes the sent nonce (matching) is accepted."""
    import provide.uterm.server.auth as auth_mod

    frozen = 1_000_000.0
    monkeypatch.setattr(auth_mod.time, "time", lambda: frozen)

    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True)

    sent: dict[str, str] = {}

    def _responder(request: httpx2.Request) -> httpx2.Response:
        nonce = request.headers["X-Uterm-Nonce"]
        sent["nonce"] = nonce
        return _signed_response(now=frozen, nonce=nonce)

    http_mock.post(_URL).mock(side_effect=_responder)

    principal = await idp.resolve_principal(_Conn())
    assert principal is not None
    assert principal.subject_id == "user-1"


@pytest.mark.asyncio
@http_mock.mock
async def test_nonce_echo_mismatch_rejected(monkeypatch) -> None:
    """A present-but-WRONG echoed nonce is an attack → rejected even when the
    enforce flag is off."""
    import provide.uterm.server.auth as auth_mod

    frozen = 1_000_000.0
    monkeypatch.setattr(auth_mod.time, "time", lambda: frozen)

    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True)

    def _responder(request: httpx2.Request) -> httpx2.Response:
        return _signed_response(now=frozen, nonce="not-the-sent-nonce")

    http_mock.post(_URL).mock(side_effect=_responder)

    principal = await idp.resolve_principal(_Conn())
    assert principal is None


@pytest.mark.asyncio
@http_mock.mock
async def test_nonce_absent_accepted_when_not_required(monkeypatch) -> None:
    """When require_response_nonce=False (default), a response that omits the
    nonce still works (replay cache is the defense)."""
    import provide.uterm.server.auth as auth_mod

    frozen = 1_000_000.0
    monkeypatch.setattr(auth_mod.time, "time", lambda: frozen)

    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True)
    http_mock.post(_URL).mock(return_value=_signed_response(now=frozen))

    principal = await idp.resolve_principal(_Conn())
    assert principal is not None


@pytest.mark.asyncio
@http_mock.mock
async def test_nonce_required_missing_echo_rejected(monkeypatch) -> None:
    """require_response_nonce=True: a response that does NOT echo the nonce is
    rejected."""
    import provide.uterm.server.auth as auth_mod

    frozen = 1_000_000.0
    monkeypatch.setattr(auth_mod.time, "time", lambda: frozen)

    idp = WebhookIdentityProvider(
        url=_URL,
        secret=_SECRET,
        require_signed_response=True,
        require_response_nonce=True,
    )
    http_mock.post(_URL).mock(return_value=_signed_response(now=frozen))

    principal = await idp.resolve_principal(_Conn())
    assert principal is None


@pytest.mark.asyncio
@http_mock.mock
async def test_nonce_required_correct_echo_accepted(monkeypatch) -> None:
    """require_response_nonce=True: a correct echo is accepted."""
    import provide.uterm.server.auth as auth_mod

    frozen = 1_000_000.0
    monkeypatch.setattr(auth_mod.time, "time", lambda: frozen)

    idp = WebhookIdentityProvider(
        url=_URL,
        secret=_SECRET,
        require_signed_response=True,
        require_response_nonce=True,
    )

    def _responder(request: httpx2.Request) -> httpx2.Response:
        return _signed_response(now=frozen, nonce=request.headers["X-Uterm-Nonce"])

    http_mock.post(_URL).mock(side_effect=_responder)

    principal = await idp.resolve_principal(_Conn())
    assert principal is not None
    assert principal.subject_id == "user-1"


@pytest.mark.asyncio
@http_mock.mock
async def test_nonce_required_wrong_echo_rejected(monkeypatch) -> None:
    """require_response_nonce=True: a present-but-wrong echo is rejected."""
    import provide.uterm.server.auth as auth_mod

    frozen = 1_000_000.0
    monkeypatch.setattr(auth_mod.time, "time", lambda: frozen)

    idp = WebhookIdentityProvider(
        url=_URL,
        secret=_SECRET,
        require_signed_response=True,
        require_response_nonce=True,
    )
    http_mock.post(_URL).mock(return_value=_signed_response(now=frozen, nonce="wrong"))

    principal = await idp.resolve_principal(_Conn())
    assert principal is None
