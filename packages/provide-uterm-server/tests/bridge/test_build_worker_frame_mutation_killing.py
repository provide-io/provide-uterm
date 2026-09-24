#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``_build_worker_frame`` (and ``_opt_int``).

``_build_worker_frame`` is the one place that translates a worker's raw
WebSocket ``msg`` dict into the frame the hub broadcasts. Every key it reads,
every default it substitutes when a key is missing, and every type coercion
it applies is wire format -- get any of them wrong and either a real value
silently turns into a default, or a worker predating a field looks
indistinguishable from one reporting a genuine zero/false/empty value.

None of the existing tests exercise that translation layer field-by-field:

- ``test_frame_wire_snapshot.py`` calls ``make_snapshot_frame`` /
  ``make_analysis_frame`` / ``coerce_worker_status_frame`` directly with
  hand-built kwargs. It pins the wire *shape* those builders emit, but never
  goes through ``_build_worker_frame``'s ``msg.get(key, default)`` calls, so
  a wrong key name, a wrong default, or a dropped ``min_val`` guard in
  ``_build_worker_frame`` itself is invisible to it.
- ``test_snapshot_ingest_counters.py`` calls ``_build_worker_frame`` directly,
  but only ever varies ``chunks_read``/``bytes_read`` (plus a fixed set of
  other fields held constant across every case) and its ``TestOptInt`` class
  already drives ``_opt_int`` itself through int/zero/None/non-int-string/
  float/bool. It says nothing about ``screen``, ``cursor``, ``cols``, ``rows``,
  ``screen_hash``, ``cursor_at_end``, ``has_trailing_space``,
  ``prompt_detected``, ``raw_tail``, ``ts``, the ``analysis`` branch, or the
  status/unknown fallthrough -- exactly the fields with surviving mutants.
- Every other ``test_snapshot_*``/``test_bridge_snapshot_*`` file drives a
  real ``TermHub`` end to end and asserts on the *consumer* side (a broadcast
  happened, a subscriber received a frame); none of them pin the exact
  dict ``_build_worker_frame`` itself returns for a given input ``msg``.

Below, every case builds its ``msg`` and its expected frame independently:
the expected frame is always constructed with a hand-written call to the
underlying ``make_*_frame``/``coerce_worker_status_frame`` builder (whose own
correctness is ``test_frame_wire_snapshot.py``'s job), so the assertion is a
genuine check on ``_build_worker_frame``'s extraction logic, not a
tautology. Every comparison is whole-dict ``==``, never individual keys, so a
mutant that only breaks one field can't hide behind an unrelated pinned key.

Documented equivalents (25 of the 137 survived mutants; to go into
``mutation_equivalents.toml`` when this file joins the perimeter). Each was
checked by exec'ing the extracted mutant body against every case below (and
several more) in a throwaway script and confirming the output is identical
to ``__mutmut_orig`` for every input, not just the ones exercised here:

- mutmut 4, 8, 9, 10 (outer ``cast("dict[str, Any]", make_snapshot_frame(...))``
  around the snapshot return), 145, 149, 150, 151 (the same around
  ``make_analysis_frame(...)``), and 176, 180, 181, 182 (the same around
  ``coerce_worker_status_frame(msg)``): each mutates only the *first*
  argument of a ``typing.cast(typ, val)`` call -- to a garbled string, a
  different case, or ``None``. ``typing.cast`` is defined as
  ``def cast(typ, val): return val``; it never inspects ``typ`` at runtime,
  so no input can make these mutants disagree with the original.
