#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for the accept/role-resolution/registration/hello
portion of ``websockets_impl._ws_browser_term``.

Kill-suite only — behavioural coverage of the full browser WS lifecycle lives
elsewhere (``test_browser_handlers_coverage.py`` and friends), which drives
``ws_browser_term`` end to end against a real hub/event loop but never pins
the *exact* argument values of every collaborator call along the connect
path, nor the exact ``hello`` payload dict, nor the exact tracer/span calls.
That left every mutant of the env-var checks, the role-resolution branch, the
``register_browser``/``touch_activity``/tracer calls, the ``hello`` kwargs
dict literals, the ``hijack_state_msg_for`` call, the DeckMux principal
resolution + connect call, the periodic-cleanup task creation, and the
idle-timeout branch in the ``SURVIVED`` state.

Strategy: call ``_ws_browser_term(hub, websocket, worker_id)`` directly
against a fake ``TermHub`` and a fake ``WebSocket`` that record every call's
*exact* arguments and never assert inside a collaborator — the function
wraps most of its body in a broad ``except Exception`` that would silently
swallow an assertion raised from inside a fake, turning a would-be kill into
a false pass. All assertions happen after the coroutine returns.

``make_hello_frame``/``encode_control_frame`` are monkeypatched to a
recorder + identity so the exact ``hello`` kwargs dict and every
``send_text`` payload can be asserted without depending on real
control-frame DLE/STX byte framing. ``asyncio.wait_for`` is monkeypatched
only in the test that exercises the idle-timeout branch, to raise
``TimeoutError`` deterministically instead of sleeping. Every test
terminates the function via either an immediate ``WebSocketDisconnect`` from
``receive_text`` or a faked ``TimeoutError`` from ``wait_for`` — never a real
sleep of more than the bounded ``asyncio.wait_for(..., timeout=2)`` wrapper
used to run the coroutine under test.
"""

from __future__ import annotations

import asyncio
from asyncio import wait_for as _real_wait_for
from types import SimpleNamespace
from typing import Any

from fastapi import WebSocketDisconnect

from provide.uterm.bridge.contracts import (
    CURRENT_PROTOCOL_VERSION,
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    PREFERRED_PROTOCOL_VERSION,
)
from provide.uterm.server.bridge.hub import BrowserRoleResolutionError
from provide.uterm.server.bridge.routes import websockets_impl
from provide.uterm.server.bridge.routes.websockets_impl import _ws_browser_term

WORKER_ID = "worker-early-1"
_MISSING = object()
HIJACKED_BY_ME_SENTINEL = object()
HELLO_RETURN_SENTINEL = {"type": "hello", "sentinel": "hello-return"}
HIJACK_STATE_SENTINEL = {"type": "control", "action": "hijack_state", "sentinel": "hijack-state"}
DM_PRINCIPAL_SENTINEL = object()


class _FakeWebSocket:
    """Records accept/close/send_text; ``receive_text`` raises immediately."""

    def __init__(self, *, has_state: bool = True, principal: Any = None) -> None:
        self.accepted = False
        self.close_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.sent_texts: list[Any] = []
        if has_state:
            self.state = SimpleNamespace(uterm_principal=principal)

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *args: Any, **kwargs: Any) -> None:
        self.close_calls.append((args, kwargs))

    async def send_text(self, text: Any) -> None:
        self.sent_texts.append(text)

    async def receive_text(self) -> str:
        raise WebSocketDisconnect


class _FakeHub:
    """Records every collaborator call's exact arguments; never asserts."""

    def __init__(self) -> None:
        self.resolve_role_calls: list[tuple[Any, Any]] = []
        self.resolve_role_result: Any = "viewer"
        self.register_browser_calls: list[tuple[Any, Any, Any, Any]] = []
        self.register_browser_result: dict[str, Any] = {
            "is_hijacked": False,
            "hijacked_by_me": HIJACKED_BY_ME_SENTINEL,
            "worker_online": True,
            "input_mode": "normal",
            "initial_snapshot": None,
            "resume_token": None,
        }
        self.touch_activity_calls: list[Any] = []
        self.hijack_state_msg_calls: list[tuple[Any, Any]] = []
        self.hijack_state_msg_result: Any = HIJACK_STATE_SENTINEL
        self.request_snapshot_calls: list[Any] = []
        self.activate_broadcasts_calls: list[tuple[Any, Any]] = []
        self.ws_idle_timeout_s: float = 12.5
        self.max_ws_message_bytes = 10_000_000
        self.browser_rate_limit_per_sec = 100
        self.browser_control_rate_limit_per_sec = 100
        self.resume_store: Any = None
        self.metric_calls: list[str] = []
        self.cleanup_browser_disconnect_calls: list[tuple[Any, Any, Any]] = []
        self.cleanup_browser_disconnect_result: dict[str, Any] = {
            "was_owner": False,
            "rest_still_active": False,
            "resume_without_owner": False,
        }
        self.check_still_hijacked_calls: list[Any] = []
        self.broadcast_hijack_state_calls: list[Any] = []
        self.notify_hijack_changed_calls: list[tuple[Any, Any, Any]] = []
        self.append_event_calls: list[tuple[Any, Any, Any]] = []
        self.prune_if_idle_calls: list[Any] = []

    async def resolve_role_for_browser(self, websocket: Any, worker_id: Any) -> Any:
        self.resolve_role_calls.append((websocket, worker_id))
        if isinstance(self.resolve_role_result, BaseException):
            raise self.resolve_role_result
        return self.resolve_role_result

    async def register_browser(
        self, worker_id: Any, websocket: Any, role: Any, defer_broadcast: Any = _MISSING
    ) -> dict[str, Any]:
        self.register_browser_calls.append((worker_id, websocket, role, defer_broadcast))
        return self.register_browser_result

    async def touch_activity(self, worker_id: Any) -> None:
        self.touch_activity_calls.append(worker_id)

    async def hijack_state_msg_for(self, worker_id: Any, websocket: Any) -> Any:
        self.hijack_state_msg_calls.append((worker_id, websocket))
        return self.hijack_state_msg_result

    async def request_snapshot(self, worker_id: Any) -> None:
        self.request_snapshot_calls.append(worker_id)

    async def activate_browser_broadcasts(self, worker_id: Any, websocket: Any) -> None:
        self.activate_broadcasts_calls.append((worker_id, websocket))

    def metric(self, name: str) -> None:
        self.metric_calls.append(name)

    async def cleanup_browser_disconnect(self, worker_id: Any, websocket: Any, owned_hijack: Any) -> dict[str, Any]:
        self.cleanup_browser_disconnect_calls.append((worker_id, websocket, owned_hijack))
        return self.cleanup_browser_disconnect_result

    async def check_still_hijacked(self, worker_id: Any) -> bool:
        self.check_still_hijacked_calls.append(worker_id)
        return False

    async def broadcast_hijack_state(self, worker_id: Any) -> None:
        self.broadcast_hijack_state_calls.append(worker_id)

    def notify_hijack_changed(self, worker_id: Any, *, enabled: Any, owner: Any) -> None:
        self.notify_hijack_changed_calls.append((worker_id, enabled, owner))

    async def append_event(self, worker_id: Any, kind: Any, payload: Any) -> None:
        self.append_event_calls.append((worker_id, kind, payload))

    async def prune_if_idle(self, worker_id: Any) -> None:
        self.prune_if_idle_calls.append(worker_id)


