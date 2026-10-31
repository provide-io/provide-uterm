#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared helpers for the previous-session suites: the read routes' and the PATCH's.

A plain module rather than a conftest, as capture_state_helpers is: mutmut
runs the suites with ``--import-mode=importlib``, under which a relative
import of a sibling module works and an import of ``tests.server`` does not.
Four ended sessions, two of alice's and two of bob's, one of each kind of
visibility, seeded before the app starts so it lists them from disk.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.auth import Principal
from provide.uterm.server.authorization import LocalAuthorizationProvider
from provide.uterm.server.capture_state import CaptureStateStore
from provide.uterm.server.config_schema_session import SessionDefinition
from provide.uterm.server.models import AuthConfig

from .viewer_revocation_helpers import JWT_KEY, bearer, mint_token

PRIVATE = "pam-alice-capture-1-10"
OPERATOR = "pam-alice-capture-2-10"
BOBS = "pam-bob-capture-3-10"
PUBLIC = "pam-bob-capture-4-10"


def capture_definition(session_id: str, owner: str, visibility: str) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name=f"{owner} (pts/1) [live]",
        connector_type="pty_capture",
        connector_config={"socket_path": "/run/uterm-capture/s.sock"},
        owner=owner,
        visibility=visibility,
    )


async def end_session(store: CaptureStateStore, definition: SessionDefinition, screen: str, ended_at: float) -> None:
    store._clock = lambda: ended_at
    store.track(definition, "i")
    await store.save_screen(definition.session_id, {"screen": screen, "cols": 90, "rows": 30}, ["one", "two"])
    await store.end(definition.session_id)


def make_app(tmp_path: Path, *, keep_days: int = 7, persist: bool = False) -> Any:
    config = default_server_config()
    config.sessions = []
    config.recording.directory = tmp_path / "recordings"
    config.session_persistence.previous_keep_days = keep_days
    config.session_persistence.persist_capture = persist
    config.session_persistence.state_dir = tmp_path / "capture-sessions"
    config.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=JWT_KEY,
        jwt_algorithms=["HS256"],
        worker_bearer_token=mint_token("runtime-worker", ["admin"]),
        clock_skew_seconds=30,
    )
    return create_server_app(config, api_only=True)


def seeded_client(tmp_path: Path) -> Iterator[TestClient]:
    store = CaptureStateStore(tmp_path / "capture-sessions", persist=False, keep_days=7, scrollback_lines=100)
    now = time.time()

    async def seed() -> None:
        await end_session(store, capture_definition(PRIVATE, "alice", "private"), "alice's private screen", now - 30)
        await end_session(store, capture_definition(OPERATOR, "alice", "operator"), "alice's operator screen", now - 20)
        await end_session(store, capture_definition(BOBS, "bob", "private"), "bob's screen", now - 10)
        await end_session(store, capture_definition(PUBLIC, "bob", "public"), "bob's public screen", now - 5)

    asyncio.run(seed())
    with TestClient(make_app(tmp_path)) as test_client:
        yield test_client


def as_caller(sub: str, *roles: str) -> dict[str, str]:
    return bearer(mint_token(sub, list(roles) or ["viewer"]))


def listed_ids(client: TestClient, headers: dict[str, str]) -> list[str]:
    response = client.get("/api/previous-sessions", headers=headers)
    assert response.status_code == 200
    return [entry["id"] for entry in response.json()["sessions"]]


class PolicyRoles(LocalAuthorizationProvider):
    """A provider that decides roles itself, as a webhook does: "policy-admin" is
    admin without the role in its token, and "root" is a viewer with it."""

    async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        return principal.subject_id in ("policy-admin", "root")

    async def is_admin(self, principal: Principal) -> bool:
        return principal.subject_id == "policy-admin"

    async def has_capability(self, principal: Principal, capability: str) -> bool:
        return principal.subject_id == "policy-admin" or await super().has_capability(principal, capability)
