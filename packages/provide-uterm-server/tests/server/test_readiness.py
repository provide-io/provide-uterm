#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the /readyz readiness endpoint and uterm_ready gate on /api/health."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.server.routes.health import create_health_router

# ---------------------------------------------------------------------------
# /readyz
# ---------------------------------------------------------------------------


def test_readyz_503_when_not_ready() -> None:
    """/readyz returns 503 when app.state.uterm_ready is False (or missing)."""
    bare = FastAPI()
    bare.state.uterm_ready = False
    bare.include_router(create_health_router())
    with TestClient(bare) as client:
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json() == {"status": "not_ready"}


def test_readyz_503_when_uterm_ready_missing() -> None:
    """/readyz returns 503 when uterm_ready attribute is absent from app.state."""
    bare = FastAPI()
    # do NOT set uterm_ready
    bare.include_router(create_health_router())
    with TestClient(bare) as client:
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json() == {"status": "not_ready"}


def test_readyz_200_when_ready() -> None:
    """/readyz returns 200 when app.state.uterm_ready is True."""
    bare = FastAPI()
    bare.state.uterm_ready = True
    bare.include_router(create_health_router())
    with TestClient(bare) as client:
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json() == {"status": "ready"}


# ---------------------------------------------------------------------------
# /healthz — pure liveness, always 200 regardless of uterm_ready
# ---------------------------------------------------------------------------


def test_healthz_always_200_when_not_ready() -> None:
    """/healthz returns 200 even when uterm_ready is False — it is pure liveness."""
    bare = FastAPI()
    bare.state.uterm_ready = False
    bare.include_router(create_health_router())
    with TestClient(bare) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_healthz_always_200_no_state() -> None:
    """/healthz returns 200 even with no app.state set at all."""
    bare = FastAPI()
    bare.include_router(create_health_router())
    with TestClient(bare) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /api/health gated on uterm_ready
# ---------------------------------------------------------------------------


def test_api_health_503_when_registry_set_but_not_ready() -> None:
    """/api/health returns 503 when registry is set but uterm_ready is False."""
    bare = FastAPI()
    bare.state.uterm_registry = object()  # type: ignore[assignment]
    bare.state.uterm_ready = False
    bare.include_router(create_health_router())
    with TestClient(bare) as client:
        r = client.get("/api/health")
        assert r.status_code == 503
        assert r.json()["ready"] is False


def test_api_health_200_when_registry_set_and_ready() -> None:
    """/api/health returns 200 when both registry and uterm_ready=True are set."""
    bare = FastAPI()
    bare.state.uterm_registry = object()  # type: ignore[assignment]
    bare.state.uterm_ready = True
    bare.include_router(create_health_router())
    with TestClient(bare) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ready"] is True
