#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``websockets_impl._ws_worker_term``'s early
section: the bearer-token auth gate, the connect-time bookkeeping
(register/touch/span/log/hijack-notify/broadcast/cleanup-task), and the
receive loop up through the ``DataChunk`` (terminal-data) branch.

Kill-suite only, targeting a specific surviving/timeout mutant batch (see
``scratchpad/b2diffs/ws_worker_A.txt`` mutmut numbers, referenced below as
``mutmut_N``). The tail of the function -- the ``worker_hello``/control-frame
dispatch, the outer exception handlers, and the disconnect ``finally`` block
-- is out of scope here (covered by sibling suites); the fakes below give it
just enough to complete cleanly (``deregister_worker`` defaults to
``(False, False)`` so the disconnect broadcast branch is never entered).

``websockets_impl.logger`` is a ``provide.telemetry`` wrapper, not a stdlib
``logging.Logger``, so ``caplog`` does not reliably capture it (see the
sibling ``test_worker_hello_and_dispatch_mutation_killing.py`` for the same
observation). ``_FakeLogger`` replaces it wholesale and records the exact
positional-argument tuple of every call, which is the only way to catch a
swapped, dropped, or reworded logging argument.

``_FakeHub`` mirrors the real ``TermHub`` service methods' signatures at the
call sites actually exercised here (see
``packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/*.py``),
so a mutant dropping a required positional argument raises ``TypeError`` from
arity alone, and every other argument mutation is caught by exact tuple
equality against the recorded call.

Every timing-sensitive test uses a *value* difference (a controlled
``asyncio.wait_for`` race between a fixed idle-timeout and a fixed receive
delay) rather than an open-ended hang, so both the real code and every mutant
finish in well under 100ms; the whole suite is wrapped in a 2s
``asyncio.wait_for`` per call as a backstop, never expected to fire.

Documented equivalents (not tested):

- mutmut_10: ``websocket.headers.get("authorization", "XXXX")`` -- the
  ``.get`` default is only consulted when the header is absent, and the
  surrounding ternary (`if auth_header.startswith("Bearer ") else ""`) routes
  *any* non-``"Bearer "``-prefixed value to the literal `""` in its `else`
  branch, regardless of what that value was. Double-checked (not just by
  inspection): the mutated default is a fixed string literal, so
  ``"XXXX".startswith("Bearer ")`` is statically ``False`` for every possible
  value mutmut could have put there (it never contains ``"Bearer "`` as a
  prefix), the ``else`` branch discards ``auth_header`` entirely in favour of
  its own literal ``""``, and ``auth_header`` is never read again anywhere
  else in the function -- unlike the ``ts`` case below, there is no second,
  independently-mutable call site that could make the discarded value
  observable.
- mutmut_64: ``hub.notify_hijack_changed(worker_id, enabled=False, )`` (the
  ``owner=None`` keyword dropped) -- the real ``notify_hijack_changed``
  signature is ``(self, worker_id, *, enabled, owner=None)``; the call site
  always passes the literal ``None`` anyway, so dropping the keyword falls
  back to the method's own default of the same value.
- mutmut_71/75/76/77 and mutmut_161/165/166/167: mutations of the first
  argument to ``cast("dict[str, Any]", ...)`` (around
  ``make_worker_connected_frame``/``make_term_frame``). ``typing.cast(typ,
  val)`` is a pure identity function at runtime -- it returns ``val``
  unchanged and never inspects ``typ`` -- so no value the first argument is
  mutated to (``None``, a differently-cased or -spelled string) can change
  what ``hub.broadcast`` receives.
- mutmut_102/125/155: three ``break`` -> ``return`` mutations at the three
  exit points of the ``while True:`` receive loop. In every case the loop is
  the *entire* body of the enclosing ``try:``, with nothing between the loop
  and the ``finally:`` below it, and the ``try/except/finally`` is the last
  statement in the function. Exiting the loop via ``break`` reaches the end
  of the ``try`` body normally, runs ``finally``, and then implicitly returns
  ``None``; ``return`` inside a ``try`` runs the same ``finally`` (Python
  guarantees this) and then explicitly returns ``None``. Both paths run the
  identical ``finally`` block against identical hub/task state and produce
  the identical return value, so no observation distinguishes them.

Note on mutmut_169/171 (``make_term_frame(event.data, ts=None)`` /
``make_term_frame(event.data, )``): an earlier pass judged these equivalent
by freezing the *shared* stdlib ``time`` module's ``.time`` attribute in
place, which makes every consumer's ``import time`` see the identical patched
callable -- exactly the setup that hides the difference. ``websockets_impl.py``
and ``provide.uterm.server.bridge.frames`` (where ``make_term_frame`` lives)
each do their own ``import time``, so rebinding each module's own ``time``
**name** to a *different* fake clock (see ``_frozen_clocks`` below) makes
"which call site's ``time.time()`` actually supplied the value" observable:
the real code always reports the outer (``websockets_impl``) clock, since it
computes ``ts`` explicitly and ``make_term_frame`` never reaches its own
``ts is None`` fallback; either mutant reaches that fallback instead and
reports the *inner* (``frames``) clock. Killed below by
``test_data_chunk_touches_broadcasts_and_appends_the_term_event``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from fastapi import WebSocketDisconnect

from provide.uterm.server.bridge import frames as _frames_module
from provide.uterm.server.bridge.frames import make_term_frame
from provide.uterm.server.bridge.routes import websockets_impl
from provide.uterm.server.bridge.routes.websockets_impl import _ws_worker_term

# Every call to the function under test is bounded by this timeout. It never
# fires for the real code or for any mutant exercised below (each scenario
# is designed to resolve in well under 100ms); it exists purely to turn a
# hypothetical hang into a fast, clearly-labelled failure.
_CALL_TIMEOUT_S = 2.0

WORKER_ID = "worker-under-test"

# Two DIFFERENT constants so that, when the DataChunk branch's `ts` argument
# is dropped/forced to `None`, "which module's own `time.time()` actually
# supplied the value" is observable rather than two clocks that happen to
# agree. See the module docstring's note on mutmut_169/171.
_OUTER_TS = 1_000_000.0
_FRAMES_MODULE_TS = 3_000_000.0


async def _run(hub: Any, ws: Any, worker_id: str = WORKER_ID) -> None:
    await asyncio.wait_for(_ws_worker_term(hub, ws, worker_id), timeout=_CALL_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeLogger:
    """Records the exact positional-argument tuple of every call. See the
    module docstring: ``websockets_impl.logger`` is not a stdlib logger."""

    def __init__(self) -> None:
        self.warnings: list[tuple[Any, ...]] = []
        self.infos: list[tuple[Any, ...]] = []
        self.debugs: list[tuple[Any, ...]] = []

    def warning(self, *args: Any) -> None:
        self.warnings.append(args)

    def info(self, *args: Any) -> None:
        self.infos.append(args)

    def debug(self, *args: Any) -> None:
        self.debugs.append(args)


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> _FakeLogger:
    log = _FakeLogger()
    monkeypatch.setattr(websockets_impl, "logger", log)
    return log


class _RecvItem:
    """One scripted ``receive_text()`` outcome: optionally delayed, either a
    text payload or a disconnect."""

    def __init__(self, *, delay: float = 0.0, text: str | None = None, disconnect: bool = False) -> None:
        self.delay = delay
        self.text = text
        self.disconnect = disconnect


class _WorkerWebSocket:
    """The worker's WebSocket. ``close()`` can optionally raise, to drive
    the ``with suppress(Exception):`` blocks that wrap it in the real code --
    every other call here just records and never raises, so an assertion
    failure is never masked by a ``suppress``."""

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        recv: list[_RecvItem] | None = None,
        raise_on_close: BaseException | None = None,
    ) -> None:
        self.headers: dict[str, str] = dict(headers or {})
        self.accepted = 0
        self.closes: list[tuple[int, str | None]] = []
        self._recv = list(recv or [])
        self._raise_on_close = raise_on_close

    async def accept(self) -> None:
        self.accepted += 1

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self._raise_on_close is not None:
            raise self._raise_on_close
        self.closes.append((code, reason))

    async def receive_text(self) -> str:
        item = self._recv.pop(0)
        if item.delay:
            await asyncio.sleep(item.delay)
        if item.disconnect:
            raise WebSocketDisconnect
        assert item.text is not None
        return item.text


class _FakeHub:
    """Mirrors the real ``TermHub`` service methods' signatures exactly at
    the call sites ``_ws_worker_term`` uses, so a mutant that drops a
    required positional argument raises ``TypeError`` from arity alone."""

    def __init__(
        self,
        *,
        worker_token: str | None = None,
        prev_was_hijacked: bool = False,
        is_active: bool = True,
        max_ws_message_bytes: int = 1_048_576,
        ws_idle_timeout_s: float = 30.0,
        deregister_result: tuple[bool, bool] = (False, False),
    ) -> None:
        self._worker_token = worker_token
        self._prev_was_hijacked = prev_was_hijacked
        self._is_active = is_active
        self.max_ws_message_bytes = max_ws_message_bytes
        self.ws_idle_timeout_s = ws_idle_timeout_s
        self._deregister_result = deregister_result

        self.touch_activity_calls: list[str] = []
        self.register_worker_calls: list[tuple[str, Any]] = []
        self.notify_hijack_changed_calls: list[tuple[str, bool, str | None]] = []
        self.broadcast_hijack_state_calls: list[str] = []
        self.broadcast_calls: list[tuple[str, Any]] = []
        self.request_snapshot_calls: list[str] = []
        self.is_active_worker_calls: list[tuple[str, Any]] = []
        self.append_event_calls: list[tuple[str, Any, Any]] = []
        self.deregister_worker_calls: list[tuple[str, Any]] = []
        self.prune_if_idle_calls: list[str] = []

    def worker_token(self) -> str | None:
        return self._worker_token

    async def touch_activity(self, worker_id: str) -> None:
        self.touch_activity_calls.append(worker_id)

    async def register_worker(self, worker_id: str, ws: Any, *, is_tunnel_worker: bool = False) -> bool:
        self.register_worker_calls.append((worker_id, ws))
        return self._prev_was_hijacked

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        self.notify_hijack_changed_calls.append((worker_id, enabled, owner))

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcast_hijack_state_calls.append(worker_id)

    async def broadcast(
        self, worker_id: str, msg: Any, *, expected_worker: Any = None, expected_event_seq: int | None = None
    ) -> None:
        self.broadcast_calls.append((worker_id, msg))

    async def request_snapshot(self, worker_id: str) -> None:
        self.request_snapshot_calls.append(worker_id)

    async def is_active_worker(self, worker_id: str, ws: Any) -> bool:
        self.is_active_worker_calls.append((worker_id, ws))
        return self._is_active

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.append_event_calls.append((worker_id, event_type, data))
        return {}

    async def deregister_worker(self, worker_id: str, ws: Any) -> tuple[bool, bool]:
        self.deregister_worker_calls.append((worker_id, ws))
        return self._deregister_result

    async def prune_if_idle(self, worker_id: str) -> None:
        self.prune_if_idle_calls.append(worker_id)

    async def cleanup_expired_hijack(self, worker_id: str) -> None:
        # Only reached if the periodic-cleanup background task's 1s sleep
        # elapses before `finally` cancels it; every test here completes in
        # well under that, so this is never actually invoked.
        pass


# ---------------------------------------------------------------------------
# Auth gate (mutmut_1, 5, 7, 9, 13, 23, 29-35)
# ---------------------------------------------------------------------------


async def test_auth_required_but_missing_closes_with_policy_violation() -> None:
    """Kills mutmut_1 (``worker_token`` forced to ``None``, which would skip
    the entire auth gate and let registration through), mutmut_5/7 (the
    ``.get`` default forced to ``None``/dropped, which -- with the header
    absent -- makes ``auth_header`` ``None`` and crashes on
    ``.startswith()`` instead of closing cleanly) and mutmut_29-35 (every
    argument, including arity drops and literal-text mutations, of the
    ``websocket.close(code=1008, reason=...)`` call)."""
    hub = _FakeHub(worker_token="secret-token")
    ws = _WorkerWebSocket(headers={}, recv=[])

    await _run(hub, ws)

    assert ws.accepted == 1
    assert ws.closes == [(1008, "authentication required")]
    assert hub.register_worker_calls == []


async def test_auth_header_key_lookup_is_case_sensitive() -> None:
    """Kills mutmut_9: the ``.get`` key forced to ``"AUTHORIZATION"``. With a
    correct token supplied under the real lowercase ``"authorization"`` key,
    the mutant's uppercase lookup misses entirely, falls back to the
    default, and rejects a request the real code accepts."""
    hub = _FakeHub(worker_token="tok-abc123")
    ws = _WorkerWebSocket(headers={"authorization": "Bearer tok-abc123"}, recv=[_RecvItem(disconnect=True)])

    await _run(hub, ws)

    assert ws.closes == []
    assert hub.register_worker_calls == [(WORKER_ID, ws)]


async def test_bearer_prefix_check_gates_the_removeprefix_branch() -> None:
    """Kills mutmut_13: the ``if auth_header.startswith("Bearer ") ... else
    ""`` condition forced to always take the ``if`` branch (``or True``). A
    header value equal to the worker token but missing the ``"Bearer "``
    prefix must be rejected by the real code (the un-prefixed value never
    reaches ``provided``); the mutant's forced-``if`` branch runs
    ``removeprefix`` as a no-op and lets the raw value straight through to
    ``compare_digest``, wrongly succeeding."""
    hub = _FakeHub(worker_token="plain-value-no-bearer")
    ws = _WorkerWebSocket(headers={"authorization": "plain-value-no-bearer"}, recv=[])

    await _run(hub, ws)

    assert ws.closes == [(1008, "authentication required")]
    assert hub.register_worker_calls == []


async def test_missing_header_else_branch_literal_is_the_empty_string() -> None:
    """Kills mutmut_23: the ternary's ``else`` literal forced from ``""`` to
    ``"XXXX"``. Choosing a worker token that equals that exact literal turns
    an absent header into a wrongly-accepted request under the mutant, while
    the real code's true empty string never matches a non-empty token."""
    hub = _FakeHub(worker_token="XXXX")
    ws = _WorkerWebSocket(headers={}, recv=[])

    await _run(hub, ws)

    assert ws.closes == [(1008, "authentication required")]
    assert hub.register_worker_calls == []


# ---------------------------------------------------------------------------
# Connect-time bookkeeping (mutmut_41-60, 66, 83)
# ---------------------------------------------------------------------------


async def test_connect_bookkeeping_spans_logs_and_hijack_notify(
    fake_logger: _FakeLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills mutmut_41 (``touch_activity`` argument forced ``None``),
    mutmut_42-45 (the connect span's ``get_tracer``/``start_as_current_span``
    arguments), mutmut_46-53 (every ``_set_ws_span_attrs`` argument,
    including arity drops), mutmut_55-58 (every argument of the
    ``term_worker_connected`` info log, including arity drops), mutmut_60
    (``notify_hijack_changed``'s ``worker_id`` forced ``None``), mutmut_66
    (``broadcast_hijack_state``'s argument forced ``None``) and mutmut_83
    (the periodic-cleanup task's ``worker_id`` argument forced ``None``)."""
    tracer_calls: list[Any] = []
    span_name_calls: list[str] = []
    span_attr_calls: list[tuple[Any, dict[str, Any]]] = []
    cleanup_calls: list[tuple[Any, str, float]] = []

    sentinel_span = object()

    class _SpanCM:
        def __enter__(self) -> Any:
            return sentinel_span

        def __exit__(self, *exc: Any) -> Literal[False]:
            return False

    def _fake_get_tracer(name: Any) -> Any:
        tracer_calls.append(name)

        class _Tracer:
            def start_as_current_span(self, span_name: str) -> Any:
                span_name_calls.append(span_name)
                return _SpanCM()

        return _Tracer()

    def _fake_set_ws_span_attrs(span: Any, **attrs: Any) -> None:
        span_attr_calls.append((span, attrs))

    async def _fake_periodic_cleanup(hub: Any, worker_id: str, interval_s: float) -> None:
        cleanup_calls.append((hub, worker_id, interval_s))
        await asyncio.Event().wait()  # cancelled by the real `finally` block

    monkeypatch.setattr(websockets_impl, "get_tracer", _fake_get_tracer)
    monkeypatch.setattr(websockets_impl, "_set_ws_span_attrs", _fake_set_ws_span_attrs)
    monkeypatch.setattr(websockets_impl, "_periodic_hijack_cleanup", _fake_periodic_cleanup)

    hub = _FakeHub(worker_token=None, prev_was_hijacked=True)
    # A non-zero delay forces a genuine suspension point, giving the event
    # loop a chance to actually start the periodic-cleanup task before the
    # `finally` block cancels it below.
    ws = _WorkerWebSocket(headers={}, recv=[_RecvItem(delay=0.001, disconnect=True)])

    await _run(hub, ws)

    assert hub.touch_activity_calls == [WORKER_ID]
    assert tracer_calls[0] == websockets_impl.__name__
    assert span_name_calls[0] == "uterm.ws.worker.connect"
    assert span_attr_calls[0] == (sentinel_span, {"worker_id": WORKER_ID, "operation": "ws.worker.connect"})
    assert fake_logger.infos == [("term_worker_connected worker_id=%s", WORKER_ID)]
    assert hub.notify_hijack_changed_calls == [(WORKER_ID, False, None)]
    assert hub.broadcast_hijack_state_calls == [WORKER_ID]
    assert cleanup_calls == [(hub, WORKER_ID, websockets_impl._WORKER_HIJACK_CLEANUP_INTERVAL_S)]


# ---------------------------------------------------------------------------
# Receive loop: idle timeout (mutmut_93, 96-101)
# ---------------------------------------------------------------------------


async def test_idle_timeout_fires_from_the_configured_timeout_value(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_93 (the ``asyncio.wait_for`` ``timeout=`` argument forced
    to ``None``) and mutmut_96-101 (every argument, including arity drops
    and literal-text mutations, of the idle-timeout info log). A receive
    delay (50ms) longer than the configured idle timeout (20ms) makes the
    real code time out and log before ever touching ``is_active_worker``;
    the mutant's unbounded ``timeout=None`` instead waits out the delay,
    receives the (unrelated) text normally, and never logs the timeout at
    all."""
    hub = _FakeHub(worker_token=None, ws_idle_timeout_s=0.02)
    ws = _WorkerWebSocket(headers={}, recv=[_RecvItem(delay=0.05, text="ignored")])

    await _run(hub, ws)

    # The connect-time "term_worker_connected" info log always fires first;
    # the idle-timeout log is the one under test here.
    assert fake_logger.infos == [
        ("term_worker_connected worker_id=%s", WORKER_ID),
        ("ws_worker_idle_timeout worker_id=%s", WORKER_ID),
    ]
    assert hub.is_active_worker_calls == []


# ---------------------------------------------------------------------------
# Receive loop: oversized message (mutmut_103, 105, 110)
# ---------------------------------------------------------------------------


async def test_oversized_check_is_strictly_greater_than_at_the_boundary(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_103: ``>`` widened to ``>=``. A message whose encoded
    byte length exactly equals ``max_ws_message_bytes`` must be processed
    normally by the real code (strictly-greater is false); the mutant's
    ``>=`` treats the boundary itself as oversized and drops it instead."""
    hub = _FakeHub(worker_token=None, max_ws_message_bytes=10, is_active=True)
    ws = _WorkerWebSocket(headers={}, recv=[_RecvItem(text="0123456789"), _RecvItem(disconnect=True)])

    await _run(hub, ws)

    assert fake_logger.warnings == []
    assert hub.append_event_calls == [(WORKER_ID, "term", {"data": "0123456789"})]


async def test_oversized_message_is_dropped_with_the_correct_log(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_105 (the oversized warning's ``worker_id`` forced
    ``None``) and mutmut_110 (that log's format string mutated to a
    ``XX``-wrapped literal)."""
    hub = _FakeHub(worker_token=None, max_ws_message_bytes=5, is_active=True)
    ws = _WorkerWebSocket(headers={}, recv=[_RecvItem(text="0123456789"), _RecvItem(disconnect=True)])

    await _run(hub, ws)

    assert fake_logger.warnings == [("ws_worker_oversized worker_id=%s size=%d", WORKER_ID, 10)]
    assert hub.append_event_calls == []


# ---------------------------------------------------------------------------
# Receive loop: inactive worker (mutmut_118-125)
# ---------------------------------------------------------------------------


async def test_inactive_worker_closes_and_logs_then_stops(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_119-124: every argument, including arity drops and
    literal-text mutations, of the ``ws_worker_closed_inactive`` debug log.
    (mutmut_125, ``break`` -> ``return`` here, is a documented equivalent --
    see the module docstring.)"""
    hub = _FakeHub(worker_token=None, is_active=False)
    ws = _WorkerWebSocket(headers={}, recv=[_RecvItem(text="x")])

    await _run(hub, ws)

    assert ws.closes == [(1000, None)]
    assert fake_logger.debugs == [("ws_worker_closed_inactive worker_id=%s", WORKER_ID)]


async def test_inactive_worker_close_failure_is_suppressed(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_118: ``with suppress(Exception):`` mutated to
    ``suppress(None)``. Forcing ``websocket.close()`` to raise: the real
    code's ``suppress(Exception)`` swallows it and reaches ``break``
    cleanly, so the outer ``except Exception`` handler (which logs
    ``term_worker_ws_error``) is never reached; the mutant's
    ``suppress(None)`` cannot swallow anything (``issubclass(exc, (None,))``
    itself raises ``TypeError``), and that new exception propagates straight
    into the outer handler."""
    hub = _FakeHub(worker_token=None, is_active=False)
    ws = _WorkerWebSocket(headers={}, recv=[_RecvItem(text="x")], raise_on_close=RuntimeError("boom"))

    await _run(hub, ws)

    assert fake_logger.warnings == []
    assert fake_logger.debugs == []


# ---------------------------------------------------------------------------
# Receive loop: malformed control stream (mutmut_128-154)
# ---------------------------------------------------------------------------

_DLE = "\x10"
_STX = "\x02"
# A structurally-invalid control header (8 non-hex digits where the length
# field must be); padded well past 256 chars so the real code's
# `raw[:256]` preview and a hypothetical `raw[:257]` slice are distinguishable.
_BAD_STREAM_RAW = f"{_DLE}{_STX}GGGGGGGG:" + ("A" * 300)


async def test_bad_control_stream_logs_closes_and_previews_correctly(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_128 (``preview`` forced to ``None``), mutmut_129
    (``raw[:256]`` widened to ``raw[:257]``), mutmut_130-141 (every argument
    and the format string, including arity drops and literal-text
    mutations, of the ``ws_worker_bad_stream`` warning log), mutmut_143-148
    (every argument, including arity drops, of the
    ``websocket.close(code=1003, reason=str(exc))`` call) and mutmut_149-154
    (every argument, including arity drops and literal-text mutations, of
    the ``ws_worker_closed_protocol_error`` debug log)."""
    hub = _FakeHub(worker_token=None, is_active=True)
    ws = _WorkerWebSocket(headers={}, recv=[_RecvItem(text=_BAD_STREAM_RAW)])

    await _run(hub, ws)

    assert len(fake_logger.warnings) == 1
    fmt, worker_id, exc, raw_len, preview = fake_logger.warnings[0]
    assert fmt == "ws_worker_bad_stream worker_id=%s: %s raw_len=%d preview=%r"
    assert worker_id == WORKER_ID
    assert str(exc) == "invalid control header"
    assert raw_len == len(_BAD_STREAM_RAW)
    assert preview == _BAD_STREAM_RAW[:256]

    assert ws.closes == [(1003, "invalid control header")]
    assert fake_logger.debugs == [("ws_worker_closed_protocol_error worker_id=%s", WORKER_ID)]


async def test_bad_control_stream_close_failure_is_suppressed(fake_logger: _FakeLogger) -> None:
    """Kills mutmut_142: ``with suppress(Exception):`` (around the
    protocol-error close) mutated to ``suppress(None)``. The bad-stream
    warning above the ``with`` block still logs unconditionally; forcing
    ``websocket.close()`` to raise lets the real code swallow it (no second
    warning), while the mutant's ``suppress(None)`` lets a new ``TypeError``
    reach the outer handler, which logs a second ``term_worker_ws_error``
    warning."""
    hub = _FakeHub(worker_token=None, is_active=True)
    short_bad_raw = f"{_DLE}{_STX}GGGGGGGG:"
    ws = _WorkerWebSocket(headers={}, recv=[_RecvItem(text=short_bad_raw)], raise_on_close=RuntimeError("boom"))

    await _run(hub, ws)

    assert len(fake_logger.warnings) == 1
    assert fake_logger.debugs == []


# ---------------------------------------------------------------------------
# Receive loop: DataChunk (terminal data) branch (mutmut_156, 172-178)
# ---------------------------------------------------------------------------


async def test_data_chunk_touches_broadcasts_and_appends_the_term_event(
    fake_logger: _FakeLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills mutmut_156 (the ``touch_activity`` call inside the loop forced
    to ``None``), mutmut_172-178 (every argument, including arity drops and
    a literal-text mutation, of the ``append_event(worker_id, "term",
    {"data": ...})`` call), and mutmut_169/171 (``ts`` forced to ``None``
    explicitly, or dropped so ``make_term_frame``'s own default applies).

    ``websockets_impl.py`` and ``provide.uterm.server.bridge.frames`` (where
    ``make_term_frame`` lives) each do their own ``import time``. Rebinding
    each module's ``time`` *name* to a distinct fake clock -- rather than
    patching the shared stdlib ``time`` module's ``.time`` attribute in
    place, which every consumer's ``import time`` would see identically --
    makes it observable which call site actually produced ``ts``: the real
    code computes ``ts=time.time()`` at the outer call site (reporting
    ``_OUTER_TS``) and ``make_term_frame`` never reaches its own
    ``ts is None`` fallback; either mutant passes ``ts=None`` through, so
    ``make_term_frame`` reaches that fallback itself and reports the
    *inner* ``_FRAMES_MODULE_TS`` instead. (mutmut_161/165/166/167, the
    ``cast()`` type-string mutations, remain a documented equivalent -- see
    the module docstring.)"""
    monkeypatch.setattr(websockets_impl, "time", SimpleNamespace(time=lambda: _OUTER_TS))
    monkeypatch.setattr(_frames_module, "time", SimpleNamespace(time=lambda: _FRAMES_MODULE_TS))

    hub = _FakeHub(worker_token=None, is_active=True)
    ws = _WorkerWebSocket(headers={}, recv=[_RecvItem(text="term-output-chunk"), _RecvItem(disconnect=True)])

    await _run(hub, ws)

    # The connect-time touch_activity (line 108) plus the DataChunk-branch
    # touch_activity (line 153): both must carry the real worker_id.
    assert hub.touch_activity_calls == [WORKER_ID, WORKER_ID]

    # Computed with an explicit, non-None `ts` so this expected value itself
    # never triggers `make_term_frame`'s own fallback -- it only pins what
    # the real `_ws_worker_term` code is expected to have already computed.
    expected_frame = make_term_frame("term-output-chunk", ts=_OUTER_TS)
    assert hub.broadcast_calls[-1] == (WORKER_ID, expected_frame)
    assert expected_frame["ts"] == _OUTER_TS
    assert expected_frame["ts"] != _FRAMES_MODULE_TS

    assert hub.append_event_calls == [(WORKER_ID, "term", {"data": "term-output-chunk"})]
