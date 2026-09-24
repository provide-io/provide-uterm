#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``websockets_impl._ws_browser_term``'s receive
loop, idle timeout, decode/dispatch, and ``finally``/cleanup logic.

Kill-suite only — behavioural coverage of the browser terminal route lives in
``test_websockets_coverage.py`` and friends, which drive ``_ws_browser_term``
end to end through a real ``TermHub`` + ``TestClient`` websocket. Those tests
never inspect the *exact* string/argument content of every ``logger.*`` call,
never distinguish the strict-greater-than oversized-message boundary from
``>=``, and never separately exercise the ``was_owner`` vs
``resume_without_owner`` disconnect branches with a fully controlled
``cleanup_browser_disconnect`` result — which is exactly what left this
function's 86 listed mutants ``SURVIVED``.

Every test here calls ``_ws_browser_term`` directly with a hand-rolled
``FakeWebSocket`` (``receive_text()`` driven by a queue of actions: a string,
the ``_TIMEOUT``/``_DISCONNECT`` sentinels, or an arbitrary exception to
raise) and a hand-rolled ``FakeHub`` that records every call it receives with
its *exact* positional/keyword arguments. ``ControlFrameDecoder``,
``dispatch_browser_event``, and ``resume_worker_on_disconnect`` are
monkeypatched on the ``websockets_impl`` module (they have their own
kill-suites) with fakes that record calls and let each test script the
outcome (a parsed "event", a raised ``ControlFrameProtocolError``, or an
arbitrary exception) without needing real DLE/STX control-frame bytes or a
real hub.

Every await is bounded via ``asyncio.wait_for(..., 2)`` in ``_run()``; no
test sleeps.

Documented equivalents (not tested — see each note for why no observable
difference exists):

- **Mutant 5** (idle-timeout ``break`` -> ``return``) and **mutant 30**
  (protocol-error ``break`` -> ``return``): both ``break`` statements exit
  the *only* loop in the function's outer ``try`` body, and that loop is the
  *last* statement in the ``try`` — nothing runs between the loop's exit and
  the enclosing ``finally``. A bare ``return`` from inside a ``try`` runs
  that same ``finally`` before actually returning. So both spellings reach
  the identical ``finally`` with the identical local state and both return
  ``None`` to the caller — there is nothing left to observe that would
  differ.
- **Mutant 66** (``_do_resume = False`` -> ``_do_resume = None`` after
  ``check_still_hijacked``) and **mutant 84** (the analogous
  ``resume_without_owner = False`` -> ``None``): both variables are only
  ever read afterwards through a bare truthiness check (``if _do_resume:`` /
  ``if resume_without_owner:``). ``None`` and ``False`` are both falsy, so
  no branch taken afterward can tell them apart.
- **Mutant 71** (``hub.notify_hijack_changed(worker_id, enabled=False,
  owner=None)`` -> drops the ``owner=None`` kwarg entirely): the real
  ``notify_hijack_changed(worker_id, *, enabled, owner=None)`` already
  defaults ``owner`` to ``None``. Omitting an explicit ``owner=None`` and
  falling back to that same default is not observable from the caller's
  side.

Mutants 62/63 (``_dm_disconnect(worker_id, principal=...)`` dropping the
``websocket`` positional, and ``_dm_disconnect(worker_id, websocket, )``
dropping the ``principal`` kwarg) are not asserted on directly: the fake
``deckmux_on_browser_disconnect`` recorder below declares ``websocket`` and
``principal`` as required parameters with no defaults, mirroring the
technique the sibling ``resume_worker_on_disconnect`` kill-suite uses for
dropped-argument mutants — a mutated call missing either one raises
``TypeError`` before this suite's own (unmutated-baseline) assertions would
even run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from provide.uterm.control_channel import ControlFrameProtocolError
from provide.uterm.server.bridge.routes import websockets_impl
from provide.uterm.server.bridge.routes.websockets_impl import _ws_browser_term

WID = "browser-worker"

# receive_text() action sentinels.
_TIMEOUT = object()
_DISCONNECT = object()
_UNSET = object()