class _FakeLogger:
    def __init__(self) -> None:
        self.info_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.warning_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.debug_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def info(self, *args: Any, **kwargs: Any) -> None:
        self.info_calls.append((args, kwargs))

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warning_calls.append((args, kwargs))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self.debug_calls.append((args, kwargs))


class _TracerRecorder:
    def __init__(self) -> None:
        self.get_tracer_calls: list[Any] = []
        self.span_names: list[Any] = []
        self.span_sentinel = object()


class _FakeSpanCM:
    def __init__(self, recorder: _TracerRecorder, name: Any) -> None:
        self._recorder = recorder
        self._name = name

    def __enter__(self) -> Any:
        self._recorder.span_names.append(self._name)
        return self._recorder.span_sentinel

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeTracer:
    def __init__(self, recorder: _TracerRecorder) -> None:
        self._recorder = recorder

    def start_as_current_span(self, name: Any) -> _FakeSpanCM:
        return _FakeSpanCM(self._recorder, name)


def _patch_hello_and_frame(monkeypatch: Any) -> list[dict[str, Any]]:
    """Replace ``make_hello_frame`` with a recorder and ``encode_control_frame``
    with identity, so ``send_text`` payloads and hello kwargs are both exact
    and inspectable without real control-frame byte-framing.
    """
    hello_calls: list[dict[str, Any]] = []

    def _fake_make_hello_frame(**kwargs: Any) -> dict[str, Any]:
        hello_calls.append(kwargs)
        return HELLO_RETURN_SENTINEL

    monkeypatch.setattr(websockets_impl, "make_hello_frame", _fake_make_hello_frame)
    monkeypatch.setattr(websockets_impl, "encode_control_frame", lambda x: x)
    return hello_calls


