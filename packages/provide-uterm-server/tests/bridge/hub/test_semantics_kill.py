#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for ``hub.semantics.CommandSplitter.split``.

``test_semantics.py`` covers every branch of the hand-rolled scanner (quotes,
escapes, ``;``/``&&``/``||``/``|``) and holds it at 100% line coverage, yet 37
of the 49 generated mutants survived it. Two mechanisms explain almost all of
them:

*A rewind hides behind a boolean check.* The scanner's four "consume one
character and advance" blocks (escape-consume, backslash-detect, and the two
quote-toggle blocks) each end in ``i += 1; continue``. Every existing test
only checks that scanning eventually finishes correctly -- it never asserts
*how far* ``i`` moved on a single step. Forcing that increment to the literal
constant ``1``, or to ``-= 1``, does not change behaviour when it happens to
land where the cursor already was (e.g. the very first character); it also
does not fail loudly on a bare pytest run in most inputs -- it silently
re-scans, and re-scanning a boundary character that already toggled state
either resurrects a character the real scanner had already protected, or
oscillates forever between the same two indices. Half of these mutants hang.

``pytest.mark.timeout`` is not a reliable way to catch that: mutmut applies
its *own* per-mutant CPU-time limit, and under a loaded parallel run that fires
before pytest-timeout's SIGALRM does, which reports as mutmut's TIMEOUT
state rather than as a killed/failed test -- and the mutation gate treats
TIMEOUT as a non-kill, not a pass. Bounding the call on a background thread
does not work either: the join gives up, the test fails, but the hung thread
keeps spinning in the same process and pytest has to finish the session
around it. Every GIL hand-off then costs a full switch interval, and the
spinning thread's CPU time counts against mutmut's limit -- ``i += 1`` ->
``i = 1`` burned ~51s of CPU on a single failing test and CI recorded it as
TIMEOUT. So every test below runs ``CommandSplitter().split`` through
``_split_bounded``, which hands it a ``str`` subclass carrying an operation
budget. The scan loop calls ``len(command)`` once per iteration, so a scan
that runs away exhausts the budget within a few hundred operations and
*raises from inside the loop itself*: the call unwinds, nothing is left
running, and the mutant is recorded as killed in milliseconds.

*A discarded value is asserted only by its type, not its content.* ``found_op``
and the fixed defaults on ``op_len``/``escaped``/``in_*_quote``/``start`` are
read back only through truthiness (``if found_op:``) or through Python's
``None``-means-``0`` slice semantics (``command[None:i] == command[0:i]``).
Mutants that swap one falsy/no-op value for another falsy/no-op value are
genuinely equivalent -- see the module-level ``EQUIVALENT_MUTANTS`` note below
-- and are listed rather than chased with a test that can never fail.