async def _run(hub: Any, websocket: Any, worker_id: str = WID) -> None:
    """Call ``_ws_browser_term`` bounded by a 2s watchdog; never hangs a test."""
    await asyncio.wait_for(_ws_browser_term(hub, websocket, worker_id), timeout=2)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Records accept()/close()/send_text(); receive_text() replays a script.

    ``has_state=False`` omits the ``.state`` attribute entirely (for the
    mutant-55 "dropped default" kill, which needs a real ``AttributeError``
    when nothing is there to fall back to).
    """

    def __init__(
        self,
        actions: list[Any],
        *,
        principal: Any = None,
        has_state: bool = True,
        close_raises: BaseException | None = None,
    ) -> None:
        self._actions = list(actions)
        self._close_raises = close_raises
        self.accepted = False
        self.closed: list[tuple[int | None, str | None]] = []
        self.sent_texts: list[str] = []
        if has_state:
            self.state = SimpleNamespace(uterm_principal=principal)

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        if self._close_raises is not None:
            raise self._close_raises
        self.closed.append((code, reason))

    async def send_text(self, text: str) -> None:
        self.sent_texts.append(text)

    async def receive_text(self) -> str:
        action = self._actions.pop(0)
        if action is _TIMEOUT:
            raise TimeoutError
        if action is _DISCONNECT:
            raise WebSocketDisconnect
        if isinstance(action, BaseException):
            raise action
        assert isinstance(action, str)
        return action


class _FakeDecoder:
    """Stands in for ``ControlFrameDecoder``.

    ``feed()`` raises ``ControlFrameProtocolError`` for a ``"__ERR__:<msg>"``
    payload, otherwise returns a single opaque "event" wrapping the raw
    text — real framing bytes are irrelevant to every mutant this suite
    targets.
    """

    def __init__(self, **_kw: Any) -> None:
        pass

    def feed(self, raw: str) -> list[Any]:
        if raw.startswith("__ERR__:"):
            raise ControlFrameProtocolError(raw.removeprefix("__ERR__:"))
        return [SimpleNamespace(marker=raw)]


class _DispatchRecorder:
    """Fake ``dispatch_browser_event``: records every call's exact kwargs."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    async def __call__(
        self,
        hub: Any,
        websocket: Any,
        worker_id: str,
        role: str,
        can_hijack: bool,
        owned_hijack: bool,
        event: Any,
        browser_bucket: Any,
        browser_control_bucket: Any,
    ) -> tuple[str, bool, bool]:
        self.calls.append(
            {
                "hub": hub,
                "websocket": websocket,
                "worker_id": worker_id,
                "role": role,
                "can_hijack": can_hijack,
                "owned_hijack": owned_hijack,
                "event": event,
            }
        )
        if self._raises is not None:
            raise self._raises
        return role, can_hijack, owned_hijack


class _ResumeRecorder:
    """Fake (sync) ``resume_worker_on_disconnect``: records ``(hub, worker_id)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, str]] = []

    def __call__(self, hub: Any, worker_id: str) -> None:
        self.calls.append((hub, worker_id))


class _DeckmuxDisconnectRecorder:
    """Fake ``hub.deckmux_on_browser_disconnect``.

    ``websocket``/``principal`` are required (no defaults) so that mutants
    62/63, which drop one or the other from the real call, raise
    ``TypeError`` rather than silently succeeding — see the module docstring.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []

    async def __call__(self, worker_id: str, websocket: Any, *, principal: Any) -> None:
        self.calls.append((worker_id, websocket, principal))


class _FakeLogger:
    """Records every info/warning/debug call's exact args/kwargs."""

    def __init__(self) -> None:
        self.infos: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.warnings: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.debugs: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def info(self, *args: Any, **kwargs: Any) -> None:
        self.infos.append((args, kwargs))

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warnings.append((args, kwargs))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self.debugs.append((args, kwargs))


def _default_cleanup_result() -> dict[str, Any]:
    return {"was_owner": False, "rest_still_active": False, "resume_without_owner": False}


