#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``websockets_impl._ws_worker_term``'s receive
loop, event handling and ``finally``/cleanup logic.

Kill-suite only, targeting the 99 ``SURVIVED`` mutants recorded for
``x__ws_worker_term`` in mutmut's batch-2 run (mutant ids 179-370 in the
generated file; those numbers are mutmut's own per-function ids, not source
line numbers -- confirmed by grepping ``x__ws_worker_term__mutmut_179`` etc.
directly in the generated module). The connect/auth preamble (worker-token
check, ``register_worker``, the ``prev_was_hijacked`` broadcast) has no
survivors here and is covered by a sibling suite; every fake below still
drives through it (it can't be skipped -- it's the same function), but no
test asserts on it.

``_ws_worker_term`` creates three real ``asyncio.create_task(...)`` calls
(the periodic hijack-cleanup task, and -- in the ``finally`` block -- the
disconnect broadcast task and, when the worker was hijacked, the
hijack-state broadcast task) and never awaits the latter two itself. Relying
on real event-loop scheduling to observe their ``add_done_callback`` logic
deterministically is fragile (ordering across ``call_soon`` is not something
a test should depend on). Instead every test replaces ``websockets_impl.
asyncio`` with ``_EagerAsyncioProxy``, which forwards every attribute to the
real :mod:`asyncio` module (so ``asyncio.wait_for``/``CancelledError`` used
elsewhere in the function are untouched) except ``create_task``, which
returns a ``_FakeTask``: a stand-in that runs its coroutine to completion
*eagerly*, right at construction, via the coroutine protocol directly
(``coro.send(None)``). That works because every coroutine wrapped in
``create_task`` inside this function, once ``_periodic_hijack_cleanup`` is
also replaced with a no-op (see the ``eager_tasks`` fixture), has zero
internal ``await`` points in this suite's fakes -- a fake coroutine that
actually suspends makes ``_FakeTask.__init__`` raise loudly instead of
hanging. Because the fake task is already "done" the instant it exists,
``add_done_callback`` fires its callback immediately and synchronously,
removing all dependence on loop-scheduling order.

``websockets_impl.logger`` is a ``provide.telemetry`` wrapper, not a stdlib
``logging.Logger`` -- ``caplog`` does not reliably capture it (see the
sibling ``test_worker_hello_and_dispatch_mutation_killing.py`` module
docstring for the same point). ``_FakeLogger`` below replaces it wholesale
and records the exact positional-argument tuple of every call.

Documented equivalents (no test targets these):

- mutmut_60/61/62/63 (``cast("dict[str, Any]", make_worker_disconnected_frame
  (worker_id))`` with the type-string argument mutated to ``None``,
  ``"XXdict[str, Any]XX"``, ``"dict[str, any]"`` or ``"DICT[STR, ANY]"``):
  ``typing.cast(typ, val)`` is ``return val`` at runtime -- the first
  argument is a pure static-typing hint that is never evaluated, inspected,
  or used to convert anything. No input can distinguish any of these four
  mutants from the original or from each other.
- mutmut_82 (``hub.notify_hijack_changed(worker_id, enabled=False,
  owner=None)`` with the ``owner=None`` keyword argument dropped entirely):
  ``owner``'s declared default (see
  ``packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/
  store.py``'s ``notify_hijack_changed``) is ``None`` -- omitting the
  keyword and explicitly passing its default value are the same call from
  the callee's side. Unlike ``_set_ws_span_attrs(**attrs)`` below (which
  receives ``**kwargs`` and can observe whether a key is present at all),
  a plain keyword parameter with a matching default gives no such hook.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastapi import WebSocketDisconnect

from provide.uterm.control_channel import ControlChunk, DataChunk, encode_control_frame
from provide.uterm.server.bridge.routes import websockets_impl
from provide.uterm.server.bridge.routes.websockets_impl import _ws_worker_term

# Every call to the function under test is bounded so a hypothetical hang
# (a mutant reaching real, unmocked I/O, or a test-design bug) fails fast
# and clearly instead of stalling a mutation-gate leg.
_CALL_TIMEOUT_S = 2.0