def _patch_tracer(monkeypatch: Any) -> _TracerRecorder:
    recorder = _TracerRecorder()

    def _fake_get_tracer(name: Any) -> _FakeTracer:
        recorder.get_tracer_calls.append(name)
        return _FakeTracer(recorder)

    monkeypatch.setattr(websockets_impl, "get_tracer", _fake_get_tracer)
    return recorder


def _patch_span_attrs(monkeypatch: Any) -> list[tuple[Any, dict[str, Any]]]:
    calls: list[tuple[Any, dict[str, Any]]] = []

    def _fake_set_ws_span_attrs(span: Any, **kwargs: Any) -> None:
        calls.append((span, kwargs))

    monkeypatch.setattr(websockets_impl, "_set_ws_span_attrs", _fake_set_ws_span_attrs)
    return calls


async def _run(hub: Any, websocket: Any, worker_id: str = WORKER_ID) -> None:
    # Uses the real, pre-monkeypatch ``wait_for`` reference so this bounding
    # wrapper is unaffected by tests that fake ``asyncio.wait_for`` on the
    # ``websockets_impl`` module (which is the very same ``asyncio`` module
    # object, since both import it at module scope).
    await _real_wait_for(_ws_browser_term(hub, websocket, worker_id), timeout=2)


# ---------------------------------------------------------------------------
# Big cluster: UTERM_TEST_MODE=1 admin path -> registration -> tracer ->
# hello payload -> hijack_state_msg_for -> periodic-cleanup task -> idle
# timeout.
# ---------------------------------------------------------------------------