class FakeHub:
    """Mirrors every ``TermHub`` surface ``_ws_browser_term`` touches directly.

    Every method asserts nothing itself; instead each call is recorded with
    its exact args/kwargs in ``self.calls`` so tests can assert precisely —
    mirroring the sibling ``resume_worker_on_disconnect`` suite's ``_Hub``.
    """

    def __init__(
        self,
        *,
        max_ws_message_bytes: int = 10_000,
        ws_idle_timeout_s: float = 30.0,
        role: str = "admin",
        cleanup_result: dict[str, Any] | None = None,
        check_still_hijacked_result: bool = False,
        deckmux_disconnect: Any = _UNSET,
    ) -> None:
        self.max_ws_message_bytes = max_ws_message_bytes
        self.ws_idle_timeout_s = ws_idle_timeout_s
        self.browser_rate_limit_per_sec = 100.0
        self.browser_control_rate_limit_per_sec = 100.0
        self.resume_store = None
        self._role = role
        self._cleanup_result = cleanup_result or _default_cleanup_result()
        self._check_still_hijacked_result = check_still_hijacked_result
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        if deckmux_disconnect is not _UNSET:
            self.deckmux_on_browser_disconnect = deckmux_disconnect

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def calls_named(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for (n, args, kwargs) in self.calls if n == name]

    async def resolve_role_for_browser(self, websocket: Any, worker_id: str) -> str:
        self._record("resolve_role_for_browser", websocket, worker_id)
        return self._role

    async def register_browser(
        self, worker_id: str, websocket: Any, role: str, *, defer_broadcast: bool
    ) -> dict[str, Any]:
        self._record("register_browser", worker_id, websocket, role, defer_broadcast=defer_broadcast)
        return {
            "is_hijacked": False,
            "hijacked_by_me": False,
            "worker_online": True,
            "input_mode": "normal",
            "initial_snapshot": None,
            "resume_token": None,
        }

    async def touch_activity(self, worker_id: str) -> None:
        self._record("touch_activity", worker_id)

    async def hijack_state_msg_for(self, worker_id: str, websocket: Any) -> dict[str, Any]:
        self._record("hijack_state_msg_for", worker_id, websocket)
        return {"type": "hijack_state"}

    async def request_snapshot(self, worker_id: str) -> None:
        self._record("request_snapshot", worker_id)

    async def activate_browser_broadcasts(self, worker_id: str, websocket: Any) -> None:
        self._record("activate_browser_broadcasts", worker_id, websocket)

    def metric(self, name: str) -> None:
        self._record("metric", name)

    async def check_still_hijacked(self, worker_id: str) -> bool:
        self._record("check_still_hijacked", worker_id)
        return self._check_still_hijacked_result

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self._record("broadcast_hijack_state", worker_id)

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        self._record("notify_hijack_changed", worker_id, enabled=enabled, owner=owner)

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> None:
        self._record("append_event", worker_id, event_type, data)

    async def cleanup_browser_disconnect(self, worker_id: str, websocket: Any, owned_hijack: bool) -> dict[str, Any]:
        self._record("cleanup_browser_disconnect", worker_id, websocket, owned_hijack)
        return self._cleanup_result

    async def prune_if_idle(self, worker_id: str) -> None:
        self._record("prune_if_idle", worker_id)


def _patch_common(monkeypatch: pytest.MonkeyPatch, logger: _FakeLogger) -> None:
    monkeypatch.setattr(websockets_impl, "logger", logger)
    monkeypatch.setattr(websockets_impl, "ControlFrameDecoder", _FakeDecoder)


# ---------------------------------------------------------------------------
# Idle timeout (mutants 1-4; mutant 5 is equivalent, see module docstring)
# ---------------------------------------------------------------------------