WID = "ws-worker-term-1"

_FMT_IGNORED = "ws_worker_ignored worker_id=%s type=%r"
_FMT_REJECT = "ws_worker_frame_invalid_reject worker_id=%s error=%s"
_FMT_DROP = "ws_worker_frame_invalid_drop worker_id=%s error=%s"
_FMT_OUTER = "term_worker_ws_error worker_id=%s error=%s"
_FMT_DISCONNECTED_INFO = "term_worker_disconnected worker_id=%s"
_FMT_BROADCAST_FAILED = "worker_disconnected_broadcast_failed worker_id=%s error=%s"
_FMT_HIJACK_FAILED = "broadcast_hijack_state_failed worker_id=%s error=%s"


async def _run(hub: Any, ws: Any, worker_id: str = WID) -> None:
    await asyncio.wait_for(_ws_worker_term(hub, ws, worker_id), timeout=_CALL_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeLogger:
    """Records the exact positional-argument tuple of every call (see the
    module docstring for why ``caplog`` can't be used here)."""

    def __init__(self) -> None:
        self.debugs: list[tuple[Any, ...]] = []
        self.warnings: list[tuple[Any, ...]] = []
        self.infos: list[tuple[Any, ...]] = []

    def debug(self, *args: Any) -> None:
        self.debugs.append(args)

    def warning(self, *args: Any) -> None:
        self.warnings.append(args)

    def info(self, *args: Any) -> None:
        self.infos.append(args)


@pytest.fixture
def fake_logger(monkeypatch: pytest.MonkeyPatch) -> _FakeLogger:
    log = _FakeLogger()
    monkeypatch.setattr(websockets_impl, "logger", log)
    return log


class _FakeTask:
    """Eagerly-completed stand-in for ``asyncio.Task``. See the module
    docstring for why every coroutine reaching this fake must have zero
    internal ``await`` points."""

    def __init__(self, coro: Any) -> None:
        self._callbacks: list[Any] = []
        self._exception: BaseException | None = None
        self._result: Any = None
        try:
            coro.send(None)
        except StopIteration as si:
            self._result = si.value
        except BaseException as exc:
            self._exception = exc
        else:  # pragma: no cover - defensive: a real hang must fail loudly, not silently pass
            raise AssertionError("fake coroutine suspended -- it must have zero internal awaits")

    def add_done_callback(self, cb: Any) -> None:
        self._callbacks.append(cb)
        cb(self)

    def cancelled(self) -> bool:
        return False

    def exception(self) -> BaseException | None:
        return self._exception

    def cancel(self) -> bool:
        return False

    def __await__(self) -> Any:
        if False:  # pragma: no cover - makes this a generator function without ever suspending
            yield
        if self._exception is not None:
            raise self._exception
        return self._result


class _EagerAsyncioProxy:
    """Forwards everything to the real :mod:`asyncio` except ``create_task``
    (see the module docstring)."""

    def __init__(self, tasks: list[_FakeTask]) -> None:
        self._tasks = tasks

    def create_task(self, coro: Any, *_args: Any, **_kwargs: Any) -> _FakeTask:
        task = _FakeTask(coro)
        self._tasks.append(task)
        return task

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


@pytest.fixture(autouse=True)
def eager_tasks(monkeypatch: pytest.MonkeyPatch) -> list[_FakeTask]:
    tasks: list[_FakeTask] = []
    monkeypatch.setattr(websockets_impl, "asyncio", _EagerAsyncioProxy(tasks))

    async def _noop_cleanup(_hub: Any, _worker_id: str, _interval_s: float) -> None:
        return None

    monkeypatch.setattr(websockets_impl, "_periodic_hijack_cleanup", _noop_cleanup)
    return tasks


class _RecordingTaskSet(set[Any]):
    """A real ``set`` that also remembers every value ever passed to
    ``.add()`` -- including one later ``.discard()``-ed away -- so a test
    can see what ``hub._background_tasks.add(...)`` was called with even
    after the task's own done-callback removes it again."""

    def __init__(self) -> None:
        super().__init__()
        self.added: list[Any] = []

    def add(self, item: Any) -> None:
        self.added.append(item)
        super().add(item)


class _FakeWorkerWebSocket:
    def __init__(self, raw_messages: list[str], *, send_text_raises: BaseException | None = None) -> None:
        self._queue = list(raw_messages)
        self.headers: dict[str, str] = {}
        self.accepted = False
        self.sent: list[str] = []
        self.closes: list[tuple[int, str | None]] = []
        self._send_text_raises = send_text_raises

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if not self._queue:
            raise WebSocketDisconnect
        return self._queue.pop(0)

    async def send_text(self, data: str) -> None:
        if self._send_text_raises is not None:
            raise self._send_text_raises
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closes.append((code, reason))


class _FakeHub:
    """Mirrors the exact call shapes ``_ws_worker_term`` uses against the
    real ``TermHub`` (see ``bridge/hub/store.py``, ``core_impl.py``,
    ``router_impl.py``)."""

    def __init__(self, *, deregister_result: tuple[bool, bool] = (False, False)) -> None:
        self.worker_frame_on_invalid = "drop"
        self.max_ws_message_bytes = 65536
        self.ws_idle_timeout_s = 30.0
        self._deregister_result = deregister_result
        self._background_tasks: _RecordingTaskSet = _RecordingTaskSet()

        self.touch_calls: list[str] = []
        self.append_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.broadcast_calls: list[tuple[str, dict[str, Any]]] = []
        self.broadcast_raises: dict[str, BaseException] = {}
        self.request_snapshot_calls: list[str] = []
        self.metric_calls: list[tuple[str, int]] = []
        self.hijack_notify_calls: list[tuple[str, dict[str, Any]]] = []
        self.broadcast_hijack_state_calls: list[str] = []
        self.broadcast_hijack_state_raises: BaseException | None = None
        self.deregister_calls: list[tuple[str, Any]] = []
        self.prune_calls: list[str] = []

    def worker_token(self) -> str | None:
        return None

    async def register_worker(self, _worker_id: str, _ws: Any) -> bool:
        return False

    async def touch_activity(self, worker_id: str) -> None:
        self.touch_calls.append(worker_id)

    async def is_active_worker(self, _worker_id: str, _ws: Any) -> bool:
        return True

    async def broadcast(self, worker_id: str, msg: dict[str, Any], **_kwargs: Any) -> None:
        self.broadcast_calls.append((worker_id, msg))
        frame_type = msg.get("type")
        exc = self.broadcast_raises.get(frame_type) if isinstance(frame_type, str) else None
        if exc is not None:
            raise exc

    async def request_snapshot(self, worker_id: str) -> None:
        self.request_snapshot_calls.append(worker_id)

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.append_calls.append((worker_id, event_type, data))
        return {}

    def metric(self, name: str, value: int = 1) -> None:
        self.metric_calls.append((name, value))

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        self.hijack_notify_calls.append((worker_id, {"enabled": enabled, "owner": owner}))

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcast_hijack_state_calls.append(worker_id)
        if self.broadcast_hijack_state_raises is not None:
            raise self.broadcast_hijack_state_raises

    async def deregister_worker(self, worker_id: str, ws: Any) -> tuple[bool, bool]:
        self.deregister_calls.append((worker_id, ws))
        return self._deregister_result

    async def prune_if_idle(self, worker_id: str) -> None:
        self.prune_calls.append(worker_id)


def _decoder_factory(program: dict[str, Any]) -> Any:
    class _ProgrammedDecoder:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def feed(self, raw: str) -> list[Any]:
            result = program[raw]
            if isinstance(result, BaseException):
                raise result
            return cast("list[Any]", result)

    return _ProgrammedDecoder


def _install_decoder(monkeypatch: pytest.MonkeyPatch, program: dict[str, Any]) -> None:
    monkeypatch.setattr(websockets_impl, "ControlFrameDecoder", _decoder_factory(program))


def _hello_fake(result: bool) -> Any:
    async def _fake(_hub: Any, _ws: Any, _worker_id: str, _msg: dict[str, Any]) -> bool:
        return result

    return _fake


def _builder_fake(*, result: dict[str, Any] | None = None, raises: BaseException | None = None) -> Any:
    def _fake(_mtype: str, _msg: dict[str, Any]) -> dict[str, Any]:
        if raises is not None:
            raise raises
        return result if result is not None else {}

    return _fake


def _dispatch_fake(calls: list[Any], *, raises: BaseException | None = None) -> Any:
    async def _fake(hub: Any, worker_id: str, mtype: str, frame: dict[str, Any], expected_worker: Any = None) -> None:
        calls.append((hub, worker_id, mtype, frame, expected_worker))
        if raises is not None:
            raise raises

    return _fake


# ---------------------------------------------------------------------------
# DataChunk / term event (mutmut_1-4)
# ---------------------------------------------------------------------------


async def test_data_chunk_appends_exact_term_event_and_continues_to_next_event(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_1-3 (the ``"term"``/``"data"`` literals in the
    ``append_event`` call) and mutmut_4 (``continue`` -> ``break``, which
    would drop the second chunk in this same batch)."""
    _install_decoder(monkeypatch, {"batch": [DataChunk("first"), DataChunk("second")]})
    hub = _FakeHub()
    ws = _FakeWorkerWebSocket(["batch"])

    await _run(hub, ws)

    assert hub.append_calls == [
        (WID, "term", {"data": "first"}),
        (WID, "term", {"data": "second"}),
    ]


# ---------------------------------------------------------------------------
# Ignored control type (mutmut_5-13)
# ---------------------------------------------------------------------------


async def test_ignored_control_type_logs_exact_debug_and_continues_to_next_event(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_5-12 (every argument, including arity drops and
    literal-text mutations, of the ``ws_worker_ignored`` debug log) and
    mutmut_13 (``continue`` -> ``break``, which would drop the chunk that
    follows the ignored event in the same batch)."""
    _install_decoder(monkeypatch, {"batch": [ControlChunk({"type": "bogus"}), DataChunk("after-ignored")]})
    hub = _FakeHub()
    ws = _FakeWorkerWebSocket(["batch"])

    await _run(hub, ws)

    assert fake_logger.debugs == [(_FMT_IGNORED, WID, "bogus")]
    assert hub.append_calls == [(WID, "term", {"data": "after-ignored"})]


# ---------------------------------------------------------------------------
# worker_hello branch (mutmut_14-15)
# ---------------------------------------------------------------------------


async def test_worker_hello_mismatch_breaks_only_the_batch_not_the_whole_function(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_14 (``break`` -> ``return``): a bare ``return`` would
    exit ``_ws_worker_term`` immediately, so the worker's next queued raw
    message would never be read. Real code's ``break`` only exits the
    ``for event in events`` loop; the outer ``while`` loop keeps receiving."""
    monkeypatch.setattr(websockets_impl, "_handle_worker_hello", _hello_fake(True))
    _install_decoder(
        monkeypatch,
        {
            "hello-mismatch": [ControlChunk({"type": "worker_hello"})],
            "after-hello": [DataChunk("after-hello-chunk")],
        },
    )
    hub = _FakeHub()
    ws = _FakeWorkerWebSocket(["hello-mismatch", "after-hello"])

    await _run(hub, ws)

    assert hub.append_calls == [(WID, "term", {"data": "after-hello-chunk"})]


async def test_worker_hello_success_continues_the_same_batch(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_15 (``continue`` -> ``break``): a successful hello must
    ``continue`` to the next event already decoded from the *same* raw
    message; ``break`` would drop it instead."""
    monkeypatch.setattr(websockets_impl, "_handle_worker_hello", _hello_fake(False))
    _install_decoder(
        monkeypatch,
        {"batch": [ControlChunk({"type": "worker_hello"}), DataChunk("chunk-after-hello-ok")]},
    )
    hub = _FakeHub()
    ws = _FakeWorkerWebSocket(["batch"])

    await _run(hub, ws)

    assert hub.append_calls == [(WID, "term", {"data": "chunk-after-hello-ok"})]


# ---------------------------------------------------------------------------
# Invalid worker frame: reject policy (mutmut_16-24)
# ---------------------------------------------------------------------------


async def test_frame_invalid_reject_logs_sends_closes_and_the_loop_continues(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_16-18 (every argument of the reject warning log),
    mutmut_20-23 (every argument of the ``websocket.close`` call, including
    arity drops and literal-text mutations) and mutmut_24 (``break`` ->
    ``return``, by the same reasoning as the hello-mismatch test above)."""
    hub = _FakeHub()
    hub.worker_frame_on_invalid = "reject"
    exc = ValueError("bad snapshot")
    monkeypatch.setattr(websockets_impl, "_build_worker_frame", _builder_fake(raises=exc))
    _install_decoder(
        monkeypatch,
        {
            "bad-snapshot": [ControlChunk({"type": "snapshot"})],
            "after-reject": [DataChunk("after-reject-chunk")],
        },
    )
    ws = _FakeWorkerWebSocket(["bad-snapshot", "after-reject"])

    await _run(hub, ws)

    assert fake_logger.warnings == [(_FMT_REJECT, WID, exc)]
    assert ws.sent == [encode_control_frame({"type": "error", "reason": "invalid_frame"})]
    assert ws.closes == [(1003, "invalid_frame")]
    assert hub.append_calls == [(WID, "term", {"data": "after-reject-chunk"})]


async def test_frame_invalid_reject_swallows_a_failed_send_via_suppress(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_19 (``with suppress(Exception):`` -> ``suppress(None)``).
    Forcing ``websocket.send_text`` to raise inside the guarded block: the
    real ``suppress(Exception)`` swallows it and never reaches ``close()``
    or the outer handler; ``suppress(None)`` cannot swallow anything --
    ``contextlib.suppress.__exit__`` does ``issubclass(exc_type, (None,))``,
    which itself raises ``TypeError``, and that propagates to the OUTER
    ``except Exception`` handler, producing an extra ``term_worker_ws_error``
    warning the real code never logs."""
    hub = _FakeHub()
    hub.worker_frame_on_invalid = "reject"
    build_exc = ValueError("bad snapshot")
    monkeypatch.setattr(websockets_impl, "_build_worker_frame", _builder_fake(raises=build_exc))
    _install_decoder(monkeypatch, {"bad-snapshot": [ControlChunk({"type": "snapshot"})]})
    ws = _FakeWorkerWebSocket(["bad-snapshot"], send_text_raises=RuntimeError("send boom"))

    await _run(hub, ws)

    assert fake_logger.warnings == [(_FMT_REJECT, WID, build_exc)]
    assert ws.closes == []


# ---------------------------------------------------------------------------
# Invalid worker frame: drop policy (mutmut_25-33)
# ---------------------------------------------------------------------------


async def test_frame_invalid_drop_logs_exact_debug_and_the_loop_continues(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_25-32 (every argument of the drop debug log) and
    mutmut_33 (``continue`` -> ``break``). The ``continue``/``break`` here is
    inside ``for event in events:``, so it only distinguishes the mutant
    when the SAME decoded batch has a second event after the failing one --
    a second, separate ``receive_text()`` message would converge either way
    (both ``continue`` and ``break`` fall through to the next outer ``while``
    iteration identically when nothing else remains in the for-loop). Both
    events below come from a single raw message/single ``decoder.feed()``
    call, via one entry in the decoder program."""
    hub = _FakeHub()
    hub.worker_frame_on_invalid = "drop"
    exc = KeyError("oops")
    monkeypatch.setattr(websockets_impl, "_build_worker_frame", _builder_fake(raises=exc))
    _install_decoder(
        monkeypatch,
        {"batch": [ControlChunk({"type": "analysis"}), DataChunk("after-drop-chunk")]},
    )
    ws = _FakeWorkerWebSocket(["batch"])

    await _run(hub, ws)

    assert fake_logger.debugs == [(_FMT_DROP, WID, exc)]
    assert hub.append_calls == [(WID, "term", {"data": "after-drop-chunk"})]


# ---------------------------------------------------------------------------
# Dispatch call (mutmut_34-35) and outer-handler propagation (mutmut_36-38)
# ---------------------------------------------------------------------------


async def test_dispatch_is_called_with_the_real_websocket_not_a_dropped_none(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_34 (``websocket`` -> ``None``) and mutmut_35 (the
    ``websocket`` argument dropped entirely, which falls back to
    ``_dispatch_worker_frame``'s own default of ``None`` -- observably
    identical to mutmut_34, but both differ from the original, which passes
    the real websocket object)."""
    hub = _FakeHub()
    built_frame = {"marker": "built"}
    monkeypatch.setattr(websockets_impl, "_build_worker_frame", _builder_fake(result=built_frame))
    calls: list[Any] = []
    monkeypatch.setattr(websockets_impl, "_dispatch_worker_frame", _dispatch_fake(calls))
    _install_decoder(monkeypatch, {"batch": [ControlChunk({"type": "status"})]})
    ws = _FakeWorkerWebSocket(["batch"])

    await _run(hub, ws)

    assert calls == [(hub, WID, "status", built_frame, ws)]


async def test_dispatch_failure_propagates_and_is_logged_by_the_outer_handler(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_36-38 (every argument, including a literal-text
    mutation, of the outer ``term_worker_ws_error`` warning log). The
    dispatch call runs OUTSIDE the builder's try/except by design, so a
    failure there must reach the function's outermost ``except Exception``."""
    hub = _FakeHub()
    monkeypatch.setattr(websockets_impl, "_build_worker_frame", _builder_fake(result={}))
    dispatch_exc = RuntimeError("dispatch boom")
    monkeypatch.setattr(websockets_impl, "_dispatch_worker_frame", _dispatch_fake([], raises=dispatch_exc))
    _install_decoder(monkeypatch, {"batch": [ControlChunk({"type": "status"})]})
    ws = _FakeWorkerWebSocket(["batch"])

    await _run(hub, ws)

    assert fake_logger.warnings == [(_FMT_OUTER, WID, dispatch_exc)]


# ---------------------------------------------------------------------------
# finally: disconnect span + attrs (mutmut_39-49)
# ---------------------------------------------------------------------------


async def test_finally_records_the_disconnect_span_with_exact_tracer_and_attrs(
    monkeypatch: pytest.MonkeyPatch, fake_logger: _FakeLogger
) -> None:
    """Kills mutmut_39-42 (the tracer name / span name arguments) and
    mutmut_43-49 (every argument of the ``_set_ws_span_attrs`` call,
    including both keyword arguments being dropped entirely -- observable
    here because the replacement records the raw ``**attrs`` dict, unlike
    the real ``_set_ws_span_attrs``, which itself hides a dropped key by
    skipping ``None`` values either way)."""
    tracer_calls: list[tuple[Any, Any, Any]] = []
    attr_calls: list[tuple[Any, dict[str, Any]]] = []

    class _FakeSpanCtx:
        def __init__(self, span: Any) -> None:
            self._span = span

        def __enter__(self) -> Any:
            return self._span

        def __exit__(self, *_exc_info: Any) -> None:
            return None

    class _FakeTracer:
        def __init__(self, name: Any) -> None:
            self._name = name

        def start_as_current_span(self, span_name: Any) -> _FakeSpanCtx:
            span = object()
            tracer_calls.append((self._name, span_name, span))
            return _FakeSpanCtx(span)

    def _fake_get_tracer(name: Any) -> _FakeTracer:
        return _FakeTracer(name)

    def _fake_set_ws_span_attrs(span: Any, **attrs: Any) -> None:
        attr_calls.append((span, attrs))

    monkeypatch.setattr(websockets_impl, "get_tracer", _fake_get_tracer)
    monkeypatch.setattr(websockets_impl, "_set_ws_span_attrs", _fake_set_ws_span_attrs)
    hub = _FakeHub()
    ws = _FakeWorkerWebSocket([])

    await _run(hub, ws)

    disconnect_spans = [c for c in tracer_calls if c[1] == "uterm.ws.worker.disconnect"]
    assert len(disconnect_spans) == 1
    tracer_name, _span_name, span_obj = disconnect_spans[0]
    assert tracer_name == websockets_impl.__name__

    disconnect_attrs = [c for c in attr_calls if c[1].get("operation") == "ws.worker.disconnect"]
    assert disconnect_attrs == [(span_obj, {"worker_id": WID, "operation": "ws.worker.disconnect"})]


# ---------------------------------------------------------------------------
# finally: should_broadcast metrics + disconnect log (mutmut_50-59)
# ---------------------------------------------------------------------------


async def test_should_broadcast_emits_exact_metrics_and_disconnect_log(
    fake_logger: _FakeLogger,
) -> None:
    """Kills mutmut_50-55 (every ``hub.metric`` name literal) and
    mutmut_56-59 (every argument of the ``term_worker_disconnected`` info
    log)."""
    hub = _FakeHub(deregister_result=(True, False))
    ws = _FakeWorkerWebSocket([])

    await _run(hub, ws)

    assert hub.metric_calls == [("ws_disconnect_total", 1), ("ws_disconnect_worker_total", 1)]
    disconnect_infos = [c for c in fake_logger.infos if c[0] == _FMT_DISCONNECTED_INFO]
    assert disconnect_infos == [(_FMT_DISCONNECTED_INFO, WID)]


# ---------------------------------------------------------------------------
# finally: background-task bookkeeping + done-callback logic
# (mutmut_64-81, 83-99)
# ---------------------------------------------------------------------------


async def test_disconnect_and_hijack_background_tasks_warn_only_on_real_failure(
    fake_logger: _FakeLogger, eager_tasks: list[_FakeTask]
) -> None:
    """Kills mutmut_64/83 (``hub._background_tasks.add(...)`` argument
    dropped to ``None``: the recording set would hold ``None`` instead of
    the real task), mutmut_65/84 (the ``discard`` done-callback dropped to
    ``None``: our fake task calls a registered callback synchronously, so a
    ``None`` callback raises ``TypeError`` immediately instead of quietly
    discarding, crashing this test under mutation), mutmut_66/67/70-77 and
    their mirrored 85/86/89-96 (the two done-callback lambdas replaced or
    their log arguments mutated), mutmut_68/79/80 and mirrored 87/97/98 (the
    ``not t.cancelled() and t.exception() is not None`` guard weakened so a
    real failure would NOT be logged) and mutmut_81 (``enabled=False`` ->
    ``enabled=None`` in the ``notify_hijack_changed`` call)."""
    hub = _FakeHub(deregister_result=(True, True))
    broadcast_exc = RuntimeError("broadcast boom")
    hijack_exc = RuntimeError("hijack boom")
    hub.broadcast_raises = {"worker_disconnected": broadcast_exc}
    hub.broadcast_hijack_state_raises = hijack_exc
    ws = _FakeWorkerWebSocket([])

    await _run(hub, ws)

    _cleanup_task, broadcast_task, hijack_task = eager_tasks
    assert hub._background_tasks.added == [broadcast_task, hijack_task]
    assert hub._background_tasks == set()
    assert hub.hijack_notify_calls == [(WID, {"enabled": False, "owner": None})]
    assert fake_logger.warnings == [
        (_FMT_BROADCAST_FAILED, WID, broadcast_exc),
        (_FMT_HIJACK_FAILED, WID, hijack_exc),
    ]


async def test_disconnect_and_hijack_background_tasks_are_silent_on_success(
    fake_logger: _FakeLogger, eager_tasks: list[_FakeTask]
) -> None:
    """Kills mutmut_69/78 and mirrored 88/99 (the same guard widened with
    ``or True`` / ``not t.cancelled() or ...``, which would log a spurious
    warning even when the background task succeeded)."""
    hub = _FakeHub(deregister_result=(True, True))
    ws = _FakeWorkerWebSocket([])

    await _run(hub, ws)

    assert hub.broadcast_hijack_state_calls == [WID]
    assert fake_logger.warnings == []
