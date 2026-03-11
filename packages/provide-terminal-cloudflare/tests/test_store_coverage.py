#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for state/store.py — CF-style row types, _rows/_get/_row_value branches."""

from __future__ import annotations

import sqlite3

from provide_terminal_cloudflare.state.store import SqliteStateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _real_store() -> SqliteStateStore:
    """Real in-memory sqlite3 store (standard tuple rows)."""
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    store.migrate()
    return store


def _row_store() -> SqliteStateStore:
    """sqlite3 store with row_factory=sqlite3.Row — rows are sqlite3.Row objects."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = SqliteStateStore(conn.execute)
    store.migrate()
    return store


class _FetchResult:
    """Simulates a cursor whose fetchall() returns a fixed list."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return self._rows


class _MockExec:
    """Executor that wraps a real exec but overrides SELECT results."""

    def __init__(self, real_exec, select_rows: list) -> None:
        self._real = real_exec
        self._rows = select_rows

    def __call__(self, sql: str, *args: object) -> object:
        if sql.strip().upper().startswith("SELECT"):
            return _FetchResult(self._rows)
        # For DDL / writes, use the real executor
        try:
            return self._real(sql, *args)
        except Exception:
            return self._real(sql, args)


def _store_with_select_rows(rows: list) -> SqliteStateStore:
    """Store whose SELECT queries return the given rows (DDL goes to real sqlite3)."""
    conn = sqlite3.connect(":memory:")
    real_exec = conn.execute
    # Migrate with real exec first
    real = SqliteStateStore(real_exec)
    real.migrate()
    # Now wrap with the mock so SELECT returns custom rows
    return SqliteStateStore(_MockExec(real_exec, rows))


# ---------------------------------------------------------------------------
# SqliteStateStore._rows — static method, call directly
# ---------------------------------------------------------------------------


def test_rows_none_returns_empty() -> None:
    """Line 289: _rows(None) → []."""
    assert SqliteStateStore._rows(None) == []


def test_rows_list_returned_directly() -> None:
    """Lines 293-294: _rows(list) → the list itself."""
    data = [{"a": 1}, {"a": 2}]
    assert SqliteStateStore._rows(data) is data


def test_rows_toarray_callable() -> None:
    """Lines 290-292: result has callable toArray() → list from toArray()."""

    class _JsResult:
        def toArray(self) -> list:  # noqa: N802
            return ["x", "y"]

    assert SqliteStateStore._rows(_JsResult()) == ["x", "y"]


def test_rows_fetchall_cursor() -> None:
    """Lines 295-296: result has fetchall() → result of fetchall()."""
    assert SqliteStateStore._rows(_FetchResult(["a", "b"])) == ["a", "b"]


def test_rows_unknown_object_returns_empty() -> None:
    """Line 297: result is not None/list/toArray/fetchall → []."""
    assert SqliteStateStore._rows(object()) == []


# ---------------------------------------------------------------------------
# SqliteStateStore._get — static method
# ---------------------------------------------------------------------------


def test_get_dict_by_index() -> None:
    """Lines 302-303: _get on a dict → value at position idx."""
    row = {"a": 10, "b": 20}
    assert SqliteStateStore._get(row, 0) == 10
    assert SqliteStateStore._get(row, 1) == 20


def test_get_dict_idx_out_of_range() -> None:
    """Line 303: _get on dict with idx >= len → None."""
    assert SqliteStateStore._get({"a": 1}, 5) is None


def test_get_keys_getitem_row() -> None:
    """Lines 305-308: row has .keys() and .__getitem__ (e.g. sqlite3.Row)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (seq INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    row = conn.execute("SELECT seq FROM t").fetchone()
    assert SqliteStateStore._get(row, 0) == 42


def test_get_keys_getitem_idx_out_of_range() -> None:
    """Line 307: keys+getitem row with idx >= len(keys) → None."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    row = conn.execute("SELECT a FROM t").fetchone()
    assert SqliteStateStore._get(row, 99) is None