async def test_admin_test_mode_connect_sends_exact_hello_then_idle_timeout(monkeypatch: Any) -> None:
    """Kills mutmut_2/3/5 (the ``UTERM_TEST_MODE`` env lookup: wrong key name,
    lowercased key name, and comparison value ``"1"`` -> ``"XX1XX"`` — any of
    these makes the check fail with the real env var set to exactly ``"1"``,
    falling through to ``hub.resolve_role_for_browser`` instead of the
    literal admin assignment; caught below via ``resolve_role_calls == []``),
    mutmut_6/7/8 (``role = "admin"`` -> ``None``/``"XXadminXX"``/``"ADMIN"``
    — caught via the exact ``"role"`` field in the recorded hello kwargs),
    mutmut_27/28 (``can_hijack = role == "admin"`` -> ``role ==
    "XXadminXX"``/``"ADMIN"`` — with the real ``role == "admin"`` this is
    ``True``; both mutants make it ``False``, caught via the exact
    ``"can_hijack"`` field), mutmut_36/40/41 (``register_browser(...,
    defer_broadcast=True)`` -> ``None``/dropped/``False`` — caught via the
    exact recorded call tuple, using a sentinel default so a dropped kwarg
    doesn't crash the fake but instead mismatches), mutmut_42
    (``touch_activity(worker_id)`` -> ``touch_activity(None)`` — caught via
    the first recorded call), mutmut_43-56 (every argument to
    ``get_tracer``/``start_as_current_span``/``_set_ws_span_attrs`` — caught
    via the exact recorded tracer-name, span-name, and span-attrs calls),
    mutmut_60 (``hijacked_by_me = browser_state["hijacked_by_me"]`` ->
    ``None`` — caught via a non-None/non-bool sentinel value in the fake
    ``browser_state`` that must appear verbatim in the hello kwargs),
    mutmut_83/84/91-106/112/113 (every key/value literal case and the
    ``hijack_step_supported`` bool in the hello kwargs, including the nested
    ``capabilities`` dict — caught by comparing the exact recorded hello
    kwargs dict), mutmut_137 (``hijack_state_msg_for(worker_id, websocket)``
    -> ``(worker_id, None)`` — caught via the exact recorded call tuple),
    mutmut_189/192 (the periodic-cleanup task creation dropped to ``None``,
    or ``worker_id`` swapped for ``None`` — caught via a fake
    ``_periodic_hijack_cleanup`` that records its call args synchronously;
    the RHS-to-``None`` mutant never calls it at all), and mutmut_206/209/210
    (``wait_for(..., timeout=hub.ws_idle_timeout_s)`` -> ``timeout=None``, and
    the idle-timeout log's format string/``worker_id`` -> ``None`` — caught
    via a faked ``wait_for`` that records the exact timeout it was given and
    a fake logger that records the exact ``info`` call).
    """
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    hello_calls = _patch_hello_and_frame(monkeypatch)
    tracer_recorder = _patch_tracer(monkeypatch)
    span_attr_calls = _patch_span_attrs(monkeypatch)

    periodic_cleanup_calls: list[tuple[Any, Any, Any]] = []

    def _fake_periodic_hijack_cleanup(hub_arg: Any, worker_id_arg: Any, interval_arg: Any) -> Any:
        periodic_cleanup_calls.append((hub_arg, worker_id_arg, interval_arg))

        async def _noop() -> None:
            return None

        return _noop()

    monkeypatch.setattr(websockets_impl, "_periodic_hijack_cleanup", _fake_periodic_hijack_cleanup)

    wait_for_timeouts: list[Any] = []

    async def _fake_wait_for(aw: Any, timeout: Any = None) -> Any:
        wait_for_timeouts.append(timeout)
        aw.close()
        raise TimeoutError

    # ``websockets_impl.asyncio`` is literally this same ``asyncio`` module
    # object (both import it at module scope), so patching the attribute
    # here is exactly equivalent to patching it "on" ``websockets_impl``.
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)

    fake_logger = _FakeLogger()
    monkeypatch.setattr(websockets_impl, "logger", fake_logger)

    hub = _FakeHub()
    ws = _FakeWebSocket()

    await _run(hub, ws)

    assert ws.accepted is True
    # The literal-admin branch was taken; resolve_role_for_browser must not
    # have been consulted at all.
    assert hub.resolve_role_calls == []

    assert hub.register_browser_calls == [(WORKER_ID, ws, "admin", True)]
    assert hub.touch_activity_calls[0] == WORKER_ID

    assert tracer_recorder.get_tracer_calls == [websockets_impl.__name__]
    assert tracer_recorder.span_names == ["uterm.ws.browser.connect"]
    assert span_attr_calls == [
        (
            tracer_recorder.span_sentinel,
            {"worker_id": WORKER_ID, "operation": "ws.browser.connect", "role": "admin"},
        )
    ]

    expected_hello_kwargs = {
        "worker_id": WORKER_ID,
        "can_hijack": True,
        "hijacked": False,
        "hijacked_by_me": HIJACKED_BY_ME_SENTINEL,
        "worker_online": True,
        "input_mode": "normal",
        "role": "admin",
        "hijack_control": "ws",
        "hijack_step_supported": True,
        "capabilities": {
            "hijack_control": "ws",
            "hijack_step_supported": True,
        },
        "resume_supported": False,
        "resume_token": None,
        "protocol_version": CURRENT_PROTOCOL_VERSION,
        "protocol": {
            "selected": PREFERRED_PROTOCOL_VERSION,
            "server_min": MIN_PROTOCOL_VERSION,
            "server_max": MAX_PROTOCOL_VERSION,
        },
    }
    assert hello_calls == [expected_hello_kwargs]

    assert hub.hijack_state_msg_calls == [(WORKER_ID, ws)]
    assert ws.sent_texts == [HELLO_RETURN_SENTINEL, HIJACK_STATE_SENTINEL]
    assert hub.request_snapshot_calls == [WORKER_ID]

    assert periodic_cleanup_calls == [(hub, WORKER_ID, websockets_impl._BROWSER_HIJACK_CLEANUP_INTERVAL_S)]

    assert wait_for_timeouts == [hub.ws_idle_timeout_s]
    assert fake_logger.info_calls == [(("ws_browser_idle_timeout worker_id=%s", WORKER_ID), {})]


# ---------------------------------------------------------------------------
# hub.resolve_role_for_browser: exact call args.
# ---------------------------------------------------------------------------


async def test_resolves_role_via_hub_with_exact_args(monkeypatch: Any) -> None:
    """Kills mutmut_10/11 (``hub.resolve_role_for_browser(websocket,
    worker_id)`` -> ``(None, worker_id)``/``(websocket, None)`` — caught via
    the exact recorded call tuple; a ``None`` in either slot mismatches the
    real websocket/worker_id).
    """
    monkeypatch.delenv("UTERM_TEST_MODE", raising=False)
    _patch_hello_and_frame(monkeypatch)
    _patch_tracer(monkeypatch)
    _patch_span_attrs(monkeypatch)

    hub = _FakeHub()
    hub.resolve_role_result = "admin"
    ws = _FakeWebSocket()

    await _run(hub, ws)

    assert hub.resolve_role_calls == [(ws, WORKER_ID)]


