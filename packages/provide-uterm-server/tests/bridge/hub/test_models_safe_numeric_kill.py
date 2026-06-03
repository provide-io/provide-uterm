#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``models._safe_int`` / ``models._safe_float``.

These pure coercion helpers gate request-model numeric fields. ``models.py`` is in
the mutmut perimeter, but its covering tests lived only in non-``tests_dir`` suites,
so the helpers' mutants reported ``no tests`` once a ``models.py`` edit triggered the
changed-only gate. This self-contained suite (no async / subprocess / WebSocket —
reaper-safe) pins every branch and boundary so each operator/condition/exception
mutation flips an assertion, and is wired into ``[tool.mutmut].tests_dir``.
"""

from __future__ import annotations

from provide.uterm.server.bridge.models import _safe_float, _safe_int


class TestSafeInt:
    def test_valid_int_returned(self) -> None:
        # val present -> int(val), NOT int(default): kills the `val is None` flip.
        assert _safe_int(5, 80) == 5

    def test_none_uses_default(self) -> None:
        assert _safe_int(None, 80) == 80

    def test_none_coerces_default_not_val(self) -> None:
        assert _safe_int(None, 7) == 7

    def test_string_int_is_coerced(self) -> None:
        # Kills dropping the int(...) call (the str "12" != 12).
        assert _safe_int("12", 80) == 12

    def test_value_error_returns_default(self) -> None:
        # Non-numeric string -> ValueError -> default (kills narrowing the except).
        assert _safe_int("nope", 80) == 80

    def test_type_error_returns_default(self) -> None:
        # Un-int-able object -> TypeError -> default (kills narrowing the except).
        assert _safe_int(object(), 80) == 80

    def test_no_min_val_accepts_any(self) -> None:
        # min_val None -> no lower bound; kills `is not None` -> always-check (which
        # would do `result < None` -> TypeError) and the `is not None` -> `is None` flip.
        assert _safe_int(-100, 0) == -100

    def test_below_min_val_returns_default(self) -> None:
        assert _safe_int(0, 80, min_val=1) == 80

    def test_exact_min_val_is_accepted(self) -> None:
        # Boundary: rejection is `result < min_val`, NOT `<=`; min_val itself is valid.
        assert _safe_int(1, 80, min_val=1) == 1

    def test_above_min_val_is_accepted(self) -> None:
        assert _safe_int(2, 80, min_val=1) == 2


class TestSafeFloat:
    def test_valid_float_returned(self) -> None:
        # val present -> float(val), NOT float(default): kills the `val is None` flip.
        assert _safe_float(1.5, 9.0) == 1.5

    def test_int_coerced_to_float(self) -> None:
        assert _safe_float(3, 9.0) == 3.0

    def test_none_uses_default(self) -> None:
        assert _safe_float(None, 9.0) == 9.0

    def test_none_coerces_default_not_val(self) -> None:
        assert _safe_float(None, 2.5) == 2.5

    def test_string_float_is_coerced(self) -> None:
        # Kills dropping the float(...) call (the str "1.25" != 1.25).
        assert _safe_float("1.25", 9.0) == 1.25

    def test_value_error_returns_default(self) -> None:
        assert _safe_float("nope", 9.0) == 9.0

    def test_type_error_returns_default(self) -> None:
        assert _safe_float(object(), 9.0) == 9.0
