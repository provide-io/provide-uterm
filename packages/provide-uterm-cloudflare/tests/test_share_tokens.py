#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for ``entry/share_tokens.py`` cookie extraction."""

from __future__ import annotations

from types import SimpleNamespace

from provide.uterm.cloudflare.entry.fallback_stubs import Response
from provide.uterm.cloudflare.entry.share_tokens import (
    _attach_share_token_cookie,
    _share_token_cookie_header,
)


def _req(url: str, headers: dict[str, str] | None = None) -> object:
    return SimpleNamespace(url=url, headers=headers or {})


# ---------------------------------------------------------------------------
# Explicit invite-consumed token path
# ---------------------------------------------------------------------------


def test_explicit_token_returns_cookie_header() -> None:
    """The one-time invite flow passes the consumed token explicitly."""
    cookie = _share_token_cookie_header(_req("https://x/app/share/t1"), "t1", "abc")
    assert cookie is not None
    assert "uterm_tunnel_t1=abc" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie  # https → secure


def test_query_token_is_ignored() -> None:
    """Direct bearer tokens in URLs must not be promoted into cookies."""
    assert _share_token_cookie_header(_req("https://x/app/share/t1?token=abc"), "t1") is None
    assert _share_token_cookie_header(_req("https://x/app/share/t1?access_token=xyz"), "t1") is None


def test_http_url_omits_secure_attribute() -> None:
    """Plain ``http://`` URLs must not carry the Secure cookie attribute."""
    cookie = _share_token_cookie_header(_req("http://x/app/share/t1"), "t1", "abc")
    assert cookie is not None
    assert "Secure" not in cookie


# ---------------------------------------------------------------------------
# Cookie-only fallback path (lines 27-37) — the previously uncovered branch
# ---------------------------------------------------------------------------


def test_cookie_lowercase_header_is_used_when_query_absent() -> None:
    """Lines 27-34: share-token refresh reads only the ``cookie`` header."""
    headers = {"cookie": "uterm_tunnel_t9=cookie-token"}
    cookie = _share_token_cookie_header(_req("https://x/app/share/t9", headers), "t9")
    assert cookie is not None
    assert "uterm_tunnel_t9=cookie-token" in cookie


def test_cookie_titlecase_header_is_used_when_query_absent() -> None:
    """Line 29: HTTP/1.1 ``Cookie`` (title-case) header form is also accepted."""
    headers = {"Cookie": "uterm_tunnel_t9=tc-token"}
    cookie = _share_token_cookie_header(_req("https://x/app/share/t9", headers), "t9")
    assert cookie is not None
    assert "uterm_tunnel_t9=tc-token" in cookie


def test_no_token_anywhere_returns_none() -> None:
    """Lines 38-39: with neither query nor cookie, return ``None``."""
    cookie = _share_token_cookie_header(_req("https://x/app/share/t9"), "t9")
    assert cookie is None


def test_cookie_for_different_tunnel_id_is_ignored() -> None:
    """Cookie keyed for a different tunnel must not satisfy the lookup."""
    headers = {"cookie": "uterm_tunnel_other=abc"}
    cookie = _share_token_cookie_header(_req("https://x/app/share/t9", headers), "t9")
    assert cookie is None


def test_malformed_cookie_header_swallowed_returns_none() -> None:
    """Lines 35-37: a malformed ``cookie`` header is logged and yields ``None``."""

    class _BadHeaders:
        def get(self, _key: str, _default: object = None) -> object:
            raise RuntimeError("headers exploded")

    req = SimpleNamespace(url="https://x/app/share/t9", headers=_BadHeaders())
    assert _share_token_cookie_header(req, "t9") is None


def test_secure_parse_failure_still_returns_cookie() -> None:
    """A URL parse failure while choosing Secure must not drop the cookie."""
    from provide.uterm.cloudflare.entry import share_tokens as st

    real = st.urlparse

    def _raising_urlparse(_url: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("urlparse boom")

    st.urlparse = _raising_urlparse  # type: ignore[assignment]
    try:
        headers = {"cookie": "uterm_tunnel_t9=fallback"}
        cookie = st._share_token_cookie_header(_req("https://x/app/share/t9", headers), "t9")
    finally:
        st.urlparse = real  # type: ignore[assignment]

    assert cookie is not None
    assert "uterm_tunnel_t9=fallback" in cookie


# ---------------------------------------------------------------------------
# _attach_share_token_cookie wrapper
# ---------------------------------------------------------------------------


def test_attach_cookie_sets_header_when_token_present() -> None:
    resp = Response(body="ok", status=200, headers={"X-Foo": "1"})
    out = _attach_share_token_cookie(resp, _req("https://x/", {"cookie": "uterm_tunnel_t1=t"}), "t1")
    assert out.headers is not None
    assert out.headers.get("Set-Cookie", "").startswith("uterm_tunnel_t1=t")
    assert out.headers.get("X-Foo") == "1"


def test_attach_cookie_noop_when_no_token() -> None:
    resp = Response(body="ok", status=200, headers={"X-Foo": "1"})
    out = _attach_share_token_cookie(resp, _req("https://x/"), "t1")
    assert "Set-Cookie" not in (out.headers or {})


def test_attach_cookie_handles_missing_headers_attr() -> None:
    """Response without a pre-existing headers dict still gets the cookie set."""
    resp = Response(body="ok", status=200)
    out = _attach_share_token_cookie(resp, _req("https://x/", {"cookie": "uterm_tunnel_t1=z"}), "t1")
    assert out.headers is not None
    assert "uterm_tunnel_t1=z" in out.headers["Set-Cookie"]
