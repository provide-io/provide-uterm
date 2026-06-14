"""Share-token cookie helpers for tunnel routes."""

from __future__ import annotations

import logging
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from provide.uterm.cloudflare.entry.fallback_stubs import Response

logger = logging.getLogger(__name__)


def _share_token_cookie_header(request: object, tunnel_id: str, token: str | None = None) -> str | None:
    """Return an HttpOnly cookie header for a share token.

    The only query-string bootstrap accepted by tunnel sharing is the
    one-time ``?invite=`` flow, which passes the consumed token explicitly.
    Direct ``?token=`` and ``?access_token=`` URLs are intentionally ignored.
    """
    if token is None:
        try:
            cookie_header = str(
                getattr(request, "headers", {}).get("cookie") or getattr(request, "headers", {}).get("Cookie") or ""
            )
            cookies = SimpleCookie(cookie_header)
            cookie_key = f"uterm_tunnel_{tunnel_id}"
            if cookie_key in cookies:
                token = cookies[cookie_key].value
        except Exception as exc:
            logger.debug("share_token_cookie_parse_failed: %s", exc)
            token = None
    if token is None:
        return None
    try:
        secure = str(urlparse(str(getattr(request, "url", ""))).scheme).lower() == "https"
    except Exception as exc:
        logger.debug("share_token_cookie_secure_parse_failed: %s", exc)
        secure = False
    parts = [f"uterm_tunnel_{tunnel_id}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _attach_share_token_cookie(response: Response, request: object, tunnel_id: str) -> Response:
    """Refresh an existing share-token cookie without reading URL tokens."""
    cookie = _share_token_cookie_header(request, tunnel_id)
    if cookie is not None:
        headers = dict(response.headers or {})  # ty:ignore[unresolved-attribute]
        headers["Set-Cookie"] = cookie
        response.headers = headers  # ty:ignore[invalid-assignment]
    return response


__all__ = ["_attach_share_token_cookie", "_share_token_cookie_header"]
