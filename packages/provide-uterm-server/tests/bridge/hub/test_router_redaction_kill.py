#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for ``router_redaction`` — the two things its coverage suite never asks.

``test_snapshot_redaction.py`` exercises every branch of these helpers and holds
the file at 100% line coverage. It still leaves 15 of 106 mutants alive, in two
tight clusters, because line coverage cannot see either of these:

*The ``""`` defaults never fire.* Every existing frame carries its content
field, so ``msg.get("data", "")`` is only ever reached with the key present and
the default is dead weight to the tests. Change it to ``None`` and nothing
fails — but the wire then carries the four-character string ``"None"`` where a
terminal frame's payload should be, because ``str(None)`` is not empty.

*Nothing nests past the recursion cap.* Real snapshot payloads are two or three
levels deep, so the ``_depth`` arithmetic is unobservable: the counter can start
at 1, count by 2, count backwards, or not be passed at all, and every shallow
assertion still passes. The cap is what stops a hostile payload from driving a
``RecursionError`` on a hot broadcast path, and its cost is that secrets below
it ship unredacted — so where exactly it falls is a security boundary, not an
implementation detail, and it is pinned here to the level.
"""

from __future__ import annotations

from typing import Any

import pytest

from provide.uterm.server.bridge.hub.ext import RedactionRule
from provide.uterm.server.bridge.hub.redaction import StreamRedactor
from provide.uterm.server.bridge.hub.router_redaction import (
    _REDACT_MAX_DEPTH,
    _redact_frame_fields,
    _redact_value,
)

_PLANTED = "sk_live_ABC"
_RULES = [RedactionRule(pattern=r"sk_live_\w+", replacement="[R]")]


def _redactor() -> StreamRedactor:
    return StreamRedactor(_RULES)


def _nest(depth: int, *, container: str) -> Any:
    """Wrap ``_PLANTED`` in *depth* nested dicts or lists."""
    value: Any = _PLANTED
    for _ in range(depth):
        value = {"k": value} if container == "dict" else [value]
    return value


def _innermost(value: Any) -> Any:
    while isinstance(value, (dict, list)):
        value = value["k"] if isinstance(value, dict) else value[0]
    return value


# ---------------------------------------------------------------------------
# The "" defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame_type", "field"),
    [("term", "data"), ("snapshot", "screen"), ("analysis", "formatted")],
)
def test_a_frame_missing_its_content_field_redacts_to_empty_not_the_word_none(frame_type: str, field: str) -> None:
    """The default is ``""``, and it has to be: the value is ``str()``-wrapped.

    With a ``None`` default (or no default at all) the helper does not skip the
    field — it ships ``str(None)``, putting the literal text ``None`` into a
    terminal payload, a screen, or an analysis summary. Any non-empty default
    is wrong for the same reason.
    """
    out = _redact_frame_fields({"type": frame_type}, _redactor())

    assert out[field] == ""


# ---------------------------------------------------------------------------
# The recursion cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("container", ["dict", "list"])
def test_a_secret_at_the_last_walked_level_is_still_redacted(container: str) -> None:
    """``_REDACT_MAX_DEPTH`` containers deep is inside the cap, so it is walked.

    Pins the near side of the boundary. Any mutant that makes the counter run
    hot — starting at 1, stepping by 2 — trips the cap one or more levels early
    and leaks this secret.
    """
    result = _redact_value(_nest(_REDACT_MAX_DEPTH, container=container), _redactor())

    assert _innermost(result) == "[R]"


@pytest.mark.parametrize("container", ["dict", "list"])
def test_a_container_at_the_cap_is_returned_unwalked_so_a_deeper_secret_survives(
    container: str,
) -> None:
    """One level further and the container is returned verbatim, secret intact.

    This is the documented trade — failing closed on depth returns raw rather
    than raising on a broadcast path — so it is asserted rather than assumed.
    Pins the far side: a counter that never advances (not passed, or counting
    down) walks forever and redacts here, and ``>`` instead of ``>=`` moves the
    boundary by exactly this one level.
    """
    result = _redact_value(_nest(_REDACT_MAX_DEPTH + 1, container=container), _redactor())

    assert _innermost(result) == _PLANTED


def test_the_cap_is_the_value_the_boundary_tests_are_written_against() -> None:
    """The two tests above derive their nesting from the constant, so they follow it anywhere.

    That is what makes them readable, and it is also what would let a change to
    the constant itself pass unnoticed. Stating the number once, here, keeps the
    boundary a decision someone has to make deliberately.
    """
    assert _REDACT_MAX_DEPTH == 32
