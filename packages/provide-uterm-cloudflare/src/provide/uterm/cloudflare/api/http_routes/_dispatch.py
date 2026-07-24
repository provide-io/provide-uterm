#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ._hijack import route_hijack
from ._session import SESSION_CAPABILITIES, SESSION_ROUTE_REGISTRY
from ._shared import _session_status_item

try:
    from provide.uterm.cloudflare.cf_types import json_response
except ImportError:  # pragma: no cover — CF flat-path fallback
    from cf_types import json_response  # type: ignore[import-not-found,no-redef]  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from provide.uterm.cloudflare.contracts import RuntimeProtocol

# Methods that mutate state and therefore must be protected from cross-site
# request forgery (CSRF). GET/HEAD are safe by convention.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_cross_site(request: object, url: str) -> bool:
    """Best-effort detection of a cross-site *browser* request (CSRF risk).

    State-changing routes accept the ``CF_Authorization`` cookie as a credential,
    and that cookie's ``SameSite`` attribute is set by Cloudflare Access — outside
    this app's control (see ``docs/cloudflare-security.md``). Rather than trust it,
    reject cross-site browser requests here using ``Sec-Fetch-Site`` (sent by every
    modern browser) with an ``Origin``-vs-host fallback for older agents.

    Non-browser clients (CLI, worker, server-to-server) send neither header and are
    treated as same-site — CSRF needs an *ambient* browser cookie, which those
    callers do not carry; they must present an explicit ``Authorization`` bearer.
    """
    headers = getattr(request, "headers", None)
    if headers is None:
        return False
    sec_fetch_site = str(headers.get("Sec-Fetch-Site") or "").lower()
    if sec_fetch_site:
        return sec_fetch_site == "cross-site"
    origin = str(headers.get("Origin") or "")
    if not origin:
        return False
    if origin.lower() == "null":
        return True
    return urlparse(origin).netloc != urlparse(url).netloc


async def _check_session_visibility(runtime: RuntimeProtocol, request: object) -> object | None:
    """Return a 403 Response if the caller cannot access the session, or None.

    Mirrors the FastAPI ``can_read_session`` policy:

    * ``public``   — any authenticated caller may read (JWT already validated).
    * ``operator`` — requires operator or admin role (or session ownership).
    * ``private``  — requires session ownership or admin role.

    Any other/missing visibility value is treated as ``public`` (safe default).
    """
    visibility = str(runtime.meta.get("visibility") or "public")
    if visibility == "public":
        return None
    role = await runtime.browser_role_for_request(request)
    if role == "admin":
        return None
    subject = await runtime.browser_subject_for_request(request)
    owner = runtime.meta.get("owner")
    if subject is not None and subject == owner:
        return None
    if visibility == "operator" and role == "operator":
        return None
    return json_response({"error": "forbidden"}, status=403)


async def route_http(runtime: RuntimeProtocol, request: object) -> object:
    url = str(getattr(request, "url", ""))
    path = urlparse(url).path
    method = str(getattr(request, "method", "GET")).upper()

    # CSRF guard: reject cross-site browser requests to state-changing routes.
    # Defends the cookie-authenticated POSTs (hijack acquire/send, input_mode,
    # disconnect_worker, …) regardless of the Cloudflare-managed CF_Authorization
    # SameSite attribute. Read-only GETs are unaffected.
    if method in _STATE_CHANGING_METHODS and _is_cross_site(request, url):
        return json_response({"error": "cross_site_blocked"}, status=403)

    if path == "/api/health":
        return json_response({"ok": True, "service": "provide-uterm-cloudflare"})

    if path == "/api/sessions":
        return json_response([_session_status_item(runtime)], headers={"X-Sessions-Scope": "local"})

    hijack_result = await route_hijack(runtime, request, path, url, method)
    if hijack_result is not None:
        return hijack_result

    match = SESSION_ROUTE_REGISTRY.match(method, path)
    if match is not None:
        if match.params["session_id"] != runtime.worker_id or getattr(runtime, "_deleted_at", None) is not None:
            return json_response({"error": "not_found", "path": path}, status=404)
        guard = await _check_session_visibility(runtime, request)
        if guard is not None:
            return guard
        # Installing/deleting webhooks is a mutation: read visibility alone must
        # not allow attaching an outbound terminal-exfil sink on public sessions.
        if match.route.capability in {"sessions.webhooks.create", "sessions.webhooks.delete"}:
            from provide.uterm.cloudflare.api.http_routes._session import _can_mutate_session

            if not await _can_mutate_session(runtime, request):
                return json_response({"error": "forbidden"}, status=403)
        return await SESSION_CAPABILITIES[match.route.capability](
            runtime, request, path, url, match.route, match.params
        )

    allowed = SESSION_ROUTE_REGISTRY.allowed_methods(path)
    if allowed:
        return json_response(
            {"error": "method not allowed"},
            status=405,
            headers={"Allow": ", ".join(allowed_method.value for allowed_method in allowed)},
        )
    if any(_matches_route_shape(path, route.template) for route in SESSION_ROUTE_REGISTRY.routes):
        return json_response({"error": "invalid route parameter"}, status=422)

    return json_response({"error": "not_found", "path": path}, status=404)


def _matches_route_shape(path: str, template: str) -> bool:
    """Return whether a malformed path has the shape of a session RouteDef."""
    path_segments = path.split("/")[1:]
    template_segments = template.split("/")[1:]
    return len(path_segments) == len(template_segments) and all(
        bool(actual) if expected.startswith("{") else actual == expected
        for actual, expected in zip(path_segments, template_segments, strict=True)
    )