Every kill test in this file was validated against the actual mutmut-generated
mutant source (not just reasoned about) before being written.
"""

from __future__ import annotations

import pytest

from provide.uterm.server.bridge.hub.semantics import CommandSplitter

# Operations allowed per input character. A correct scan makes at most one
# ``len()`` call and a handful of ``[]`` lookups per character; a runaway scan
# makes unboundedly many, so any generous constant separates the two.
_OPS_PER_CHAR = 16


class ScanBudgetExceededError(AssertionError):
    """Raised from inside the scan loop when it does far more work than its input allows."""


class _BudgetedStr(str):
    """A ``str`` that raises once ``len()``/``[]`` have been called too many times.

    ``split`` reads its input only through ``len(command)`` (once per loop
    iteration), ``command[i]``/``command[a:b]`` and ``startswith``. Counting the
    first two bounds every iteration of the scan loop, so a mutant that stops
    the cursor advancing (or rewinds it) fails deterministically and promptly
    instead of hanging. ``str`` methods do not route through these dunders, and
    ``__getitem__`` returns a plain ``str``, so results compare exactly.
    """

    __slots__ = ("_budget",)

    def __new__(cls, value: str) -> _BudgetedStr:
        obj = super().__new__(cls, value)
        obj._budget = _OPS_PER_CHAR * (len(value) + 1)
        return obj

    def _spend(self) -> None:
        self._budget -= 1
        if self._budget < 0:
            raise ScanBudgetExceededError(f"CommandSplitter().split({str(self)!r}) did not terminate")

    def __len__(self) -> int:
        self._spend()
        return super().__len__()

    def __getitem__(self, key: object) -> str:  # type: ignore[override]
        self._spend()
        return super().__getitem__(key)  # type: ignore[index]


def _split_bounded(command: str) -> list[str]:
    """Run ``CommandSplitter().split(command)`` with an operation budget.

    Several mutants below stop or rewind the scanner's cursor and loop forever
    instead of returning. The budget turns that hang into an exception raised
    from inside the loop -- see the module docstring for why neither
    ``pytest.mark.timeout`` nor a thread-bounded join is good enough.
    """
    return CommandSplitter().split(_BudgetedStr(command))


def test_the_budget_never_trips_on_a_correct_scan() -> None:
    """The budget must leave headroom for the real scanner on every construct it handles."""
    command = "a 'b;c' \\; \"d|e\" && f || g | h ; i\\;j"
    assert _split_bounded(command) == ["a 'b;c' \\; \"d|e\"", "f", "g", "h", "i\\;j"]


def test_the_budget_trips_on_a_scan_that_never_advances() -> None:
    """The budget itself must fire: a loop that re-reads one index forever raises promptly."""
    budgeted = _BudgetedStr("ab")
    with pytest.raises(ScanBudgetExceededError):
        for _ in range(_OPS_PER_CHAR * 100):
            budgeted[0]


# ---------------------------------------------------------------------------
# Equivalent mutants (documented, not tested):
#
# - `in_single_quote`/`in_double_quote`/`escaped` initialised (or reset) to
#   `None` instead of `False`: every read is a `not x` / `if x:` truthiness
#   check, never `is False`/`is None`, and `not None == not False == True`,
#   so the very first toggle produces an identical boolean either way.
# - `start = None` instead of `start = 0`: every use is a slice bound
#   (`command[start:i]`, `command[start:]`), and Python defines
#   `seq[None:x] == seq[0:x]` -- the mutation is a no-op by definition.
# - `found_op = None` initialised to `""` instead: `found_op` is read back
#   only via `if found_op:`, and both are falsy.
# - `op_len = 0` initialised to `None`/`1`: `op_len` is only read inside
#   `if found_op:`, and every branch that sets `found_op` truthy also sets
#   `op_len` in the same branch, so the initial value is dead.
# - `found_op = "&&"/"||"/";"/"|"` replaced with a non-empty `"XX..XX"`
#   sentinel: `found_op`'s *value* is never read again after the branch that
#   sets it (only its truthiness, at `if found_op:`), so any non-empty
#   replacement string is behaviourally identical.
# - `command.startswith("XX||XX", i)` instead of `("||", i)`: the literal
#   never matches real input, so this elif always falls through to the two
#   single-`"|"` branches below it. Splitting a `"||"` pair as two adjacent
#   `"|"` operators is always net-identical to splitting it as one `"||"`
#   operator, because the "part" between two adjacent operator matches is
#   always empty and is discarded by `if part:` -- confirmed by brute-force
#   search over ~600k generated command strings with no counter-example.
# ---------------------------------------------------------------------------


def test_leading_operator_is_not_swallowed_by_a_bootstrapped_true_escape_flag() -> None:
    """``escaped``'s *initial* declaration must start ``False``, not ``True``.

    If it started ``True``, the scanner would treat the command's first
    character as if it followed a backslash -- silently consumed, never
    checked as an operator -- before any character has actually been read.
    """
    assert _split_bounded(";ls") == ["ls"]


def test_the_scan_cursor_must_start_at_index_zero_not_one() -> None:
    """``i``'s initial declaration must be ``0``; starting at ``1`` skips command[0].

    Skipping the first character means it is never checked as an operator,
    so a leading ``;`` is folded into the following text instead of splitting
    on it.
    """
    assert _split_bounded(";x") == ["x"]


def test_an_escaped_operator_stays_escaped_when_the_flag_resets_forever() -> None:
    """The ``escaped`` flag must reset to ``False`` after consuming one character.

    If the reset instead re-armed ``escaped`` to ``True``, every character
    after the first backslash is treated as escaped forever, and the ``;``
    later in the command is swallowed as literal text instead of splitting.
    """
    assert _split_bounded("a\\b;c") == ["a\\b", "c"]


def test_the_character_right_after_a_leading_backslash_is_never_rescanned() -> None:
    """Consuming the escaped character must advance the cursor by exactly one.

    Rewinding this step (forcing the cursor to the literal constant ``1``)
    re-examines the just-protected character as if it had never been
    escaped -- here, that character is itself an operator, so a rewind lets
    it split when it should have stayed literal. A rewind that instead lands
    back *on* the backslash (the ``-=`` variant) oscillates forever, which is
    why this test carries the module's operation budget.
    """
    assert _split_bounded("\\;x") == ["\\;x"]


def test_consuming_an_escaped_character_never_skips_the_one_after_it() -> None:
    """Consuming the escaped character must advance by exactly one, not two.

    Advancing by two (or breaking out of the scan loop entirely) skips the
    very next character without ever checking it as an operator, so a ``;``
    placed right after the escaped character is folded into the surrounding
    text instead of splitting the command.
    """
    assert _split_bounded("\\a;b") == ["\\a", "b"]


def test_detecting_a_backslash_mid_command_advances_by_exactly_one() -> None:
    """Backslash-detection must set the cursor to (its own index + 1).

    Forcing it to the literal ``1`` is invisible only when the backslash is
    the very first character (position 0); anywhere later, it rewinds onto
    or past the backslash itself and the scan oscillates -- hence the
    operation budget. The ``+= 2``/``break`` variants instead skip the escaped
    character forward, letting the operator after it (protected by the
    escape) be treated as literal text bundled into the wrong part.
    """
    assert _split_bounded("ab\\c;d") == ["ab\\c", "d"]


def test_toggling_single_quote_state_advances_by_exactly_one() -> None:
    """The single-quote toggle branch must step the cursor by exactly one.

    A quoted string always has (at least) two quote characters; rewinding
    the step to the literal constant ``1`` is a no-op on the *opening*
    quote (already at index ~1) but re-triggers on the *closing* quote,
    which oscillates the quote state forever -- hence the operation budget.
    The ``+= 2``/``break`` variants instead skip the character right after a
    quote, letting a protected ``;`` leak through as literal text.
    """
    assert _split_bounded("'a';b") == ["'a'", "b"]


def test_toggling_double_quote_state_advances_by_exactly_one() -> None:
    """Mirror of the single-quote case for the double-quote toggle branch."""
    assert _split_bounded('"a";b') == ['"a"', "b"]


def test_double_ampersand_operator_length_is_exactly_two() -> None:
    """``&&``'s ``op_len`` must be ``2``; ``3`` overshoots past the next part.

    Overshooting by one consumes the first character of what should have
    been the next command, so it is silently dropped from the result.
    """
    assert _split_bounded("a&&b") == ["a", "b"]


def test_double_pipe_operator_length_is_exactly_two() -> None:
    """Mirror of the ``&&`` case: ``||``'s ``op_len`` must be ``2``, not ``3``."""
    assert _split_bounded("a||b") == ["a", "b"]