def test_get_to_py_dict() -> None:
    """Lines 310-315: row has to_py() → delegates to _get on py result."""

    class _PyodideRow:
        def to_py(self) -> dict:
            return {"x": 7}

    assert SqliteStateStore._get(_PyodideRow(), 0) == 7


def test_get_to_py_raises() -> None:
    """Lines 310-313: row.to_py() raises → py_row=None → falls through to row[idx]."""

    class _BadToPy:
        def to_py(self) -> None:
            raise RuntimeError("boom")

        def __getitem__(self, idx: int) -> int:
            return 99

    assert SqliteStateStore._get(_BadToPy(), 0) == 99


# ---------------------------------------------------------------------------
# SqliteStateStore._row_value — class method
# ---------------------------------------------------------------------------


def test_row_value_dict() -> None:
    """Line 268: isinstance(row, dict) → row.get(key)."""
    assert SqliteStateStore._row_value({"seq": 5}, "seq", 0) == 5


def test_row_value_get_method_non_dict() -> None:
    """Lines 270-273: row has .get() but is not a dict → value from .get()."""

    class _GetObj:
        def get(self, key: str, default: object = None) -> object:
            return {"seq": 42}.get(key, default)

    assert SqliteStateStore._row_value(_GetObj(), "seq", 0) == 42


def test_row_value_get_method_returns_none_falls_through() -> None:
    """Lines 270-273: row.get() returns None → falls through to attribute check."""

    class _NoneGet:
        def get(self, key: str, default: object = None) -> None:
            return None

        seq = 11

    # Falls through to hasattr(row, key) → getattr
    assert SqliteStateStore._row_value(_NoneGet(), "seq", 0) == 11


def test_row_value_attribute() -> None:
    """Lines 275-276: row has the named attribute → getattr(row, key)."""

    class _AttrRow:
        event_type = "snapshot"

    assert SqliteStateStore._row_value(_AttrRow(), "event_type", 2) == "snapshot"


def test_row_value_to_py() -> None:
    """Lines 278-283: row has to_py() → delegates to _row_value on py result."""

    class _PyRow:
        def to_py(self) -> dict:
            return {"seq": 99}

    assert SqliteStateStore._row_value(_PyRow(), "seq", 0) == 99


def test_row_value_to_py_raises_falls_through() -> None:
    """Lines 278-281: row.to_py() raises → py_row=None → falls to _get."""

    class _BadPy:
        def to_py(self) -> None:
            raise RuntimeError("nope")

        def __getitem__(self, idx: int) -> int:
            return 77

    assert SqliteStateStore._row_value(_BadPy(), "missing_key", 0) == 77


# ---------------------------------------------------------------------------
# min_event_seq / current_event_seq — row-type branches
# ---------------------------------------------------------------------------


def test_min_event_seq_empty_rows() -> None:
    """Lines 155-156: _rows returns [] → returns 0 immediately."""
    store = _store_with_select_rows([])
    assert store.min_event_seq("worker-x") == 0


def test_min_event_seq_dict_row() -> None:
    """Lines 158-159: row is a dict → row.get('seq')."""
    store = _store_with_select_rows([{"seq": 3}])
    assert store.min_event_seq("worker-x") == 3


def test_min_event_seq_keys_getitem_row() -> None:
    """Lines 160-161: sqlite3.Row (has .keys() + .__getitem__) → row['seq']."""
    store = _row_store()
    store.append_event("w1", "test", {})
    store.append_event("w1", "test", {})
    result = store.min_event_seq("w1")
    assert result >= 1


def test_current_event_seq_empty_rows() -> None:
    """Lines 231-232: _rows returns [] → returns 0 immediately."""
    store = _store_with_select_rows([])
    assert store.current_event_seq("worker-x") == 0


def test_current_event_seq_dict_row() -> None:
    """Lines 234-235: row is a dict → row.get('seq')."""
    store = _store_with_select_rows([{"seq": 7}])
    assert store.current_event_seq("worker-x") == 7


def test_current_event_seq_keys_getitem_row() -> None:
    """Lines 236-237: sqlite3.Row → row['seq']."""
    store = _row_store()
    store.append_event("w2", "evt", {})
    result = store.current_event_seq("w2")
    assert result >= 1
