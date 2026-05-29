#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hijack-lease contract parity. Both backends drive the shared
``HijackCoordinator`` expiry rule (acquire / active / expire / single-winner /
release); these tests pin that contract identically across backends.

Note: the CF-specific wall-clock *persistence* across DO hibernation is a
Cloudflare-internal property (FastAPI keeps leases in-memory) and is covered by
the cloudflare package's own lease-persistence test, not here."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .backends import ConformanceBackend

pytestmark = pytest.mark.asyncio


async def test_acquire_makes_lease_active(backend: ConformanceBackend) -> None:
    assert backend.acquire_lease("w1", owner="alice", ttl_s=60, now=1000.0) is True
    assert backend.lease_active("w1", now=1000.0) is True


async def test_lease_expires_by_clock(backend: ConformanceBackend) -> None:
    backend.acquire_lease("w1", owner="alice", ttl_s=60, now=1000.0)
    assert backend.lease_active("w1", now=1059.0) is True  # within ttl
    assert backend.lease_active("w1", now=1061.0) is False  # past ttl


async def test_second_owner_denied_while_active(backend: ConformanceBackend) -> None:
    assert backend.acquire_lease("w1", owner="alice", ttl_s=60, now=1000.0) is True
    # A different owner cannot steal an active lease.
    assert backend.acquire_lease("w1", owner="mallory", ttl_s=60, now=1010.0) is False
    assert backend.lease_active("w1", now=1010.0) is True


async def test_release_frees_the_worker(backend: ConformanceBackend) -> None:
    backend.acquire_lease("w1", owner="alice", ttl_s=60, now=1000.0)
    backend.release_lease("w1")
    assert backend.lease_active("w1", now=1001.0) is False
    # After release, a new owner may acquire.
    assert backend.acquire_lease("w1", owner="bob", ttl_s=60, now=1002.0) is True
