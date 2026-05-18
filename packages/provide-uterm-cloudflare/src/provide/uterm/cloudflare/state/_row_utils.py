#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Row-shape adapters for the Cloudflare Durable Object SQL surface.

The Durable Object SQLite executor returns rows in several different shapes
depending on the runtime (native Python dicts, JS proxies with ``toArray``,
``fetchall``-style cursors, attribute-only objects, Pyodide ``to_py`` wrappers).
These helpers normalize that to ``list[Any]`` and provide indexed/keyed access
for ``SqliteStateStore``.
"""

from __future__ import annotations

import contextlib
from typing import Any


def rows(result: Any) -> list[Any]:
    if result is None:
        return []
    to_array = getattr(result, "toArray", None)
    if callable(to_array):
        return list(to_array())
    if isinstance(result, list):
        return result
    if hasattr(result, "fetchall"):
        return list(result.fetchall())
    return []


def get_by_index(row: Any, idx: int) -> Any:
    if isinstance(row, dict):
        values = list(row.values())
        return values[idx] if idx < len(values) else None
    if hasattr(row, "keys") and hasattr(row, "__getitem__"):
        keys = list(row.keys())
        if idx >= len(keys):
            return None
        return row[keys[idx]]
    if hasattr(row, "to_py"):
        try:
            py_row = row.to_py()
        except Exception:
            py_row = None
        if py_row is not None:
            return get_by_index(py_row, idx)
    return row[idx]


def row_value(row: Any, key: str, idx: int) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    if hasattr(row, "get"):
        with contextlib.suppress(Exception):
            value = row.get(key)
            if value is not None:
                return value
    if hasattr(row, key):
        with contextlib.suppress(Exception):
            return getattr(row, key)
    if hasattr(row, "to_py"):
        try:
            py_row = row.to_py()
        except Exception:
            py_row = None
        if py_row is not None:
            return row_value(py_row, key, idx)
    return get_by_index(row, idx)