def test_double_pipe_detection_actually_checks_for_double_pipe() -> None:
    """A ``||`` at the very start of the command must be detected as one operator.

    If the ``command.startswith("||", i)`` check used ``None`` (or omitted
    ``i``) as the search-start position instead of the live cursor, it always
    re-checks from absolute position 0 -- which happens to also be a ``||``
    here, so the branch is still entered, but the *rest* of the scan is
    fed a stale start position and the leading empty part is lost outright
    instead of simply not being appended.
    """
    assert _split_bounded("||a") == ["a"]


def test_semicolon_operator_length_is_exactly_one() -> None:
    """``;``'s ``op_len`` must be ``1``; ``2`` overshoots past the next character."""
    assert _split_bounded("a;b") == ["a", "b"]


def test_single_pipe_operator_length_is_exactly_one() -> None:
    """Mirror of the ``;`` case for the single-``|`` branch."""
    assert _split_bounded("a|b") == ["a", "b"]


def test_operator_consumption_advances_the_cursor_forward_not_to_op_len() -> None:
    """Consuming an operator must move the cursor by ``op_len``, not *to* ``op_len``.

    Setting ``i`` to the literal ``op_len`` (rather than advancing by it)
    is invisible only when the operator happens to sit at that exact index;
    anywhere else it rewinds into already-scanned text and re-detects the
    same operator forever -- hence the operation budget.
    """
    assert _split_bounded("a;") == ["a"]


def test_operator_consumption_never_walks_the_cursor_backward() -> None:
    """Consuming an operator must add ``op_len``, not subtract it.

    Subtracting walks the cursor to a negative index; Python's negative
    indexing quietly wraps that back into the string and the scan runs off
    the front of the buffer entirely, raising ``IndexError`` on the very
    next character lookup.
    """
    assert _split_bounded(";") == []


def test_the_main_loop_advances_one_character_at_a_time() -> None:
    """The unconditional bottom-of-loop ``i += 1`` must not become ``i = 1``.

    Forcing the cursor back to the literal ``1`` after the very first plain
    character is a no-op (it was already headed to ``1``); on the second
    character it rewinds and the scanner re-reads the same character
    forever -- hence the operation budget. The ``-= 1`` variant instead walks
    off the front of the string and raises ``IndexError``, which also fails
    this test, just via an exception instead of a timeout.
    """
    assert _split_bounded("ab") == ["ab"]
