#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fixtures for the bridge test package.

Autouse fixture: keep webhook egress tests hermetic by stubbing the DNS
resolver used by assert_webhook_target_allowed.  Tests that need a specific
resolver behaviour override this patch explicitly via their own monkeypatch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _stub_egress_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch egress._resolve_cached to return a benign public IP.

    This keeps every test in this package hermetic: no real DNS is performed
    for the fake webhook URLs used in unit tests (hook.test, etc.).
    """
    from provide.uterm.server import egress as egress_mod

    monkeypatch.setattr(
        egress_mod,
        "_resolve_cached",
        AsyncMock(return_value=("93.184.216.34",)),
    )