# ---------------------------------------------------------------------------
# BrowserRoleResolutionError -> close(code=1008, reason=...) -> return.
# ---------------------------------------------------------------------------


async def test_role_resolution_error_closes_with_exact_code_and_reason_then_returns(monkeypatch: Any) -> None:
    """Kills mutmut_14 (``code=1008`` -> ``code=None``), mutmut_15
    (``reason="browser role resolution failed"`` -> ``reason=None``),
    mutmut_16 (the ``code`` kwarg dropped entirely), mutmut_17 (the
    ``reason`` kwarg dropped entirely), mutmut_18 (``code=1008`` ->
    ``code=1009``), and mutmut_19/20 (the reason text's content/case
    mutated) — all caught via one exact-equality assertion on the recorded
    ``close`` call's args/kwargs. Also asserts the function returns
    immediately: ``register_browser`` (which runs after role resolution)
    must never be reached.
    """
    monkeypatch.delenv("UTERM_TEST_MODE", raising=False)
    _patch_hello_and_frame(monkeypatch)
    _patch_tracer(monkeypatch)
    _patch_span_attrs(monkeypatch)

    hub = _FakeHub()
    hub.resolve_role_result = BrowserRoleResolutionError("nope")
    ws = _FakeWebSocket()

    await _run(hub, ws)

    assert ws.close_calls == [((), {"code": 1008, "reason": "browser role resolution failed"})]
    assert hub.register_browser_calls == []


# ---------------------------------------------------------------------------
# Invalid resolved role -> defensive fallback to "viewer".
# ---------------------------------------------------------------------------


async def test_role_not_in_valid_roles_falls_back_to_exact_viewer_string(monkeypatch: Any) -> None:
    """Kills mutmut_22/23/24 (``role = "viewer"`` ->
    ``None``/``"XXviewerXX"``/``"VIEWER"`` in the ``role not in VALID_ROLES``
    defensive branch — caught via the exact ``"role"`` field of the recorded
    hello kwargs, which must be exactly the lowercase string ``"viewer"``).
    """
    monkeypatch.delenv("UTERM_TEST_MODE", raising=False)
    hello_calls = _patch_hello_and_frame(monkeypatch)
    _patch_tracer(monkeypatch)
    _patch_span_attrs(monkeypatch)

    hub = _FakeHub()
    hub.resolve_role_result = "not-a-real-role"
    ws = _FakeWebSocket()

    await _run(hub, ws)

    assert hello_calls[0]["role"] == "viewer"


# ---------------------------------------------------------------------------
# owned_hijack initialized to False (observed via the finally block's
# cleanup_browser_disconnect call, since nothing else in this early slice
# mutates it).
# ---------------------------------------------------------------------------


async def test_owned_hijack_starts_as_exact_false(monkeypatch: Any) -> None:
    """Kills mutmut_29 (``owned_hijack = False`` -> ``None``) — caught via
    the third argument of the recorded ``cleanup_browser_disconnect`` call in
    the ``finally`` block, which must be exactly ``False`` (``None == False``
    is ``False`` in Python, so equality alone distinguishes them).
    """
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    _patch_hello_and_frame(monkeypatch)
    _patch_tracer(monkeypatch)
    _patch_span_attrs(monkeypatch)

    hub = _FakeHub()
    ws = _FakeWebSocket()

    await _run(hub, ws)

    assert len(hub.cleanup_browser_disconnect_calls) == 1
    _worker_id_arg, _ws_arg, owned_hijack_arg = hub.cleanup_browser_disconnect_calls[0]
    assert owned_hijack_arg is False


# ---------------------------------------------------------------------------
# DeckMux principal resolution + connect call, normal (non-test-mode) path.
# ---------------------------------------------------------------------------


