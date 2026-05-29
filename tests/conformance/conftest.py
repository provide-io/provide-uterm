#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fixtures for the FastAPI ↔ Cloudflare conformance suite."""

from __future__ import annotations

import pytest

from .backends import CloudflareBackend, ConformanceBackend, FastApiBackend

pytestmark = pytest.mark.asyncio

_FACTORIES: dict[str, type[ConformanceBackend]] = {
    "fastapi": FastApiBackend,
    "cloudflare": CloudflareBackend,
}


@pytest.fixture(params=["fastapi", "cloudflare"])
def backend(request: pytest.FixtureRequest) -> ConformanceBackend:
    """A fresh backend adapter, run once per backend so every test is a parity test."""
    return _FACTORIES[request.param]()
