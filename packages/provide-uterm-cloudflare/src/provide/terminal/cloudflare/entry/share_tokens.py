"""Share-token cookie helpers for tunnel routes."""

from __future__ import annotations

import logging
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from provide.terminal.cloudflare.entry.fallback_stubs import Response

logger = logging.getLogger(__name__)


def _share_token_cookie_header(request: object, tunnel_id: str) -> str | None:
    """Return an HttpOnly cookie header for a valid share token, if present."""
    token: str | None = None
    try:
        query = parse_qs(urlparse(str(getattr(request, "url", ""))).query)
        candidates: list[str | None] = [*query.get("token", []), *query.get("access_token", [])]
        token = (candidates or [None])[0]
    except Exception as exc:
        logger.debug("share_token_query_parse_failed: %s", exc)
        token = None
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
    secure = str(urlparse(str(getattr(request, "url", ""))).scheme).lower() == "https"
    parts = [f"uterm_tunnel_{tunnel_id}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _attach_share_token_cookie(response: Response, request: object, tunnel_id: str) -> Response:
    """Stamp the share token as an HttpOnly cookie so it stays out of HTML."""
    cookie = _share_token_cookie_header(request, tunnel_id)
    if cookie is not None:
        headers = dict(response.headers or {})
        headers["Set-Cookie"] = cookie
        response.headers = headers
    return response


__all__ = ["_attach_share_token_cookie", "_share_token_cookie_header"]