async def test_idle_timeout_logs_exact_message_and_stops_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutants 1-4: every rewrite of the
    ``logger.info("ws_browser_idle_timeout worker_id=%s", worker_id)`` call
    (``worker_id`` dropped/replaced with ``None``, and the format string's
    content/case mangled) makes the recorded call compare unequal to the
    exact expected 2-arg call."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    hub = FakeHub()
    ws = FakeWebSocket([_TIMEOUT])

    await _run(hub, ws)

    assert logger_fake.infos == [(("ws_browser_idle_timeout worker_id=%s", WID), {})]
    assert hub.calls_named("prune_if_idle") == [((WID,), {})]


# ---------------------------------------------------------------------------
# Oversized-message boundary (mutant 6) and its log call (mutants 7, 8)
# ---------------------------------------------------------------------------


async def test_oversized_boundary_is_strictly_greater_than(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutant 6 (``>`` -> ``>=``): a message whose UTF-8 length is
    *exactly* ``hub.max_ws_message_bytes`` must NOT be treated as oversized —
    it must reach the decoder/dispatcher. The mutant's ``>=`` would instead
    discard it via the oversized ``continue`` branch, so ``dispatch`` would
    never be called."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    dispatch = _DispatchRecorder()
    monkeypatch.setattr(websockets_impl, "dispatch_browser_event", dispatch)
    hub = FakeHub(max_ws_message_bytes=8)
    exact = "x" * 8
    ws = FakeWebSocket([exact, _DISCONNECT])

    await _run(hub, ws)

    assert not any(args and args[0] == "ws_browser_oversized worker_id=%s size=%d" for args, _ in logger_fake.warnings)
    assert len(dispatch.calls) == 1


async def test_oversized_message_logs_exact_warning_and_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutants 7 (``worker_id`` -> ``None``) and 8 (the format
    string's content mangled): the oversized warning's args must match
    exactly, and the oversized message must never reach ``dispatch``."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    dispatch = _DispatchRecorder()
    monkeypatch.setattr(websockets_impl, "dispatch_browser_event", dispatch)
    hub = FakeHub(max_ws_message_bytes=4)
    too_big = "x" * 5
    ws = FakeWebSocket([too_big, _DISCONNECT])

    await _run(hub, ws)

    assert logger_fake.warnings == [(("ws_browser_oversized worker_id=%s size=%d", WID, 5), {})]
    assert dispatch.calls == []


# ---------------------------------------------------------------------------
# Bad control-frame stream (mutants 9-16, 18-29; mutant 17 separately below;
# mutant 30 is equivalent, see module docstring)
# ---------------------------------------------------------------------------


async def test_bad_stream_logs_closes_and_debug_logs_with_exact_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutants 9-16 (every arg/format-string rewrite of the
    ``ws_browser_bad_stream`` warning), 18-23 (every arg rewrite of
    ``websocket.close(code=1003, reason=str(exc))``), and 24-29 (every
    arg/format-string rewrite of the ``ws_browser_closed_protocol_error``
    debug log) — each assertion below pins one call's exact args, and a
    dropped argument changes the tuple's length/equality just as much as a
    substituted one."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    hub = FakeHub()
    ws = FakeWebSocket(["__ERR__:boom123"])

    await _run(hub, ws)

    assert len(logger_fake.warnings) == 1
    args, kwargs = logger_fake.warnings[0]
    assert kwargs == {}
    assert args[0] == "ws_browser_bad_stream worker_id=%s: %s"
    assert args[1] == WID
    bad_stream_exc = args[2]
    assert isinstance(bad_stream_exc, ControlFrameProtocolError)
    assert str(bad_stream_exc) == "boom123"

    assert ws.closed == [(1003, "boom123")]
    assert logger_fake.debugs == [(("ws_browser_closed_protocol_error worker_id=%s", WID), {})]


async def test_bad_stream_close_failure_is_suppressed_not_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutant 17 (``suppress(Exception)`` -> ``suppress(None)``).

    When ``websocket.close()`` itself raises, ``contextlib.suppress``'s
    ``__exit__`` only evaluates ``issubclass(exctype, self._exceptions)``
    when an exception is actually pending — so ``suppress(None)`` behaves
    identically to ``suppress(Exception)`` in the *no-failure* case (both are
    equivalent then), but here ``close()`` raises: ``suppress(Exception)``
    swallows it and the loop proceeds via ``break``; ``suppress(None)``
    instead raises ``TypeError`` from ``issubclass(RuntimeError, (None,))``,
    which propagates to the outer ``except Exception`` handler and produces
    a *second* warning (``term_browser_ws_error``). Asserting exactly one
    warning distinguishes them."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    hub = FakeHub()
    ws = FakeWebSocket(["__ERR__:boom"], close_raises=RuntimeError("close boom"))

    await _run(hub, ws)

    assert len(logger_fake.warnings) == 1
    assert logger_fake.warnings[0][0][0] == "ws_browser_bad_stream worker_id=%s: %s"
    assert logger_fake.debugs == []


# ---------------------------------------------------------------------------
# dispatch_browser_event's can_hijack/owned_hijack args (mutants 31, 32)
# ---------------------------------------------------------------------------


async def test_dispatch_browser_event_receives_exact_can_hijack_and_owned_hijack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutants 31 (``can_hijack`` -> ``None``) and 32 (``owned_hijack``
    -> ``None``): with an admin role, ``can_hijack`` must be exactly ``True``
    and a freshly connected browser's ``owned_hijack`` must be exactly
    ``False`` — neither equals ``None``."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    dispatch = _DispatchRecorder()
    monkeypatch.setattr(websockets_impl, "dispatch_browser_event", dispatch)
    hub = FakeHub(role="admin")
    ws = FakeWebSocket(["hello", _DISCONNECT])

    await _run(hub, ws)

    assert len(dispatch.calls) == 1
    call = dispatch.calls[0]
    assert call["can_hijack"] is True
    assert call["owned_hijack"] is False


# ---------------------------------------------------------------------------
# Outer exception handler (mutants 33-40)
# ---------------------------------------------------------------------------


async def test_unexpected_dispatch_exception_logs_exact_outer_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutants 33-40: every arg/format-string rewrite of
    ``logger.warning("term_browser_ws_error worker_id=%s error=%s",
    worker_id, exc)`` in the outer ``except Exception`` handler."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    boom = RuntimeError("dispatch exploded")
    monkeypatch.setattr(websockets_impl, "dispatch_browser_event", _DispatchRecorder(raises=boom))
    hub = FakeHub()
    ws = FakeWebSocket(["hello"])

    await _run(hub, ws)

    assert logger_fake.warnings == [(("term_browser_ws_error worker_id=%s error=%s", WID, boom), {})]


# ---------------------------------------------------------------------------
# Unconditional finally: metrics, touch_activity, prune_if_idle
# (mutants 41-47, 86)
# ---------------------------------------------------------------------------


async def test_finally_emits_exact_disconnect_metrics_touch_activity_and_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutants 41-43 (``hub.metric("ws_disconnect_total")`` mangled),
    44-46 (``hub.metric("ws_disconnect_browser_total")`` mangled), 47
    (the finally block's ``hub.touch_activity(worker_id)`` -> ``None``), and
    86 (``hub.prune_if_idle(worker_id)`` -> ``None``). These all run
    unconditionally on every disconnect, so an immediate disconnect exercises
    every one of them."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    hub = FakeHub()
    ws = FakeWebSocket([_DISCONNECT])

    await _run(hub, ws)

    assert hub.calls_named("metric") == [
        (("ws_disconnect_total",), {}),
        (("ws_disconnect_browser_total",), {}),
    ]
    # touch_activity fires once on connect (in the try) and once in finally;
    # both must carry the real worker_id, not None.
    assert hub.calls_named("touch_activity") == [((WID,), {}), ((WID,), {})]
    assert hub.calls_named("prune_if_idle") == [((WID,), {})]


# ---------------------------------------------------------------------------
# UTERM_TEST_MODE env check gating the DeckMux disconnect principal lookup
# (mutants 48-51)
# ---------------------------------------------------------------------------


async def test_env_check_variants_all_collapse_to_skipping_principal_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutants 48-51 (the env-var name's case/content mangled, the
    ``!=``/``"1"`` comparison flipped or its literal mangled): with
    ``UTERM_TEST_MODE=1`` the real check must SKIP computing the principal
    from ``websocket.state`` (it stays ``None``) even though a real
    principal is present on the socket. Any of these four mutants makes the
    check effectively always-true regardless of the real env value, so it
    would compute the real principal instead."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    dm_recorder = _DeckmuxDisconnectRecorder()
    hub = FakeHub(deckmux_disconnect=dm_recorder)
    ws = FakeWebSocket([_DISCONNECT], principal="REAL_PRINCIPAL")

    await _run(hub, ws)

    assert dm_recorder.calls == [(WID, ws, None)]


# ---------------------------------------------------------------------------
# Principal lookup from websocket.state when NOT in test mode
# (mutants 52-54, 56-61; mutant 55 separately below)
# ---------------------------------------------------------------------------


async def test_principal_lookup_reads_real_state_when_not_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutants 52-54 (the ``getattr`` chain short-circuited to
    ``None`` at various points), 56-59 (the ``"state"``/``"uterm_principal"``
    attribute-name literals mangled, so the lookup silently misses and falls
    back to the default ``None``), and 60-61 (the ``_dm_disconnect`` call's
    ``websocket``/``principal`` args replaced with ``None``): with
    ``UTERM_TEST_MODE`` unset, a real, non-``None`` principal on
    ``websocket.state.uterm_principal`` must reach the DeckMux disconnect
    call unchanged, and the exact websocket object must be passed through."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "0")
    dm_recorder = _DeckmuxDisconnectRecorder()
    hub = FakeHub(deckmux_disconnect=dm_recorder, role="admin")
    ws = FakeWebSocket([_DISCONNECT], principal="REAL_PRINCIPAL")

    await _run(hub, ws)

    assert dm_recorder.calls == [(WID, ws, "REAL_PRINCIPAL")]


async def test_principal_lookup_defaults_to_none_when_websocket_has_no_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutant 55 (``getattr(websocket, "state", None)`` -> drops the
    default, i.e. ``getattr(websocket, "state")``): with a websocket that
    genuinely has no ``.state`` attribute, the real two-arg-with-default form
    falls back to ``None`` and the call completes normally. The mutant's
    default-less form raises ``AttributeError`` instead, which would
    propagate out of this (unmutated-baseline) call as a test error."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "0")
    dm_recorder = _DeckmuxDisconnectRecorder()
    hub = FakeHub(deckmux_disconnect=dm_recorder, role="admin")
    ws = FakeWebSocket([_DISCONNECT], has_state=False)

    await _run(hub, ws)

    assert dm_recorder.calls == [(WID, ws, None)]


# ---------------------------------------------------------------------------
# was_owner branch: resume/broadcast/notify/append_event
# (mutants 64, 65, 67, 68-70, 72, 73-82; mutant 71 is equivalent)
# ---------------------------------------------------------------------------


async def test_was_owner_rest_still_active_blocks_resume_and_emits_exact_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutant 64 (``rest_still_active = disconnect_result[...]`` ->
    ``None``): with the hub reporting a REST session is still active,
    ``_do_resume`` must start ``False`` (``not True``), so neither
    ``resume_worker_on_disconnect`` nor ``notify_hijack_changed`` may fire
    (both gated by ``if _do_resume:``) and ``check_still_hijacked`` is never
    even called (short-circuited). The mutant's ``None`` would instead make
    ``not None`` truthy, wrongly triggering both. Also kills mutants 68
    (``broadcast_hijack_state(worker_id)`` -> ``None``) and 73-82 (every
    arg/literal rewrite of the ``append_event(worker_id, "hijack_released",
    {"owner": "dashboard_ws_disconnect"})`` call, including the dict-value
    drop in mutant 76, whose default of ``None`` differs from the expected
    dict) — both of those run unconditionally inside the ``was_owner``
    branch regardless of ``_do_resume``."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    resume_recorder = _ResumeRecorder()
    monkeypatch.setattr(websockets_impl, "resume_worker_on_disconnect", resume_recorder)
    hub = FakeHub(cleanup_result={"was_owner": True, "rest_still_active": True, "resume_without_owner": False})
    ws = FakeWebSocket([_DISCONNECT])

    await _run(hub, ws)

    assert resume_recorder.calls == []
    assert hub.calls_named("check_still_hijacked") == []
    assert hub.calls_named("notify_hijack_changed") == []
    assert hub.calls_named("broadcast_hijack_state") == [((WID,), {})]
    assert hub.calls_named("append_event") == [((WID, "hijack_released", {"owner": "dashboard_ws_disconnect"}), {})]


async def test_was_owner_check_still_hijacked_cancels_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutant 65 (``hub.check_still_hijacked(worker_id)`` -> ``None``)
    and mutant 67 (``_do_resume = False`` -> ``True`` after that check):
    with ``rest_still_active=False`` (so ``_do_resume`` starts ``True``) and
    ``check_still_hijacked`` reporting the worker is still hijacked (by
    someone else), the real code must cancel the resume — neither
    ``resume_worker_on_disconnect`` nor ``notify_hijack_changed`` may fire."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    resume_recorder = _ResumeRecorder()
    monkeypatch.setattr(websockets_impl, "resume_worker_on_disconnect", resume_recorder)
    hub = FakeHub(
        cleanup_result={"was_owner": True, "rest_still_active": False, "resume_without_owner": False},
        check_still_hijacked_result=True,
    )
    ws = FakeWebSocket([_DISCONNECT])

    await _run(hub, ws)

    assert hub.calls_named("check_still_hijacked") == [((WID,), {})]
    assert resume_recorder.calls == []
    assert hub.calls_named("notify_hijack_changed") == []


async def test_was_owner_happy_path_resumes_and_notifies_with_exact_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutants 68 (``broadcast_hijack_state(worker_id)`` -> ``None``),
    69 (``notify_hijack_changed(worker_id, ...)`` -> ``None``), 70
    (``enabled=False`` -> ``None``), and 72 (``enabled=False`` -> ``True``):
    on the clean owner-disconnect-and-resume path, both calls' exact args
    must match."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    resume_recorder = _ResumeRecorder()
    monkeypatch.setattr(websockets_impl, "resume_worker_on_disconnect", resume_recorder)
    hub = FakeHub(cleanup_result={"was_owner": True, "rest_still_active": False, "resume_without_owner": False})
    ws = FakeWebSocket([_DISCONNECT])

    await _run(hub, ws)

    assert resume_recorder.calls == [(hub, WID)]
    assert hub.calls_named("broadcast_hijack_state") == [((WID,), {})]
    assert hub.calls_named("notify_hijack_changed") == [((WID,), {"enabled": False, "owner": None})]


# ---------------------------------------------------------------------------
# resume_without_owner branch (mutants 83, 85; mutant 84 is equivalent)
# ---------------------------------------------------------------------------


async def test_resume_without_owner_check_still_hijacked_cancels_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutant 83 (``hub.check_still_hijacked(worker_id)`` -> ``None``
    in the ``elif resume_without_owner:`` branch) and mutant 85
    (``resume_without_owner = False`` -> ``True`` after that check): with the
    hub reporting the worker is still hijacked, the real code must cancel the
    resume."""
    logger_fake = _FakeLogger()
    _patch_common(monkeypatch, logger_fake)
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    resume_recorder = _ResumeRecorder()
    monkeypatch.setattr(websockets_impl, "resume_worker_on_disconnect", resume_recorder)
    hub = FakeHub(
        cleanup_result={"was_owner": False, "rest_still_active": False, "resume_without_owner": True},
        check_still_hijacked_result=True,
    )
    ws = FakeWebSocket([_DISCONNECT])

    await _run(hub, ws)

    assert hub.calls_named("check_still_hijacked") == [((WID,), {})]
    assert resume_recorder.calls == []