- mutmut 43, 47, 48 (the ``cursor=cast("dict[str, int]", ...)`` type
  string), 107, 111, 112, 113 (the ``prompt_detected=cast("dict[str, Any] |
  None", ...)`` type string), and 117, 121, 122, 123 (the
  ``raw_tail=cast("str | None", ...)`` type string): the same
  cast-ignores-its-first-argument reasoning, applied to the three inner
  ``cast(...)`` calls instead of the outer one.
- mutmut 101 (``has_trailing_space=bool(msg.get("has_trailing_space",
  None))``) and 103 (``bool(msg.get("has_trailing_space", ))``, i.e. the
  default silently drops to ``.get``'s own implicit ``None``): the default
  only matters when the key is absent, and ``bool(None) == bool(False) ==
  False`` -- collapsing the default to ``None`` is behaviourally identical
  to leaving it ``False`` once wrapped in ``bool()``.

mutmut 136 and 170 (the ``ts=_safe_float(msg.get("ts"), None)`` default
swap, in the snapshot and analysis branches respectively) looked equivalent
under a single frozen ``time.time()`` -- both the original (default
``time.time()`` at this call site) and the mutant (default ``None``, which
``_safe_float`` degrades to on ``float(None)``'s ``TypeError``, deferring to
``make_snapshot_frame``/``make_analysis_frame``'s own ``time.time() if ts is
None else ts`` fallback one frame later) end up asking "what time is it"
exactly once, and a *single* frozen clock answers both call sites
identically. But ``websockets_worker.py``, the core ``provide.uterm.frames``
(the snapshot fallback) and ``provide.uterm.server.bridge.frames`` (the
analysis fallback) each do their own ``import time``, so pinning each
module's ``time`` **name** to a *different* constant (see ``_frozen_clocks``
below) makes "which call site actually supplied the default" observable:
the original always reports ``WORKER_TS``, the mutant reports whichever
builder's own constant. These are real, killed below by
``test_minimal_snapshot_message_pins_every_default`` (136) and
``test_minimal_analysis_message_pins_every_default`` (170).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm import frames as _core_frames_module
from provide.uterm.server.bridge import frames as _server_frames_module
from provide.uterm.server.bridge.frames import make_analysis_frame, make_snapshot_frame
from provide.uterm.server.bridge.routes import websockets_worker as _websockets_worker_module
from provide.uterm.server.bridge.routes.websockets_worker import _build_worker_frame, _opt_int

# Three DIFFERENT constants so that, when `ts` is absent, "which module's own
# `time.time()` actually supplied the default" is an observable value rather
# than three clocks that happen to agree. The original always answers
# WORKER_TS (websockets_worker's own default-arg call); a mutant that drops
# that default to a hardcoded `None` answers whichever builder's OWN
# `time.time() if ts is None else ts` fallback picks it up one frame later.
WORKER_TS = 1_000_000.0
_CORE_FRAMES_TS = 2_000_000.0
_SERVER_FRAMES_TS = 3_000_000.0


@pytest.fixture(autouse=True)
def _frozen_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebind each module's own ``time`` name to a distinct fake clock.

    Patching the shared ``time`` module's ``.time`` attribute in place (e.g.
    ``monkeypatch.setattr("time.time", ...)``) would make every consumer's
    ``import time`` see the same value, since they all share one module
    object -- which is exactly what would make mutmut 136/170 look
    equivalent (see the module docstring). Rebinding the ``time`` *name*
    inside each of the three relevant modules instead gives each one its own
    independent, deterministic clock.
    """
    monkeypatch.setattr(_websockets_worker_module, "time", SimpleNamespace(time=lambda: WORKER_TS))
    monkeypatch.setattr(_core_frames_module, "time", SimpleNamespace(time=lambda: _CORE_FRAMES_TS))
    monkeypatch.setattr(_server_frames_module, "time", SimpleNamespace(time=lambda: _SERVER_FRAMES_TS))


# ---------------------------------------------------------------------------
# snapshot branch
# ---------------------------------------------------------------------------

FULL_SNAPSHOT_MSG: dict[str, Any] = {
    "screen": "SCREEN-VAL",
    "cursor": {"x": 7, "y": 9},
    "cols": 132,
    "rows": 55,
    "screen_hash": "HASH-VAL",
    "cursor_at_end": False,
    "has_trailing_space": True,
    "prompt_detected": {"kind": "shell"},
    "raw_tail": "RAWTAIL-VAL",
    "chunks_read": 42,
    "bytes_read": 4096,
    "ts": 123456.5,
}


def test_full_snapshot_message_maps_every_field_exactly() -> None:
    """A fully-populated snapshot msg with a distinctive value on every key.

    Kills 47 survived mutants that rename/mistype a key read from ``msg``, or
    replace a value with a hardcoded ``None``/literal, or swap a `_safe_int`
    ``min_val``/default that only matters once the real value is in range:
    12, 13, 14, 15, 16, 17, 18, 19, 22, 31, 44, 49, 53, 54, 61, 67, 68, 69,
    72, 78, 79, 80, 83, 84, 86, 88, 89, 92, 96, 97, 99, 100, 102, 104, 105,
    108, 114, 115, 116, 118, 124, 125, 126, 135, 139, 140, 141.
    """
    expected = make_snapshot_frame(
        screen="SCREEN-VAL",
        cursor={"x": 7, "y": 9},
        cols=132,
        rows=55,
        screen_hash="HASH-VAL",
        cursor_at_end=False,
        has_trailing_space=True,
        prompt_detected={"kind": "shell"},
        raw_tail="RAWTAIL-VAL",
        chunks_read=42,
        bytes_read=4096,
        ts=123456.5,
    )

    assert _build_worker_frame("snapshot", dict(FULL_SNAPSHOT_MSG)) == expected


def test_minimal_snapshot_message_pins_every_default() -> None:
    """An empty snapshot msg pins every ``.get(key, default)`` fallback.

    Kills 25 survived mutants that swap a hardcoded default value (a wrong
    default string/bool/int, or a ``min_val`` relaxed to ``None``) that is
    only observable when the key is *absent* -- with the key present
    (``test_full_snapshot_message_maps_every_field_exactly`` above), the
    default is never consulted: 37, 39, 42, 50, 52, 55, 56, 57, 58, 59, 60,
    62, 70, 73, 81, 85, 87, 90, 91, 93, 94, 95, 98, 106.

    Also kills mutmut 136 (``ts=_safe_float(msg.get("ts"), None)``): with
    ``ts`` absent, the original reports ``WORKER_TS`` (its own default-arg
    ``time.time()`` call); the mutant's dropped default falls through to
    ``make_snapshot_frame``'s ``ts is None`` fallback one frame later, which
    -- under ``_frozen_clocks`` -- reports the different ``_CORE_FRAMES_TS``
    instead.
    """
    expected = make_snapshot_frame(
        screen="",
        cursor={"x": 0, "y": 0},
        cols=80,
        rows=25,
        screen_hash="",
        cursor_at_end=True,
        has_trailing_space=False,
        prompt_detected=None,
        raw_tail=None,
        chunks_read=None,
        bytes_read=None,
        ts=WORKER_TS,
    )

    assert _build_worker_frame("snapshot", {}) == expected


_SNAPSHOT_COLS_ROWS_CASES: list[tuple[str, dict[str, Any], int, int]] = [
    ("cols_zero_falls_back_to_default", {"cols": 0}, 80, 25),
    ("rows_zero_falls_back_to_default", {"rows": 0}, 80, 25),
    ("cols_non_numeric_falls_back_to_default", {"cols": "abc"}, 80, 25),
    ("cols_numeric_string_is_coerced", {"cols": "40"}, 40, 25),
    ("rows_numeric_string_is_coerced", {"rows": "10"}, 80, 10),
    ("cols_at_the_min_val_boundary_is_kept", {"cols": 1}, 1, 25),
    ("rows_at_the_min_val_boundary_is_kept", {"rows": 1}, 80, 1),
]


@pytest.mark.parametrize(
    ("case_id", "msg_override", "expected_cols", "expected_rows"),
    _SNAPSHOT_COLS_ROWS_CASES,
    ids=[c[0] for c in _SNAPSHOT_COLS_ROWS_CASES],
)
def test_snapshot_cols_rows_boundary_and_coercion(
    case_id: str, msg_override: dict[str, Any], expected_cols: int, expected_rows: int
) -> None:
    """``cols``/``rows`` go through ``_safe_int(val, default, min_val=1)``.

    - ``cols_zero_falls_back_to_default`` / ``rows_zero_falls_back_to_default``
      kill mutmut 63, 66 / 74, 77 (``min_val=1`` relaxed to ``min_val=None``,
      which would let ``0`` -- below the real floor -- through unchanged).
    - ``cols_at_the_min_val_boundary_is_kept`` / ``rows_at_the_min_val_boundary_is_kept``
      kill mutmut 71 / 82 (``min_val=1`` tightened to ``min_val=2``, which
      would reject the in-range boundary value ``1`` and substitute the
      default instead).
    - ``cols_non_numeric_falls_back_to_default``,
      ``cols_numeric_string_is_coerced`` and ``rows_numeric_string_is_coerced``
      pin ``_safe_int``'s own coercion (already covered by mutants killed
      above; kept here as direct regression coverage for the numeric-string
      and non-numeric-string paths through this call site specifically).
    """
    expected = make_snapshot_frame(
        screen="",
        cursor={"x": 0, "y": 0},
        cols=expected_cols,
        rows=expected_rows,
        screen_hash="",
        cursor_at_end=True,
        has_trailing_space=False,
        prompt_detected=None,
        raw_tail=None,
        chunks_read=None,
        bytes_read=None,
        ts=WORKER_TS,
    )

    assert _build_worker_frame("snapshot", dict(msg_override)) == expected


# ---------------------------------------------------------------------------
# analysis branch
# ---------------------------------------------------------------------------


def test_full_analysis_message_maps_every_field_exactly() -> None:
    """A fully-populated analysis msg with a distinctive value on every key.

    Kills 26 survived mutants: the branch guard itself (142: ``mtype ==
    "analysis"`` weakened to ``!=``; 143, 144: the ``"analysis"`` literal
    mangled/upper-cased) plus every key-rename/hardcoded-value mutation on
    ``formatted``/``raw``/``ts`` and the whole-call-replaced-with-``None``
    mutants: 146, 147, 148, 152, 153, 154, 155, 156, 157, 158, 159, 161,
    163, 164, 166, 167, 168, 169, 171, 172, 173, 174, 175.
    """
    msg = {"formatted": "FORMATTED-VAL", "raw": {"r": 1}, "ts": 999.5}
    expected = make_analysis_frame(formatted="FORMATTED-VAL", raw={"r": 1}, ts=999.5)

    assert _build_worker_frame("analysis", msg) == expected


def test_minimal_analysis_message_pins_every_default() -> None:
    """An empty analysis msg pins the ``formatted``/``raw``/``ts`` defaults.

    Kills mutmut 160, 162, 165 (the ``formatted`` default changed from
    ``""`` to a non-empty literal, or ``raw``'s implicit ``None`` masked by
    a renamed key) -- observable only when the key is absent, since with the
    key present (test above) the default is never consulted.

    Also kills mutmut 170 (``ts=_safe_float(msg.get("ts"), None)`` in this
    branch): the same reasoning as mutmut 136 above, but the dropped default
    falls through to ``make_analysis_frame``'s *own* ``ts is None`` fallback
    -- which lives in ``provide.uterm.server.bridge.frames``, not the core
    ``provide.uterm.frames`` -- so under ``_frozen_clocks`` it reports
    ``_SERVER_FRAMES_TS`` instead of ``WORKER_TS``.
    """
    expected = make_analysis_frame(formatted="", raw=None, ts=WORKER_TS)

    assert _build_worker_frame("analysis", {}) == expected


# ---------------------------------------------------------------------------
# status / unknown-mtype fallthrough
#
# ``_build_worker_frame`` has no explicit "status" check: any mtype that
# isn't "snapshot" and isn't "analysis" falls through to
# ``coerce_worker_status_frame(msg)`` (the module docstring calls this "the
# only remaining builder branch (filtered upstream)" -- upstream filtering,
# not this function, is what keeps "status" the only such value in
# practice).
# ---------------------------------------------------------------------------


def test_full_status_message_passes_through_unmodified() -> None:
    """``coerce_worker_status_frame`` only ``setdefault``s ``type``/``ts``.

    With both already present in ``msg``, the returned frame is ``msg``
    unchanged (as a new dict). Kills mutmut 177 (the whole
    ``coerce_worker_status_frame(msg)`` call replaced with ``None``), 178
    (the outer ``cast``'s type-string argument dropped entirely, leaving a
    single-argument call that raises ``TypeError``), 179 (the value argument
    dropped instead, same ``TypeError``), and 183 (``coerce_worker_status_frame(None)``
    -- passed ``None`` instead of ``msg``, which raises inside ``dict(None)``).
    """
    msg = {"foo": "bar", "type": "should_be_overridden_by_setdefault_NOT", "ts": 55.5}

    assert _build_worker_frame("status", dict(msg)) == msg


def test_minimal_status_message_gets_type_and_ts_defaults() -> None:
    """An empty status msg gets ``type``/``ts`` filled in by ``setdefault``.

    Same mutants as ``test_full_status_message_passes_through_unmodified``
    also fail this case; kept as the natural default-pinning counterpart.

    Unlike the snapshot/analysis branches, ``_build_worker_frame`` never
    calls ``time.time()`` itself for "status" -- it hands ``msg`` straight
    to ``coerce_worker_status_frame``, whose own ``frame.setdefault("ts",
    time.time())`` lives in ``provide.uterm.server.bridge.frames``. So the
    expected default here is ``_SERVER_FRAMES_TS``, not ``WORKER_TS``.
    """
    assert _build_worker_frame("status", {}) == {"type": "status", "ts": _SERVER_FRAMES_TS}


def test_an_unrecognized_mtype_still_falls_through_to_status() -> None:
    """Documents the fallthrough itself, not just its downstream call.

    Kills mutmut 142 from this angle too: with the ``if mtype == "analysis"``
    guard weakened to ``!=``, an unrecognized mtype like ``"unknown"`` would
    wrongly take the analysis branch (reading ``formatted``/``raw`` off a msg
    that has neither) instead of falling through to the status builder. Same
    ``_SERVER_FRAMES_TS`` default as the minimal-status case above, for the
    same reason.
    """
    assert _build_worker_frame("unknown", {"anything": 1}) == {
        "anything": 1,
        "type": "status",
        "ts": _SERVER_FRAMES_TS,
    }


# ---------------------------------------------------------------------------
# _opt_int
#
# The int/zero/None/non-int-string/float/bool cases are already driven
# directly against every value this module's own docstring calls out (see
# ``test_snapshot_ingest_counters.py::TestOptInt``); this only adds the one
# input shape not covered there.
# ---------------------------------------------------------------------------


def test_opt_int_preserves_a_negative_int() -> None:
    """A negative reading is still a real ``int``, not "absent"."""
    assert _opt_int(-5) == -5
