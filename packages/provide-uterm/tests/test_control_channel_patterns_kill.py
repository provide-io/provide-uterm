#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kills for ``LinkPatternRegistry``'s id-less sentinel counter.

Until 2026-09-03 this module produced **zero** mutants: both its classes were
``@dataclass``, and mutmut skips the entire body of a decorated class. The
registry is a plain class now, which put 53 mutants on the perimeter — and
exposed one family nothing could kill.

``_counter`` supplies dict keys for patterns registered without an ``id``. Its
*value* never reaches the public API: ``get_all`` returns values, ``unregister``
takes a string id, and ``sync_payload`` serialises patterns. Only key
**distinctness** is observable, so ``+= 2`` and ``-= 1`` collide no more often
than ``+= 1`` does and every behavioural assertion passes under them.

So the tests below assert the key sequence itself. That reaches into
``_patterns``, which is private — deliberately: the sentinel scheme is a
documented contract of this class (see the module docstring on
``control_channel_patterns``), and it is the only place the contract is visible.
The alternative was four allowlisted equivalents, which would have recorded the
gap rather than closed it.
"""

from __future__ import annotations

from provide.uterm.control_channel_patterns import LinkPattern, LinkPatternRegistry


def _idless(pattern: str) -> LinkPattern:
    return LinkPattern(pattern=pattern, action="cmd")


def test_id_less_patterns_take_consecutive_keys_counting_up_from_zero() -> None:
    """Kills ``_counter`` starting at 1, and ``+= 1`` becoming ``= 1``/``-= 1``/``+= 2``.

    Three patterns, not two: ``self._counter = 1`` only collides on the *third*
    id-less registration (keys 0, 1, then 1 again), so a two-pattern fixture
    reports the same observable state as the original.
    """
    registry = LinkPatternRegistry()

    registry.register(_idless("a"))
    registry.register(_idless("b"))
    registry.register(_idless("c"))

    assert list(registry._patterns) == [0, 1, 2]
    assert [p.pattern for p in registry.get_all()] == ["a", "b", "c"]


def test_clear_returns_the_counter_to_zero_not_merely_to_some_value() -> None:
    """Kills ``clear``'s ``_counter = 1``.

    An emptied registry cannot collide whatever the counter holds, so the reset
    is invisible unless the restarted sequence itself is asserted.
    """
    registry = LinkPatternRegistry()
    registry.register(_idless("a"))
    registry.register(_idless("b"))

    registry.clear()
    registry.register(_idless("c"))

    assert list(registry._patterns) == [0]


def test_an_id_less_pattern_never_displaces_one_registered_under_a_string_id() -> None:
    """The two key spaces are disjoint: ``int`` sentinels against ``str`` ids.

    Pins why the counter may run anywhere without corrupting id'd entries, so a
    future change to string-formatted sentinel keys fails here rather than
    silently overwriting a pattern whose id happens to read as a number.
    """
    registry = LinkPatternRegistry()

    registry.register(LinkPattern(pattern="kept", action="url", id="0"))
    registry.register(_idless("sentinel"))

    assert list(registry._patterns) == ["0", 0]
    assert [p.pattern for p in registry.get_all()] == ["kept", "sentinel"]