async def test_deckmux_principal_resolved_from_websocket_state_and_passed_through(monkeypatch: Any) -> None:
    """Kills mutmut_155 (``_dm_principal = getattr(getattr(websocket,
    "state", None), "uterm_principal", None)`` -> ``None`` outright),
    mutmut_156 (the inner ``getattr``'s object -> ``None``), mutmut_161 (the
    outer ``getattr``'s ``websocket`` -> ``None``), mutmut_166/167 (the
    ``"state"`` key's case/content mutated), and mutmut_168/169 (the
    ``"uterm_principal"`` key's case/content mutated) — all caught by one
    sentinel identity check: with ``websocket.state.uterm_principal`` set to
    a distinct sentinel, any of these mutations makes the resolved principal
    ``None`` instead of the sentinel. Also kills mutmut_171-178 (every
    positional argument to ``_dm_connect(worker_id, websocket, role,
    principal=_dm_principal)`` replaced with ``None`` or dropped, which
    shifts the remaining positional arguments) — caught via one
    exact-equality assertion on the recorded ``*args, **kwargs`` call.
    """
    monkeypatch.delenv("UTERM_TEST_MODE", raising=False)
    _patch_hello_and_frame(monkeypatch)
    _patch_tracer(monkeypatch)
    _patch_span_attrs(monkeypatch)

    dm_connect_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _fake_dm_connect(*args: Any, **kwargs: Any) -> Any:
        dm_connect_calls.append((args, kwargs))
        return None

    hub = _FakeHub()
    hub.resolve_role_result = "admin"
    hub.deckmux_on_browser_connect = _fake_dm_connect  # type: ignore[attr-defined]
    ws = _FakeWebSocket(has_state=True, principal=DM_PRINCIPAL_SENTINEL)

    await _run(hub, ws)

    assert dm_connect_calls == [((WORKER_ID, ws, "admin"), {"principal": DM_PRINCIPAL_SENTINEL})]


async def test_deckmux_principal_defaults_to_none_when_websocket_has_no_state_attr(monkeypatch: Any) -> None:
    """Kills mutmut_165 (the inner ``getattr(websocket, "state", None)``'s
    ``None`` default dropped, i.e. ``getattr(websocket, "state")``) — with a
    websocket that genuinely has no ``.state`` attribute, the real code's
    default makes ``_dm_principal`` resolve to ``None`` and the function
    proceeds normally (``_dm_connect`` gets called); the mutant instead
    raises ``AttributeError`` from inside the ``try`` block, which the
    function's broad ``except Exception`` swallows *before* ``_dm_connect``
    is ever invoked. Caught via ``dm_connect_calls`` being non-empty only on
    the real code.
    """
    monkeypatch.delenv("UTERM_TEST_MODE", raising=False)
    _patch_hello_and_frame(monkeypatch)
    _patch_tracer(monkeypatch)
    _patch_span_attrs(monkeypatch)

    dm_connect_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _fake_dm_connect(*args: Any, **kwargs: Any) -> Any:
        dm_connect_calls.append((args, kwargs))
        return None

    hub = _FakeHub()
    hub.resolve_role_result = "admin"
    hub.deckmux_on_browser_connect = _fake_dm_connect  # type: ignore[attr-defined]
    ws = _FakeWebSocket(has_state=False)
    assert not hasattr(ws, "state")

    await _run(hub, ws)

    assert len(dm_connect_calls) == 1
    _args, kwargs = dm_connect_calls[0]
    assert kwargs == {"principal": None}


async def test_deckmux_principal_forced_none_in_test_mode_even_with_real_state(monkeypatch: Any) -> None:
    """Kills mutmut_151/152 (the second ``UTERM_TEST_MODE`` env lookup's key
    name mutated — a wrong key always reads as unset, so the ``!= "1"``
    check would wrongly be ``True`` and resolve the principal from
    ``websocket.state`` instead of forcing ``None``), mutmut_153 (``!=
    "1"`` -> ``== "1"``, flipping the guard so it takes the getattr branch
    when test-mode IS set), and mutmut_154 (the comparison value ``"1"`` ->
    ``"XX1XX"``, same effect). All four are caught by one assertion: with
    ``UTERM_TEST_MODE=1`` and a real, non-``None`` ``websocket.state.uterm_principal``,
    the real code must still pass ``principal=None`` to ``_dm_connect``.
    """
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    _patch_hello_and_frame(monkeypatch)
    _patch_tracer(monkeypatch)
    _patch_span_attrs(monkeypatch)

    dm_connect_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _fake_dm_connect(*args: Any, **kwargs: Any) -> Any:
        dm_connect_calls.append((args, kwargs))
        return None

    hub = _FakeHub()
    hub.deckmux_on_browser_connect = _fake_dm_connect  # type: ignore[attr-defined]
    ws = _FakeWebSocket(has_state=True, principal=DM_PRINCIPAL_SENTINEL)

    await _run(hub, ws)

    assert len(dm_connect_calls) == 1
    _args, kwargs = dm_connect_calls[0]
    assert kwargs == {"principal": None}
