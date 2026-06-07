#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""REST hijack-release lease ownership (acquirer / session-owner / admin).

A non-acquirer operator who reaches the release route via a shared session's
can_mutate_session must not be able to drop a lease they did not acquire. The
acquiring principal is recorded on the lease (HijackSession.acquired_by) and
verified by _may_release_lease at release time.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import APIRouter

from provide.uterm.server.bridge.routes.rest import (
    _may_release_lease,
    _principal_subject,
    register_rest_routes,
)


def _http_req(*, subject: str | None = None, session: Any = None, is_admin: bool = False) -> SimpleNamespace:
    principal = SimpleNamespace(subject_id=subject) if subject is not None else None
    return SimpleNamespace(
        state=SimpleNamespace(uterm_principal=principal),
        app=SimpleNamespace(
            state=SimpleNamespace(
                uterm_registry=SimpleNamespace(get_definition=AsyncMock(return_value=session)),
                uterm_authz=SimpleNamespace(is_admin=AsyncMock(return_value=is_admin)),
            )
        ),
    )


# -- _principal_subject ------------------------------------------------------


def test_principal_subject_present() -> None:
    assert _principal_subject(_http_req(subject="alice")) == "alice"


def test_principal_subject_absent() -> None:
    assert _principal_subject(SimpleNamespace(state=SimpleNamespace(uterm_principal=None))) is None


# -- _may_release_lease ------------------------------------------------------


async def test_legacy_lease_allows_capability_release() -> None:
    # acquired_by None → pre-existing capability model (hijack_id possession) → allowed.
    assert await _may_release_lease(_http_req(subject="bob"), "w1", SimpleNamespace(acquired_by=None)) is True


async def test_acquirer_may_release() -> None:
    assert await _may_release_lease(_http_req(subject="alice"), "w1", SimpleNamespace(acquired_by="alice")) is True


async def test_session_owner_may_release_others_lease() -> None:
    req = _http_req(subject="owner1", session=SimpleNamespace(owner="owner1"))
    assert await _may_release_lease(req, "w1", SimpleNamespace(acquired_by="someone-else")) is True


async def test_admin_may_release_others_lease() -> None:
    req = _http_req(subject="admin1", session=SimpleNamespace(owner="other"), is_admin=True)
    assert await _may_release_lease(req, "w1", SimpleNamespace(acquired_by="someone-else")) is True


async def test_admin_release_when_session_definition_missing() -> None:
    # registry returns None → owner branch skipped → falls through to admin check.
    req = _http_req(subject="admin1", session=None, is_admin=True)
    assert await _may_release_lease(req, "w1", SimpleNamespace(acquired_by="someone-else")) is True


async def test_non_acquirer_non_owner_non_admin_denied() -> None:
    req = _http_req(subject="intruder", session=SimpleNamespace(owner="other"), is_admin=False)
    assert await _may_release_lease(req, "w1", SimpleNamespace(acquired_by="someone-else")) is False


# -- release route: 403 branch -----------------------------------------------


def _release_endpoint(hub: Any):
    router = APIRouter()
    register_rest_routes(hub, router)
    for route in router.routes:
        if getattr(route, "path", "") == "/worker/{worker_id}/hijack/{hijack_id}/release":
            return route.endpoint  # type: ignore[attr-defined]
    raise AssertionError("release route not found")


async def test_release_route_denies_non_owner() -> None:
    """The route returns 403 when _may_release_lease denies (non-acquirer operator)."""
    hub = MagicMock()
    hub.get_rest_session = AsyncMock(return_value=SimpleNamespace(acquired_by="alice", owner="alice"))
    hub.release_rest_hijack = AsyncMock()
    release = _release_endpoint(hub)
    req = _http_req(subject="intruder", session=SimpleNamespace(owner="other"), is_admin=False)
    resp = await release(req, "w1", "00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 403
    hub.release_rest_hijack.assert_not_awaited()  # denied before the actual release
