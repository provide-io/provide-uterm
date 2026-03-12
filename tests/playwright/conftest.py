#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Playwright tests must run after all other tests to avoid event-loop contamination."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def pytest_collection_modifyitems(items: list) -> None:
    """Move all items from tests/playwright/ to the end of the test collection."""
    playwright_items = [i for i in items if "tests/playwright/" in str(i.fspath)]
    other_items = [i for i in items if "tests/playwright/" not in str(i.fspath)]
    items[:] = other_items + playwright_items
