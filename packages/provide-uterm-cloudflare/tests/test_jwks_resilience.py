#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""JWKS fetch resilience: stale-on-error fallback + negative-retry backoff.

A transient or flapping JWKS endpoint must not take down all authentication:
when a refresh fails but a previously-fetched copy is still cached, the stale
copy is served and further refresh attempts are suppressed for a short window
so the down endpoint is not hammered on every request.

The cache is per-isolate module state, so each test pops its url from both
``_JWKS_CACHE`` and ``_JWKS_RETRY_AFTER`` to stay isolated.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import provide.uterm.cloudflare.auth.jwt as jwt_module
import pytest
from provide.uterm.cloudflare.auth.jwt import _JWKS_CACHE_TTL_S, _fetch_jwks


def _seed_expired(url: str, data: dict) -> None:
    """Plant an already-expired cache entry and clear any retry backoff."""
    jwt_module._JWKS_CACHE[url] = (time.monotonic() - _JWKS_CACHE_TTL_S - 10.0, data)
    jwt_module._JWKS_RETRY_AFTER.pop(url, None)


def _cleanup(url: str) -> None:
    jwt_module._JWKS_CACHE.pop(url, None)
    jwt_module._JWKS_RETRY_AFTER.pop(url, None)


def _ok_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


async def test_serves_stale_jwks_when_refresh_fails() -> None:
    """An expired cache + a failing refresh serves the stale copy (not an error)."""
    url = "https://idp.example.test/stale/jwks.json"
    stale = {"keys": [{"kid": "old"}]}
    _seed_expired(url, stale)
    try:
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            result = await _fetch_jwks(url)
        assert result == stale
        assert url in jwt_module._JWKS_RETRY_AFTER  # backoff window armed
    finally:
        _cleanup(url)


async def test_negative_retry_window_suppresses_refetch() -> None:
    """Inside the backoff window, an expired cache is served without a network call."""
    url = "https://idp.example.test/backoff/jwks.json"
    stale = {"keys": [{"kid": "old"}]}
    _seed_expired(url, stale)
    jwt_module._JWKS_RETRY_AFTER[url] = time.monotonic() + 100.0  # inside backoff

    def _boom(*_a, **_k):
        raise AssertionError("urlopen must not be called inside the backoff window")

    try:
        with patch("urllib.request.urlopen", side_effect=_boom):
            result = await _fetch_jwks(url)
        assert result == stale
    finally:
        _cleanup(url)


async def test_refresh_failure_without_cache_raises() -> None:
    """With no cached copy to fall back to, a fetch failure must propagate."""
    url = "https://idp.example.test/no-cache/jwks.json"
    _cleanup(url)
    try:
        with patch("urllib.request.urlopen", side_effect=OSError("down")), pytest.raises(OSError, match="down"):
            await _fetch_jwks(url)
        assert url not in jwt_module._JWKS_RETRY_AFTER  # no backoff armed without a cache
    finally:
        _cleanup(url)


async def test_successful_refresh_clears_backoff() -> None:
    """Once the backoff elapses, a successful refresh updates the cache and clears it."""
    url = "https://idp.example.test/recover/jwks.json"
    fresh = {"keys": [{"kid": "new"}]}
    _seed_expired(url, {"keys": [{"kid": "old"}]})
    jwt_module._JWKS_RETRY_AFTER[url] = time.monotonic() - 1.0  # backoff already elapsed
    try:
        with patch("urllib.request.urlopen", return_value=_ok_response(fresh)):
            result = await _fetch_jwks(url)
        assert result == fresh
        assert jwt_module._JWKS_CACHE[url][1] == fresh
        assert url not in jwt_module._JWKS_RETRY_AFTER  # cleared on success
    finally:
        _cleanup(url)
