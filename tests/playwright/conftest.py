#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Playwright tests must run after all other tests to avoid event-loop contamination."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items: list) -> None:
    """Mark all playwright tests and move them to the end of the collection."""
    marker = pytest.mark.playwright
    playwright_items = []
    other_items = []
    for item in items:
        if "tests/playwright/" in str(item.fspath):
            item.add_marker(marker)
            playwright_items.append(item)
        else:
            other_items.append(item)
    items[:] = other_items + playwright_items
