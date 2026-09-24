#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``websockets_worker._handle_worker_hello`` and
``websockets_worker._dispatch_worker_frame``.

Kill-suite only. Under mutmut 3.8 (2026-09-23):

- ``_handle_worker_hello`` had 135 ``no tests`` mutants: nothing in the
  mutation test selection ever calls the real function body at all (existing
  suites either exercise the ``ws_worker_term`` recv loop end-to-end through a
  real hub, which never reaches every branch here, or don't touch this helper
  directly), so every one of the 135 must be killed by a test that calls the
  real function directly.
- ``_dispatch_worker_frame`` had 21 ``survived`` mutants (the ``analysis`` and
  ``status`` branches' ``hub.broadcast``/``hub.append_event`` calls) plus 13
  ``timeout`` mutants (the owned-snapshot branch's
  ``hub.commit_snapshot_event``/``hub.broadcast`` calls, which hung real tests
  past mutmut's limit -- almost certainly because those tests drive a real
  hub/lock, and a mutated argument there stalls on real I/O instead of failing
  fast). Every fake method below is a synchronous return with no lock, no
  network and no unbounded await, so a mutant is refuted or not in well under
  a millisecond -- there is nothing left to hang on.

``websockets_worker.logger`` is a ``provide.telemetry`` ``_TraceWrapper``
around a structlog ``BoundLogger``, not a stdlib ``logging.Logger`` -- pytest's
``caplog`` fixture hooks the stdlib logging tree and does not reliably capture
it. Every logging assertion below instead monkeypatches
``websockets_worker.logger`` with a strict recorder that stores the exact
positional-argument tuple each ``warning``/``info`` call receives (format
string included), which is the only way to catch a swapped, renamed, dropped,
or reordered logging argument -- a dropped argument shows up directly as a
shorter tuple.

``hub.set_worker_hello``/``hub.broadcast_hijack_state`` (hello) and
``hub.commit_snapshot_event``/``hub.broadcast``/``hub.append_event``
(dispatch) are modelled with the exact keyword-only signatures of the real
``TermHub`` service methods (see
``packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/connection.py``,
``.../hub/router_broadcast.py`` and ``.../hub/router_impl.py``), so a mutant
that drops a required positional argument raises ``TypeError`` from arity
alone, before any assertion even runs.

``websocket.send_text``/``websocket.close`` in the protocol-mismatch branch
run inside ``with suppress(Exception):`` in the real code, so an assertion
raised from inside those fakes would itself be swallowed by that block --
every fake here only records, and every assertion happens in the test body
after the call returns. ``suppress(Exception)`` is itself directly exercised
(mutmut_74, ``suppress(None)``) by a dedicated fake ``send_text`` that raises,
which the real code must swallow and the mutant cannot (``issubclass(exc,
(None,))`` raises ``TypeError`` inside ``contextlib.suppress.__exit__``).

Every kill claimed below (and every documented equivalent) was confirmed by
loading mutmut's own extracted mutant bodies from
``scratchpad/mutants_websockets_worker/.../websockets_worker.py`` and running
them against these exact fakes in a throwaway driver script (deleted after
use, not part of this file) -- including a ~1000-input brute-force sweep over
the ``protocol``/``protocol_version``/``input_mode`` fields for the eight
disputed mutants below, which found no distinguishing input for any of them.

Documented equivalents (``_handle_worker_hello``, all given
``MIN_PROTOCOL_VERSION == MAX_PROTOCOL_VERSION == 1`` today):

- mutmut_12, mutmut_15: drop the ``min_val=1`` clamp from the ``protocol.min``
  ``_safe_int`` call. Unobservable: ``negotiate_protocol_version`` itself does
  ``lo = max(int(client_min), MIN_PROTOCOL_VERSION)``, re-clamping *any*
  ``client_min <= 1`` (clamped or not) to exactly ``1``. Whatever the local
  clamp does or doesn't do to a sub-1 value, the outer function erases the
  difference.
- mutmut_19: raises the same call's ``min_val`` from ``1`` to ``2``. The only
  integer where old (``>=1``) and new (``>=2``) clamping disagree is exactly
  ``1``, and clamping ``1`` falls back to ``MIN_PROTOCOL_VERSION == 1`` --
  the same value ``1`` would have been anyway. No integer input can tell the
  two apart.
- mutmut_30: the same shift (``min_val=1`` to ``2``) on the ``protocol.max``
  call, with ``MAX_PROTOCOL_VERSION == 1`` playing the identical role.
- mutmut_23, mutmut_26 are the *not*-equivalent siblings of 12/15 on the
  ``protocol.max`` side, and are genuinely killed (see ``P4_min5_max0``
  below): ``negotiate_protocol_version`` only re-clamps ``client_max`` from
  *above* (``hi = min(int(client_max), MAX_PROTOCOL_VERSION)``), never from
  below, so an unclamped low/negative ``client_max`` really does reach ``hi``
  unmodified.
- mutmut_42: the legacy ``_safe_int(msg.get("protocol_version"), 0)`` call's
  default, raised from ``0`` to ``1``. Its only consumer is
  ``_client_min = _legacy_v if _legacy_v >= 1 else 1``, and both ``0`` and
  ``1`` route to the *same* final value ``1`` there (``0`` via the ``else``,
  ``1`` via the ``if`` returning itself) -- the default's value never
  survives the ternary.
- mutmut_46, mutmut_47: widen that same ternary's ``_legacy_v >= 1`` guard to
  ``> 1`` / ``>= 2``. The only value where the old and new guards disagree is
  ``_legacy_v == 1``, and at that exact point the ``if`` branch would return
  ``_legacy_v`` (i.e. ``1``) -- identical to the ``else`` branch's literal
  ``1``. Again the boundary value coincides with the fallback value, so no
  input distinguishes them.
- mutmut_53: the else-branch (no ``protocol`` dict, no ``protocol_version``)
  default ``_client_max = 1``, raised to ``2``. That branch always sets
  ``_client_min = 1`` too (untouched by this mutant), and
  ``negotiate_protocol_version`` clamps ``hi = min(int(client_max),
  MAX_PROTOCOL_VERSION)`` to ``<= 1`` regardless of whether the raw value is
  ``1`` or ``2`` -- so ``_selected`` and everything downstream of it comes out
  identical, and the raw ``_client_max`` is only ever echoed on the mismatch
  path, which this branch can never reach (mismatch needs ``_client_min > 1``,
  and this branch's ``_client_min`` is a fixed ``1``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from provide.uterm.bridge.contracts import MAX_PROTOCOL_VERSION, MIN_PROTOCOL_VERSION
from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.routes import websockets_worker
from provide.uterm.server.bridge.routes.websockets_worker import (
    _dispatch_worker_frame,
    _handle_worker_hello,
)

# No test below may be allowed to hang, no matter what a mutant does to an
# awaited chain it doesn't fully control (e.g. an argument-dropping mutation
# reaching a different collaborator, or coverage-based test selection running
# this body against a fake it wasn't written against). Every call to the two
# functions under test is bounded by this timeout; it never fires in the
# healthy case (it races a timer against an already-fast call), but it turns
# any hypothetical hang into a fast, clearly-labelled failure instead of a
# stalled mutation-gate leg.
_CALL_TIMEOUT_S = 2.0


async def _await_hello(hub: Any, ws: Any, worker_id: str, msg: dict[str, Any]) -> bool:
    return await asyncio.wait_for(_handle_worker_hello(hub, ws, worker_id, msg), timeout=_CALL_TIMEOUT_S)


async def _await_dispatch(
    hub: Any, worker_id: str, mtype: str, frame: dict[str, Any], *, expected_worker: Any = None
) -> None:
    await asyncio.wait_for(
        _dispatch_worker_frame(hub, worker_id, mtype, frame, expected_worker=expected_worker),
        timeout=_CALL_TIMEOUT_S,
    )


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeLogger:
    """Records the exact positional-argument tuple of every call.

    See the module docstring: ``websockets_worker.logger`` is not a stdlib
    logger, so this replaces it wholesale rather than relying on ``caplog``.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[Any, ...]] = []
        self.infos: list[tuple[Any, ...]] = []

    def warning(self, *args: Any) -> None:
        self.warnings.append(args)

    def info(self, *args: Any) -> None:
        self.infos.append(args)


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> _FakeLogger:
    log = _FakeLogger()
    monkeypatch.setattr(websockets_worker, "logger", log)
    return log


# ---------------------------------------------------------------------------
# _handle_worker_hello
# ---------------------------------------------------------------------------

WORKER = "hello-worker"


class _HelloWebSocket:
    """The worker's WebSocket. ``send_text``/``close`` run inside
    ``with suppress(Exception):`` in the real code, so nothing here raises
    to signal a problem -- every call is just recorded and checked by the
    test after ``_handle_worker_hello`` returns."""

    def __init__(self, *, raise_on_send: BaseException | None = None) -> None:
        self.sent: list[str] = []
        self.closes: list[tuple[int, str | None]] = []
        self._raise_on_send = raise_on_send

    async def send_text(self, data: str) -> None:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        assert isinstance(data, str), f"send_text got {data!r}"
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closes.append((code, reason))


class _HelloHub:
    """Mirrors ``hub/connection.py``'s ``set_worker_hello`` and
    ``hub/lease.py``'s ``broadcast_hijack_state`` signatures exactly, so a
    mutant that drops a required positional argument raises ``TypeError``
    from arity alone."""

    def __init__(self, *, mode_applied: bool = True) -> None:
        self._mode_applied = mode_applied
        self.hello_calls: list[tuple[str, str, int | None]] = []
        self.broadcasts: list[str] = []

    async def set_worker_hello(self, worker_id: str, mode: str, protocol_version: int | None = None) -> bool:
        self.hello_calls.append((worker_id, mode, protocol_version))
        return self._mode_applied

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcasts.append(worker_id)


def _mismatch_frame(client_min: int, client_max: int) -> str:
    return encode_control_frame(
        {
            "type": "error",
            "reason": "protocol_mismatch",
            "client_min": client_min,
            "client_max": client_max,
            "server_min": MIN_PROTOCOL_VERSION,
            "server_max": MAX_PROTOCOL_VERSION,
        }
    )


_MISMATCH_WARNING_FMT = "worker_hello_protocol_mismatch worker_id=%s client=[%d,%d] server=[%d,%d]"
_HELLO_INFO_FMT = "worker_hello worker_id=%s input_mode=%s protocol_selected=%d applied=%s"
_INVALID_MODE_WARNING_FMT = (
    "worker_hello_invalid_mode worker_id=%s input_mode=%r — expected 'hijack' or 'open', ignoring"
)


async def test_hijack_mode_negotiates_default_protocol_and_broadcasts(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_1-4 (``_hello_mode`` lookup forced/mis-keyed to ``None``,
    which would skip the hub entirely), mutmut_50-52 (else-branch
    ``_client_min``/``_client_max`` literals), mutmut_54-59 (the
    ``negotiate_protocol_version`` call and the ``is None`` mismatch check),
    mutmut_101-103 (the ``"hijack"`` membership check), mutmut_106-113 (every
    argument of the ``set_worker_hello``/``broadcast_hijack_state`` calls,
    including arity drops), and mutmut_114-125 (every argument, including
    arity drops, of the success info log)."""
    hub = _HelloHub(mode_applied=True)
    ws = _HelloWebSocket()
    msg = {"input_mode": "hijack"}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is False
    assert hub.hello_calls == [(WORKER, "hijack", 1)]
    assert hub.broadcasts == [WORKER]
    assert fake_logger.infos == [(_HELLO_INFO_FMT, WORKER, "hijack", 1, True)]
    assert fake_logger.warnings == []
    assert (ws.sent, ws.closes) == ([], [])


async def test_open_mode_is_the_other_half_of_the_membership_check(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_104-105 (``"open"`` literal mutations in the membership
    tuple): with ``_hello_mode == "hijack"`` those two mutants are
    unobservable (the tuple's first element is untouched), so ``"open"``
    itself must be driven through."""
    hub = _HelloHub(mode_applied=True)
    ws = _HelloWebSocket()
    msg = {"input_mode": "open"}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is False
    assert hub.hello_calls == [(WORKER, "open", 1)]
    assert hub.broadcasts == [WORKER]
    assert fake_logger.infos == [(_HELLO_INFO_FMT, WORKER, "open", 1, True)]
    assert fake_logger.warnings == []
    assert (ws.sent, ws.closes) == ([], [])


async def test_an_unrecognised_mode_warns_and_never_touches_the_hub(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_126 (the ``elif _hello_mode is not None`` flip, which
    would skip this branch for a non-``None`` invalid mode), mutmut_127-134
    (every argument, including arity drops and literal-text mutations, of
    the invalid-mode warning) and mutmut_135 (the final ``return False``
    flipped to ``True``)."""
    hub = _HelloHub()
    ws = _HelloWebSocket()
    msg = {"input_mode": "bogus"}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is False
    assert (hub.hello_calls, hub.broadcasts) == ([], [])
    assert fake_logger.infos == []
    assert fake_logger.warnings == [(_INVALID_MODE_WARNING_FMT, WORKER, "bogus")]
    assert (ws.sent, ws.closes) == ([], [])


async def test_an_explicit_protocol_block_above_range_is_a_mismatch(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_5-8 (``_proto_block`` forced/mis-keyed to ``None``, which
    would fall through to the ``{1,1}`` default and turn this mismatch into a
    success), mutmut_9-10/16-18 (the ``protocol.min`` ``_safe_int`` call's
    ``val`` argument), mutmut_13-14 (that call's arity), mutmut_20-21/27-29
    (the mirrored ``protocol.max`` mutations), mutmut_24-25 (that call's
    arity), and the full mismatch branch: mutmut_60-73 (every warning-log
    argument and the format string, including arity drops), mutmut_75-92
    (every key and value of the sent error frame, including the frame being
    dropped to ``None`` entirely -- swallowed by ``suppress`` and observed as
    an empty ``ws.sent``), mutmut_93-99 (every ``websocket.close`` argument,
    including its default-vs-explicit ``code``/``reason``) and mutmut_100
    (``return True`` flipped to ``False``)."""
    hub = _HelloHub()
    ws = _HelloWebSocket()
    msg = {"protocol": {"min": 5, "max": 5}}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is True
    assert (hub.hello_calls, hub.broadcasts) == ([], [])
    assert fake_logger.infos == []
    assert fake_logger.warnings == [(_MISMATCH_WARNING_FMT, WORKER, 5, 5, MIN_PROTOCOL_VERSION, MAX_PROTOCOL_VERSION)]
    assert ws.sent == [_mismatch_frame(5, 5)]
    assert ws.closes == [(1002, "protocol_mismatch")]


async def test_protocol_max_alone_defaults_min_and_succeeds(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_11: the ``protocol.min`` ``_safe_int`` call's *default*
    argument (``MIN_PROTOCOL_VERSION``) forced to ``None``. That default is
    only ever consulted when ``protocol.min`` itself is absent, so this test
    omits it. The real code computes ``_client_min = 1`` and negotiates a
    quiet success; the mutant's ``_safe_int(None, None, min_val=1)`` returns
    ``None`` (its own except-clause swallows ``int(None)`` and returns the
    ``None`` default), and the ``None`` then reaches
    ``negotiate_protocol_version``'s own unguarded ``int(client_min)``, which
    raises -- an uncaught exception where the real code returns quietly."""
    hub = _HelloHub()
    ws = _HelloWebSocket()
    msg = {"protocol": {"max": 5}}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is False
    assert (hub.hello_calls, hub.broadcasts) == ([], [])
    assert (fake_logger.infos, fake_logger.warnings) == ([], [])
    assert (ws.sent, ws.closes) == ([], [])


async def test_protocol_min_alone_defaults_max_and_mismatches(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_22: the ``protocol.max`` ``_safe_int`` call's default
    argument (``MAX_PROTOCOL_VERSION``) forced to ``None``, by the same
    reasoning as mutmut_11 above but on the ``max`` side. The real code
    defaults ``_client_max`` to ``1`` and reaches the ordinary mismatch path
    (``_client_min=5`` alone is already above range); the mutant's ``None``
    default reaches ``negotiate_protocol_version``'s unguarded
    ``int(client_max)`` and raises."""
    hub = _HelloHub()
    ws = _HelloWebSocket()
    msg = {"protocol": {"min": 5}}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is True
    assert fake_logger.warnings == [(_MISMATCH_WARNING_FMT, WORKER, 5, 1, MIN_PROTOCOL_VERSION, MAX_PROTOCOL_VERSION)]
    assert ws.sent == [_mismatch_frame(5, 1)]
    assert ws.closes == [(1002, "protocol_mismatch")]


async def test_a_low_protocol_max_is_not_floored_without_its_clamp(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_23 and mutmut_26: dropping the ``protocol.max``
    ``_safe_int`` call's ``min_val=1`` floor. Unlike the mirrored ``min``-side
    mutants (documented as equivalent above), this one is genuinely
    observable: ``negotiate_protocol_version`` only clamps ``client_max`` from
    *above*, never from below, so an unfloored ``protocol.max: 0`` reaches
    ``hi`` as a raw ``0`` instead of the real code's floored ``1`` -- visible
    directly in the echoed ``client_max`` field of the mismatch frame/log
    (``protocol.min: 5`` alone already forces the mismatch, independent of
    this value, so the *only* thing this scenario probes is whether
    ``client_max`` got floored)."""
    hub = _HelloHub()
    ws = _HelloWebSocket()
    msg = {"protocol": {"min": 5, "max": 0}}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is True
    assert fake_logger.warnings == [(_MISMATCH_WARNING_FMT, WORKER, 5, 1, MIN_PROTOCOL_VERSION, MAX_PROTOCOL_VERSION)]
    assert ws.sent == [_mismatch_frame(5, 1)]
    assert ws.closes == [(1002, "protocol_mismatch")]


async def test_legacy_protocol_version_five_negotiates_and_mismatches(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_31-35 (the ``elif "protocol_version" in msg`` key-lookup
    and the legacy ``_safe_int`` call's ``val`` argument), mutmut_37-41
    (that call's arity and mis-keyed lookups), mutmut_43-44 (``_client_min``
    forced to ``None``/always taking the ``else`` branch) and mutmut_49
    (``_client_max = _client_min`` forced to ``None``). A legacy value of
    ``5`` is unambiguously above range, so any of these collapsing the value
    back down to the ``{1,1}`` default flips a mismatch into a quiet
    success."""
    hub = _HelloHub()
    ws = _HelloWebSocket()
    msg = {"protocol_version": 5}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is True
    assert (hub.hello_calls, hub.broadcasts) == ([], [])
    assert fake_logger.warnings == [(_MISMATCH_WARNING_FMT, WORKER, 5, 5, MIN_PROTOCOL_VERSION, MAX_PROTOCOL_VERSION)]
    assert ws.sent == [_mismatch_frame(5, 5)]
    assert ws.closes == [(1002, "protocol_mismatch")]


async def test_legacy_protocol_version_zero_clamps_to_one_and_succeeds(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_45 (``_legacy_v if (_legacy_v >= 1) or True else 1`` --
    always takes the ``if`` branch, which for ``_legacy_v=0`` would use ``0``
    instead of the real code's floored ``1``) and mutmut_48 (the ``else``
    branch's literal ``1`` raised to ``2``). Both would turn this legitimate
    ``0`` (below range, correctly floored to the default ``1``) into an
    above-range value, flipping a quiet success into a mismatch."""
    hub = _HelloHub()
    ws = _HelloWebSocket()
    msg = {"protocol_version": 0, "input_mode": "hijack"}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is False
    assert hub.hello_calls == [(WORKER, "hijack", 1)]
    assert hub.broadcasts == [WORKER]
    assert fake_logger.warnings == []


async def test_an_explicit_none_protocol_version_still_defaults_safely(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_36: the legacy ``_safe_int(msg.get("protocol_version"),
    0)`` call's *default* argument forced to ``None``. ``"protocol_version"
    in msg`` is true here even though its value is ``None``, so ``val`` is
    ``None`` and the default is actually consulted. The real code falls back
    to ``0`` (then floored to ``_client_min=1`` by the ternary) and succeeds
    quietly; the mutant's ``_safe_int(None, None)`` returns ``None`` (its own
    except-clause swallows ``int(None)``), and ``None >= 1`` then raises
    ``TypeError`` before ``negotiate_protocol_version`` is even reached."""
    hub = _HelloHub()
    ws = _HelloWebSocket()
    msg = {"protocol_version": None}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is False
    assert (hub.hello_calls, hub.broadcasts) == ([], [])
    assert (fake_logger.infos, fake_logger.warnings) == ([], [])
    assert (ws.sent, ws.closes) == ([], [])


async def test_suppress_exception_swallows_a_failed_send_and_skips_close(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_74 (``with suppress(Exception):`` mutated to
    ``suppress(None)``). Forces ``websocket.send_text`` to raise inside the
    guarded block: the real code's ``suppress(Exception)`` swallows it and
    falls through to ``return True`` without ever reaching ``close()``; the
    mutant's ``suppress(None)`` cannot swallow anything -- ``contextlib.
    suppress.__exit__`` does ``issubclass(exctype, self._exceptions)`` with
    ``self._exceptions == (None,)``, which itself raises ``TypeError``
    (``None`` is not a class), propagating out of ``_handle_worker_hello``
    instead of returning."""
    hub = _HelloHub()
    ws = _HelloWebSocket(raise_on_send=RuntimeError("boom"))
    msg = {"protocol": {"min": 5, "max": 5}}

    result = await _await_hello(hub, ws, WORKER, msg)

    assert result is True
    assert (ws.sent, ws.closes) == ([], [])


# ---------------------------------------------------------------------------
# _dispatch_worker_frame
# ---------------------------------------------------------------------------

DWID = "dispatch-worker"


class _DispatchHub:
    """Mirrors ``hub/router_impl.py``'s ``commit_snapshot_event``,
    ``hub/router_broadcast.py``'s ``broadcast`` and ``hub/router_impl.py``'s
    ``append_event`` signatures exactly (all keyword-only after the first two
    positionals), so a mutant that drops a required positional raises
    ``TypeError`` from arity alone, and everything else is caught by exact
    argument equality. No method touches a lock, a socket or an unbounded
    await -- every call returns immediately, which is what turns the 13
    ``timeout`` mutants below into fast failures instead of hangs."""

    def __init__(self) -> None:
        self.commit_calls: list[tuple[str, dict[str, Any], Any]] = []
        self.broadcast_calls: list[tuple[str, dict[str, Any], Any, int | None]] = []
        self.append_calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def commit_snapshot_event(
        self, worker_id: str, snapshot: dict[str, Any], *, expected_worker: Any = None
    ) -> dict[str, Any] | None:
        self.commit_calls.append((worker_id, snapshot, expected_worker))
        return {"event_seq": 7, "marker": "committed"}

    async def broadcast(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        expected_worker: Any = None,
        expected_event_seq: int | None = None,
    ) -> None:
        self.broadcast_calls.append((worker_id, msg, expected_worker, expected_event_seq))

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.append_calls.append((worker_id, event_type, data))
        return {}


async def test_an_owned_snapshot_commit_broadcasts_with_the_fenced_seq() -> None:
    """Kills mutmut_14-20 (every argument, including arity drops, of the
    ``commit_snapshot_event`` call), mutmut_21 (the ``is not None`` guard
    flipped, which would skip the broadcast even though the fake always
    returns a committed dict), mutmut_26-27 (the owned ``broadcast`` call's
    ``worker_id``/``owned_commit`` positionals dropped -- both arity errors
    against a fake whose ``msg`` parameter has no default) and mutmut_30-32
    (the ``expected_event_seq=int(owned_commit["event_seq"])`` expression:
    forced to ``int(None)``, or the dict subscripted with a wrong-case key
    that raises ``KeyError`` against this fake's ``{"event_seq": 7, ...}``)."""
    hub = _DispatchHub()
    frame = {"data": "owned-snapshot"}
    sentinel = object()

    await _await_dispatch(hub, DWID, "snapshot", frame, expected_worker=sentinel)

    assert hub.commit_calls == [(DWID, frame, sentinel)]
    assert hub.broadcast_calls == [(DWID, {"event_seq": 7, "marker": "committed"}, sentinel, 7)]
    assert hub.append_calls == []


async def test_analysis_frames_are_broadcast_and_never_appended() -> None:
    """Kills mutmut_33-35 (the ``elif mtype == "analysis"`` check negated or
    mis-cased, which falls through to the ``status`` ``else`` branch and
    would spuriously call ``append_event``) and mutmut_36-39 (every argument,
    including arity drops, of that branch's ``broadcast`` call)."""
    hub = _DispatchHub()
    frame = {"formatted": "analysis output"}

    await _await_dispatch(hub, DWID, "analysis", frame)

    assert hub.commit_calls == []
    assert hub.broadcast_calls == [(DWID, frame, None, None)]
    assert hub.append_calls == []


async def test_status_frames_are_broadcast_and_appended_as_worker_status() -> None:
    """Kills mutmut_40-43 (every argument, including arity drops, of the
    ``status`` branch's ``broadcast`` call) and mutmut_44-53 (every argument
    of the ``append_event`` call, including arity drops -- which shift
    ``"worker_status"``/``{"status": frame}`` into the wrong parameter
    positions -- and the ``"worker_status"`` / inner ``"status"`` key
    literals mis-cased or renamed)."""
    hub = _DispatchHub()
    frame = {"cpu": 1}

    await _await_dispatch(hub, DWID, "status", frame)

    assert hub.commit_calls == []
    assert hub.broadcast_calls == [(DWID, frame, None, None)]
    assert hub.append_calls == [(DWID, "worker_status", {"status": frame})]
