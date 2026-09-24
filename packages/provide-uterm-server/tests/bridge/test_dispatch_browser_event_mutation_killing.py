#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``websockets_browser.dispatch_browser_event``.

Kill-suite only -- behavioural coverage of the browser recv loop lives in
``test_websockets_browser_*.py`` and the ``test_browser_handlers_*.py`` suites.
Those drive this function through a real ``TermHub`` and ``AsyncMock``/real
sockets that answer whatever they are asked, never pinning the exact argument
tuple a collaborator receives -- so a ``None`` swapped in for one argument, or
one argument dropped (shortening the tuple), still lets the call succeed. Some
of those are the ``TIMEOUT`` ids below: a wrong argument into the *real*
``_handle_resume``/``handle_browser_message`` does not raise, it changes what
gets awaited into something that never completes, so mutmut's real-collaborator
run hangs instead of failing. Nothing sends a ``presence_update``,
``queued_input``, ``control_request`` or ``fanout_send`` frame either, so the
``deckmux``/``fan_out_controller`` ``getattr`` chains, the ``UTERM_TEST_MODE``
principal gate, the fan-out admin gate and the ``fanout_result`` field names
are all unexercised. And nothing distinguishes the input- from the
control-rate-limit branch at the ``mtype == "input"`` boundary, or makes
``send_text`` fail to check ``suppress(Exception)`` actually swallows it.

Every collaborator here is a strict, synchronous-fast stand-in that asserts
the exact arguments it receives (``_Recorder``), or a hub/socket/bucket whose
attributes are genuinely *absent* unless a test sets them -- so a mutant that
drops a ``getattr(..., default)`` fallback crashes with ``AttributeError``
instead of quietly finding a value, and a mutant that drops or swaps an
argument fails an ``assert`` instantly instead of hanging.

Documented equivalents (not added to ``mutation_equivalents.toml`` -- this
suite does not touch that file; noted here for the record):

- mutmut_188 (``_fo_is_admin = False`` -> ``= None`` inside the fan-out
  ``except Exception:`` handler): the only later use is ``if not
  _fo_is_admin:``, and ``not None == not False``. Nothing downstream ever
  reads the value itself, so no input distinguishes the two.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.control_channel import DataChunk, encode_control_frame
from provide.uterm.server.bridge.fanout._models import FanOutResult, SessionFanOutResult
from provide.uterm.server.bridge.frames import make_error_frame
from provide.uterm.server.bridge.routes import browser_handlers, websockets_browser
from provide.uterm.server.bridge.routes.websockets_browser import dispatch_browser_event

WID = "dispatch-worker"
ROLE_IN = "role-in-marker"
CAN_HIJACK_IN = "can-hijack-in-marker"
OWNED_IN = "owned-hijack-in-marker"
_MISSING = object()


def _error(text: str) -> str:
    return encode_control_frame(make_error_frame(text))


class _Recorder:
    """A strict async stand-in for one of dispatch_browser_event's collaborators.

    Asserts the exact positional-argument tuple and keyword-argument mapping
    it is called with. A ``None`` swapped in for one argument, one argument
    dropped (which changes the tuple's length), or a keyword value replaced,
    fails the assertion instantly instead of silently succeeding -- and,
    because this is a plain fast function with no real I/O, it can never hang
    the way a wrong call into the real collaborator sometimes does.
    """

    def __init__(
        self,
        expected_args: tuple[Any, ...] = (),
        expected_kwargs: dict[str, Any] | None = None,
        ret: Any = None,
    ) -> None:
        self._expected_args = expected_args
        self._expected_kwargs = expected_kwargs or {}
        self._ret = ret
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        assert args == self._expected_args, f"called with args={args!r}, expected {self._expected_args!r}"
        assert kwargs == self._expected_kwargs, f"called with kwargs={kwargs!r}, expected {self._expected_kwargs!r}"
        self.calls.append((args, kwargs))
        return self._ret


class _Bucket:
    """A rate-limit bucket stand-in with a fixed, recorded ``allow()`` outcome."""

    def __init__(self, allowed: bool) -> None:
        self._allowed = allowed
        self.calls = 0

    def allow(self) -> bool:
        self.calls += 1
        return self._allowed


class _WS:
    """A browser socket that records outbound frames.

    ``state``/``app`` are genuinely absent attributes unless a test passes
    them in, matching the real ``fastapi.WebSocket`` shape closely enough to
    let a dropped-``getattr``-default mutant crash on the missing attribute
    instead of silently finding one.
    """

    def __init__(self, *, fail: bool = False, state: Any = _MISSING, app: Any = _MISSING) -> None:
        self.sent: list[str] = []
        self._fail = fail
        if state is not _MISSING:
            self.state = state
        if app is not _MISSING:
            self.app = app

    async def send_text(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("send failed")
        assert isinstance(text, str), f"send_text got {text!r}"
        self.sent.append(text)


class _Hub:
    """A hub stand-in exposing only what ``dispatch_browser_event`` touches.

    ``deckmux_handle_message``, ``fan_out_controller`` and
    ``get_worker_browser_role`` are genuinely undefined unless a test sets
    them -- matching production's ``getattr(hub, "...", None)`` fallback, and
    letting a mutant that drops that fallback's default crash with
    ``AttributeError`` rather than quietly finding a value.
    """

    def __init__(self) -> None:
        self.metric_calls: list[tuple[str, int]] = []
        self.touch_calls: list[str] = []
        self.resume_store: Any = None

    def metric(self, name: str, value: int = 1) -> None:
        assert isinstance(name, str), f"metric got name={name!r}"
        self.metric_calls.append((name, value))

    async def touch_activity(self, worker_id: str) -> None:
        assert isinstance(worker_id, str), f"touch_activity got worker_id={worker_id!r}"
        self.touch_calls.append(worker_id)


class _Logger:
    def __init__(self) -> None:
        self.warning_calls: list[tuple[Any, ...]] = []
        self.debug_calls: list[tuple[Any, ...]] = []

    def warning(self, *args: Any, **kwargs: Any) -> None:
        assert not kwargs, f"logger.warning got kwargs {kwargs!r}"
        self.warning_calls.append(args)

    def debug(self, *args: Any, **kwargs: Any) -> None:
        assert not kwargs, f"logger.debug got kwargs {kwargs!r}"
        self.debug_calls.append(args)


@pytest.fixture(autouse=True)
def _pinned_logger(monkeypatch: pytest.MonkeyPatch) -> _Logger:
    log = _Logger()
    monkeypatch.setattr(websockets_browser, "logger", log)
    return log


def _patch_hbm(monkeypatch: pytest.MonkeyPatch, expected_args: tuple[Any, ...], ret: Any) -> _Recorder:
    """Patch ``websockets_browser.handle_browser_message`` (module-level import binding)."""
    recorder = _Recorder(expected_args, ret=ret)
    monkeypatch.setattr(websockets_browser, "handle_browser_message", recorder)
    return recorder


async def _dispatch(
    hub: Any,
    ws: Any,
    event: Any,
    *,
    role: str = ROLE_IN,
    can_hijack: Any = CAN_HIJACK_IN,
    owned_hijack: Any = OWNED_IN,
    browser_bucket: Any = None,
    browser_control_bucket: Any = None,
) -> tuple[str, Any, Any]:
    return await dispatch_browser_event(
        hub,
        ws,
        WID,
        role,
        can_hijack,
        owned_hijack,
        event,
        browser_bucket if browser_bucket is not None else _Bucket(True),
        browser_control_bucket if browser_control_bucket is not None else _Bucket(True),
    )


# ---------------------------------------------------------------------------
# msg_b construction + the bottom touch_activity/handle_browser_message block
# ---------------------------------------------------------------------------


async def test_a_data_chunk_becomes_an_exact_input_message_reaching_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_2 (``isinstance`` forced always-False: a ``DataChunk``
    then falls to ``event.control``, which it lacks -- ``AttributeError``),
    4-9 (the ``{"type": "input", "data": ...}`` dict mangled), 18 (``and
    browser_bucket.allow()`` instead of ``and not ...``: an allowing bucket
    would wrongly rate-limit and never reach the handler), 263-265 (the
    ``"input"`` membership gating ``touch_activity``), 270
    (``touch_activity(None)``) and 271-283 (every argument substitution/
    omission on ``handle_browser_message`` -- ``TIMEOUT`` ids 271/273/276/
    278-283 fail this recorder's ``assert`` instantly instead of hanging)."""
    hub, ws = _Hub(), _WS()
    msg_b = {"type": "input", "data": "hello"}
    recorder = _patch_hbm(monkeypatch, (hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), "HBM_RETURN")
    result = await _dispatch(hub, ws, DataChunk(data="hello"))
    assert result == (ROLE_IN, CAN_HIJACK_IN, "HBM_RETURN")
    assert hub.touch_calls == [WID]
    assert recorder.calls == [((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), {})]
    assert hub.metric_calls == []
    assert ws.sent == []


async def test_a_hijack_release_control_frame_also_touches_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_263 (the membership check inverted to ``not in``) and
    268/269 (the ``"hijack_release"`` literal mangled) via the third member
    of the ``touch_activity`` gate tuple."""
    hub, ws = _Hub(), _WS()
    msg_b = {"type": "hijack_release"}
    recorder = _patch_hbm(monkeypatch, (hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), "HBM_RETURN")
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == (ROLE_IN, CAN_HIJACK_IN, "HBM_RETURN")
    assert hub.touch_calls == [WID]
    assert recorder.calls == [((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), {})]


# ---------------------------------------------------------------------------
# The input-rate-limit branch: ``mtype == "input" and not browser_bucket.allow()``
# ---------------------------------------------------------------------------


async def test_the_input_bucket_denying_an_input_message_sends_rate_limited_and_stops(
    monkeypatch: pytest.MonkeyPatch, _pinned_logger: _Logger
) -> None:
    """Kills mutmut_15-18 (``mtype == "input"``, tested with a genuine
    ``"input"``), 19-21 (``metric`` literal), 22-27 (``warning``
    literal/args) and 29-33 (``send_text``/``make_error_frame`` literal/
    ``None`` substitutions)."""
    hub, ws = _Hub(), _WS()
    recorder = _patch_hbm(monkeypatch, (), None)
    bucket = _Bucket(False)
    result = await _dispatch(hub, ws, DataChunk(data="x"), browser_bucket=bucket)
    assert result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert hub.metric_calls == [("ws_browser_rate_limited_total", 1)]
    assert _pinned_logger.warning_calls == [("ws_browser_rate_limited worker_id=%s", WID)]
    assert _pinned_logger.debug_calls == [("ws_browser_rate_limited_sent worker_id=%s", WID)]
    assert ws.sent == [_error("rate_limited")]
    assert recorder.calls == []
    assert bucket.calls == 1


async def test_the_input_rate_limit_send_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_28 (``suppress(Exception)`` -> ``suppress(None)``): once
    ``send_text`` actually raises, ``suppress(None)`` makes
    ``contextlib.suppress.__exit__`` itself raise ``TypeError`` (``issubclass()
    arg 2 must be a class...``), which escapes ``dispatch_browser_event``
    instead of being swallowed -- breaking this test's plain return."""
    hub = _Hub()
    ws = _WS(fail=True)
    _patch_hbm(monkeypatch, (), None)
    result = await _dispatch(hub, ws, DataChunk(data="x"), browser_bucket=_Bucket(False))
    assert result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert hub.metric_calls == [("ws_browser_rate_limited_total", 1)]


async def test_a_denied_input_bucket_is_never_consulted_for_a_non_input_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_14 (``mtype == "input" or not browser_bucket.allow()``):
    with a non-``"input"`` ``mtype``, the real ``and`` short-circuits before
    calling ``browser_bucket.allow()``, but the ``or`` mutant calls it and
    wrongly rate-limits when it denies. Reinforces 15 and 266/267 (the
    ``"hijack_request"`` literal, via ``touch_activity``)."""
    hub, ws = _Hub(), _WS()
    msg_b = {"type": "hijack_request"}
    recorder = _patch_hbm(monkeypatch, (hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), "HBM_RETURN")
    browser_bucket, control_bucket = _Bucket(False), _Bucket(True)
    result = await _dispatch(
        hub, ws, SimpleNamespace(control=msg_b), browser_bucket=browser_bucket, browser_control_bucket=control_bucket
    )
    assert result == (ROLE_IN, CAN_HIJACK_IN, "HBM_RETURN")
    assert browser_bucket.calls == 0
    assert hub.metric_calls == []
    assert hub.touch_calls == [WID]
    assert recorder.calls == [((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), {})]


# ---------------------------------------------------------------------------
# The control-rate-limit branch:
# ``mtype is not None and mtype != "input" and not browser_control_bucket.allow()``
# ---------------------------------------------------------------------------


async def test_the_control_bucket_denying_a_non_input_message_sends_rate_limited_and_stops(
    monkeypatch: pytest.MonkeyPatch, _pinned_logger: _Logger
) -> None:
    """Kills mutmut_42/43 (``is not None``/``!=`` flipped, tested with a
    genuinely non-``None``, non-``"input"`` ``mtype``), 47-49 (``metric``
    literal), 50-57 (``warning`` literal/args) and 59-63 (``send_text``
    literal/``None`` substitutions)."""
    hub, ws = _Hub(), _WS()
    recorder = _patch_hbm(monkeypatch, (), None)
    msg_b = {"type": "hijack_request"}
    bucket = _Bucket(False)
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b), browser_control_bucket=bucket)
    assert result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert hub.metric_calls == [("ws_browser_control_rate_limited_total", 1)]
    assert _pinned_logger.warning_calls == [
        ("ws_browser_control_rate_limited worker_id=%s mtype=%s", WID, "hijack_request")
    ]
    assert ws.sent == [_error("rate_limited")]
    assert recorder.calls == []
    assert bucket.calls == 1


async def test_the_control_rate_limit_send_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_58 (``suppress(Exception)`` -> ``suppress(None)``) on the
    control-rate-limit branch's ``send_text``."""
    hub = _Hub()
    ws = _WS(fail=True)
    _patch_hbm(monkeypatch, (), None)
    msg_b = {"type": "hijack_request"}
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b), browser_control_bucket=_Bucket(False))
    assert result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert hub.metric_calls == [("ws_browser_control_rate_limited_total", 1)]


async def test_an_input_message_never_hits_the_control_rate_limit_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_44/45 (``!= "input"`` mangled to ``"XXinputXX"``/
    ``"INPUT"``): with ``mtype`` genuinely ``"input"``, the real check is
    ``False`` and the branch is skipped regardless of the control bucket;
    the mangled literal makes it ``True``, wrongly entering when the control
    bucket denies."""
    hub, ws = _Hub(), _WS()
    msg_b = {"type": "input", "data": "z"}
    recorder = _patch_hbm(monkeypatch, (hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), "HBM_RETURN")
    result = await _dispatch(
        hub, ws, DataChunk(data="z"), browser_bucket=_Bucket(True), browser_control_bucket=_Bucket(False)
    )
    assert result == (ROLE_IN, CAN_HIJACK_IN, "HBM_RETURN")
    assert hub.metric_calls == []
    assert ws.sent == []
    assert recorder.calls == [((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), {})]


# ---------------------------------------------------------------------------
# The resume branch: ``mtype == "resume" and hub.resume_store is not None``
# ---------------------------------------------------------------------------


async def test_resume_dispatches_to_its_handler_and_recomputes_role_for_a_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_66-68 (the ``"resume"`` literal and ``is not None`` flip,
    tested with a genuine ``resume_store``), 69-81 (every argument on the
    ``_handle_resume`` call -- ``TIMEOUT`` ids 69-72/74/76-81 fail this
    recorder's ``assert`` instantly instead of hanging), 82 (``role = None``),
    84-87 (``get_worker_browser_role``'s arguments, including its ``TIMEOUT``
    dropped-argument variants 86/87) and 88/89 (``can_hijack`` from a
    non-``"admin"`` role)."""
    hub = _Hub()
    hub.resume_store = object()
    ws = _WS()
    msg_b = {"type": "resume", "token": "tok-1"}
    resume_recorder = _Recorder((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), ret="OWNED_AFTER_RESUME")
    monkeypatch.setattr(browser_handlers, "_handle_resume", resume_recorder)
    role_recorder = _Recorder((WID, ws), ret="operator")
    hub.get_worker_browser_role = role_recorder
    hbm_recorder = _patch_hbm(monkeypatch, (), None)
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == ("operator", False, "OWNED_AFTER_RESUME")
    assert resume_recorder.calls == [((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), {})]
    assert role_recorder.calls == [((WID, ws), {})]
    assert hbm_recorder.calls == []


async def test_resume_grants_can_hijack_for_the_admin_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_90/91 (the ``"admin"`` literal mangled): a non-``"admin"``
    role gives the same ``False`` for ``can_hijack`` regardless of the
    mangled literal (as in the test above), so this needs ``role`` to
    genuinely become ``"admin"``."""
    hub = _Hub()
    hub.resume_store = object()
    ws = _WS()
    msg_b = {"type": "resume", "token": "tok-2"}
    monkeypatch.setattr(browser_handlers, "_handle_resume", _Recorder((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN)))
    hub.get_worker_browser_role = _Recorder((WID, ws), ret="admin")
    _patch_hbm(monkeypatch, (), None)
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == ("admin", True, None)


async def test_a_falsy_recomputed_role_falls_back_to_the_original_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_83 (``or`` -> ``and``): with the recomputed role falsy
    (``""``) and the original ``role`` truthy, ``or`` keeps the original
    role but ``and`` would return the falsy recomputed one."""
    hub = _Hub()
    hub.resume_store = object()
    ws = _WS()
    msg_b = {"type": "resume", "token": "tok-3"}
    monkeypatch.setattr(browser_handlers, "_handle_resume", _Recorder((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN)))
    hub.get_worker_browser_role = _Recorder((WID, ws), ret="")
    _patch_hbm(monkeypatch, (), None)
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == (ROLE_IN, False, None)


async def test_a_missing_resume_store_falls_through_to_the_generic_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_64 (``and`` -> ``or``): with ``mtype == "resume"`` true
    but ``resume_store`` genuinely ``None``, the real ``and`` skips the
    branch entirely; the ``or`` mutant would wrongly enter it anyway."""
    hub = _Hub()
    hub.resume_store = None
    ws = _WS()
    msg_b = {"type": "resume", "token": "tok-4"}
    resume_recorder = _Recorder((), ret=None)
    monkeypatch.setattr(browser_handlers, "_handle_resume", resume_recorder)
    hbm_recorder = _patch_hbm(monkeypatch, (hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), "HBM_RETURN")
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == (ROLE_IN, CAN_HIJACK_IN, "HBM_RETURN")
    assert resume_recorder.calls == []
    assert hbm_recorder.calls == [((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), {})]


async def test_a_non_resume_message_with_a_resume_store_falls_through_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_65 (``!=`` -> `==`): with ``mtype`` genuinely not
    ``"resume"`` but ``resume_store`` set, the real ``!=`` check skips the
    branch (its own ``==`` comparison is ``False``); the mutant's ``!=``
    check (now the whole first clause) is ``True``, wrongly entering it."""
    hub = _Hub()
    hub.resume_store = object()
    ws = _WS()
    msg_b = {"type": "hijack_request"}
    resume_recorder = _Recorder((), ret=None)
    monkeypatch.setattr(browser_handlers, "_handle_resume", resume_recorder)
    hbm_recorder = _patch_hbm(monkeypatch, (hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), "HBM_RETURN")
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == (ROLE_IN, CAN_HIJACK_IN, "HBM_RETURN")
    assert resume_recorder.calls == []
    assert hub.touch_calls == [WID]
    assert hbm_recorder.calls == [((hub, ws, WID, ROLE_IN, msg_b, OWNED_IN), {})]


# ---------------------------------------------------------------------------
# presence_update / queued_input / control_request -> deckmux_handle_message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mtype", ["presence_update", "queued_input", "control_request"])
async def test_each_deckmux_message_type_reaches_the_handler(monkeypatch: pytest.MonkeyPatch, mtype: str) -> None:
    """Kills mutmut_93-98 (each of the three membership literals mangled):
    parametrized so all three are individually pinned."""
    monkeypatch.setenv("UTERM_TEST_MODE", "1")  # keep the principal path trivial (forced None) for this check
    hub, ws = _Hub(), _WS()
    msg_b = {"type": mtype, "marker": "distinct"}
    recorder = _Recorder((WID, ws, msg_b), {"principal": None})
    hub.deckmux_handle_message = recorder
    hbm_recorder = _patch_hbm(monkeypatch, (), None)
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert recorder.calls == [((WID, ws, msg_b), {"principal": None})]
    assert hbm_recorder.calls == []


async def test_deckmux_reads_the_principal_from_the_socket_when_not_in_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_99-103/105/106 (the ``getattr(hub, "deckmux_handle_message",
    None)`` lookup mangled -- several crash with ``TypeError`` on a
    non-string attribute name, also failing this test), 107 (``is not None``
    -> ``is None``), 109-113 (``UTERM_TEST_MODE``, variable genuinely unset),
    114-123/125-128 (the ``uterm_principal`` getattr chain, with a populated
    ``state`` so a wrong name/object finds nothing) and 129-136 (every
    argument on the ``_dm_handle`` call)."""
    monkeypatch.delenv("UTERM_TEST_MODE", raising=False)
    principal = SimpleNamespace(subject_id="operator-1")
    hub = _Hub()
    ws = _WS(state=SimpleNamespace(uterm_principal=principal))
    msg_b = {"type": "presence_update", "marker": "distinct"}
    recorder = _Recorder((WID, ws, msg_b), {"principal": principal})
    hub.deckmux_handle_message = recorder
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert recorder.calls == [((WID, ws, msg_b), {"principal": principal})]


async def test_deckmux_forces_the_principal_to_none_in_test_mode_even_when_the_socket_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_108 (``_dm_msg_principal = None`` initial default mangled
    to ``""``), and reinforces 109-113 from the opposite direction: with
    ``UTERM_TEST_MODE`` genuinely ``"1"``, the real code never reads the
    socket's principal at all."""
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    principal = SimpleNamespace(subject_id="operator-2")
    hub = _Hub()
    ws = _WS(state=SimpleNamespace(uterm_principal=principal))
    msg_b = {"type": "queued_input", "marker": "distinct"}
    recorder = _Recorder((WID, ws, msg_b), {"principal": None})
    hub.deckmux_handle_message = recorder
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert recorder.calls == [((WID, ws, msg_b), {"principal": None})]


async def test_deckmux_defaults_the_principal_to_none_when_the_socket_has_no_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_124 (``getattr(websocket, "state")`` with its default
    dropped): with a socket that genuinely lacks ``.state``, the real default
    quietly gives ``None``; the mutant raises ``AttributeError``."""
    monkeypatch.delenv("UTERM_TEST_MODE", raising=False)
    hub = _Hub()
    ws = _WS()  # no state at all
    msg_b = {"type": "control_request", "marker": "distinct"}
    recorder = _Recorder((WID, ws, msg_b), {"principal": None})
    hub.deckmux_handle_message = recorder
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert recorder.calls == [((WID, ws, msg_b), {"principal": None})]


async def test_an_unwired_deckmux_leaves_the_message_quietly_undispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_104 (``getattr(hub, "deckmux_handle_message")`` with its
    default dropped): with a hub that genuinely has no such attribute, the
    real default quietly gives ``None`` and the function returns cleanly;
    the mutant raises ``AttributeError``."""
    hub = _Hub()  # no deckmux_handle_message attribute at all
    ws = _WS()
    msg_b = {"type": "presence_update", "marker": "distinct"}
    hbm_recorder = _patch_hbm(monkeypatch, (), None)
    result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert hbm_recorder.calls == []


# ---------------------------------------------------------------------------
# fanout_send
# ---------------------------------------------------------------------------


class _Authz:
    def __init__(self, *, admin_of: Any, ret: bool) -> None:
        self._admin_of = admin_of
        self._ret = ret
        self.calls: list[Any] = []

    async def is_admin(self, principal: Any) -> bool:
        assert principal is self._admin_of, f"is_admin got {principal!r}"
        self.calls.append(principal)
        return self._ret


class _FanOutCtrl:
    def __init__(self, *, group_id: str, subj: str, data: str, group: Any, result: Any) -> None:
        self._group_id = group_id
        self._subj = subj
        self._data = data
        self._group = group
        self._result = result
        self.get_group_calls: list[tuple[str, str]] = []
        self.send_calls: list[tuple[str, str, Any]] = []

    async def get_group(self, group_id: str, *, principal: str) -> Any:
        assert group_id == self._group_id, f"get_group got group_id={group_id!r}"
        assert principal == self._subj, f"get_group got principal={principal!r}"
        self.get_group_calls.append((group_id, principal))
        return self._group

    async def send(self, group_id: str, data: str, *, principal: Any) -> Any:
        assert group_id == self._group_id, f"send got group_id={group_id!r}"
        assert data == self._data, f"send got data={data!r}"
        self.send_calls.append((group_id, data, principal))
        return self._result


def _fanout_ws(principal: Any, authz: Any) -> _WS:
    return _WS(
        state=SimpleNamespace(uterm_principal=principal), app=SimpleNamespace(state=SimpleNamespace(uterm_authz=authz))
    )


def _fanout_result() -> FanOutResult:
    return FanOutResult(
        group_id="group-out",
        send_id="send-out",
        command="cmd-out",
        sent_at=999.5,
        results=[SessionFanOutResult(worker_id="w1", ok=True, output_delta="delta", elapsed_ms=7, divergent=False)],
        divergent_sessions=["w1"],
        failed_sessions=["w2"],
        error="err-out",
        approval_required=True,
        approval_id="approval-out",
    )


def _expected_fanout_frame(result: FanOutResult) -> str:
    return encode_control_frame(
        {
            "type": "fanout_result",
            "group_id": result.group_id,
            "send_id": result.send_id,
            "command": result.command,
            "sent_at": result.sent_at,
            "results": [{"worker_id": "w1", "ok": True, "output_delta": "delta", "elapsed_ms": 7, "divergent": False}],
            "divergent_sessions": result.divergent_sessions,
            "failed_sessions": result.failed_sessions,
            "error": result.error,
            "approval_required": result.approval_required,
            "approval_id": result.approval_id,
        }
    )


async def test_fanout_send_reaches_the_controller_and_sends_the_exact_result_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_187 (``is_admin(None)``: this fake asserts the exact
    principal), 222 (``_fo_subj = None``), 224-227 (``get_group``'s
    arguments/signature), and 242-262 (every key of the ``fanout_result``
    frame, via an exact-string comparison built from the same field values in
    a different dict literal)."""
    principal = SimpleNamespace(subject_id="admin-1")
    authz = _Authz(admin_of=principal, ret=True)
    result_obj = _fanout_result()
    ctrl = _FanOutCtrl(group_id="group-in", subj="admin-1", data="data-in", group=object(), result=result_obj)
    hub = _Hub()
    hub.fan_out_controller = ctrl
    ws = _fanout_ws(principal, authz)
    msg_b = {"type": "fanout_send", "group_id": "group-in", "data": "data-in"}
    hbm_recorder = _patch_hbm(monkeypatch, (), None)
    dispatch_result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert dispatch_result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert ctrl.get_group_calls == [("group-in", "admin-1")]
    assert ctrl.send_calls == [("group-in", "data-in", principal)]
    assert ws.sent == [_expected_fanout_frame(result_obj)]
    assert hbm_recorder.calls == []


async def test_fanout_send_defaults_missing_group_id_and_data_to_empty_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_208/210/213 (``msg_b.get("group_id", ...)``'s default
    mangled/dropped) via ``get_group``'s strict argument, and 216/218/221
    (the same for ``"data"``) via ``send``'s -- ``group`` must be non-``None``
    here so ``send`` is actually reached and ``_fo_data`` gets observed;
    a ``None`` group returns before ``send`` and never exercises those ids."""
    principal = SimpleNamespace(subject_id="admin-2")
    authz = _Authz(admin_of=principal, ret=True)
    result_obj = _fanout_result()
    ctrl = _FanOutCtrl(group_id="", subj="admin-2", data="", group=object(), result=result_obj)
    hub = _Hub()
    hub.fan_out_controller = ctrl
    ws = _fanout_ws(principal, authz)
    msg_b = {"type": "fanout_send"}
    _patch_hbm(monkeypatch, (), None)
    dispatch_result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert dispatch_result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert ctrl.get_group_calls == [("", "admin-2")]
    assert ctrl.send_calls == [("", "", principal)]
    assert ws.sent == [_expected_fanout_frame(result_obj)]


async def test_fanout_send_rejects_the_anonymous_principal_without_touching_the_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_184/185 (the ``"anonymous"`` literal mangled): with
    ``subject_id`` genuinely ``"anonymous"``, the real ``!=`` check
    short-circuits the admin gate to ``False`` without calling ``is_admin``;
    a mangled literal would wrongly call it. Also kills mutmut_194 (the
    ``"global admin role required"`` literal) via the exact sent frame."""
    principal = SimpleNamespace(subject_id="anonymous")
    authz = _Authz(admin_of=principal, ret=True)
    ctrl = _FanOutCtrl(group_id="g", subj="admin-3", data="d", group=object(), result=object())
    hub = _Hub()
    hub.fan_out_controller = ctrl
    ws = _fanout_ws(principal, authz)
    msg_b = {"type": "fanout_send", "group_id": "g", "data": "d"}
    hbm_recorder = _patch_hbm(monkeypatch, (), None)
    dispatch_result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert dispatch_result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert authz.calls == []
    assert ctrl.get_group_calls == []
    assert ws.sent == [_error("global admin role required")]
    assert hbm_recorder.calls == []


async def test_fanout_send_defaults_principal_and_authz_to_none_on_a_bare_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_145/150 (the ``_fo_principal`` line's outer/inner
    ``getattr`` default dropped) and 160/165 (same, on the ``_fo_authz``
    line): with a socket genuinely lacking both ``.state`` and ``.app``,
    every real default quietly gives ``None`` down to a clean rejection;
    each mutant instead raises ``AttributeError`` partway through."""
    hub = _Hub()
    ws = _WS()  # no state, no app
    msg_b = {"type": "fanout_send", "group_id": "g", "data": "d"}
    hbm_recorder = _patch_hbm(monkeypatch, (), None)
    dispatch_result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert dispatch_result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert ws.sent == [_error("global admin role required")]
    assert hbm_recorder.calls == []


async def test_fanout_send_defaults_authz_to_none_when_app_state_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_175 (``getattr(_fo_authz, "uterm_authz", )`` default
    dropped): with ``websocket.app.state`` genuinely present but lacking
    ``uterm_authz``, the real default quietly gives ``None``; the mutant
    raises ``AttributeError`` before the admin gate is even reached."""
    hub = _Hub()
    ws = _WS(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace()))
    msg_b = {"type": "fanout_send", "group_id": "g", "data": "d"}
    hbm_recorder = _patch_hbm(monkeypatch, (), None)
    dispatch_result = await _dispatch(hub, ws, SimpleNamespace(control=msg_b))
    assert dispatch_result == (ROLE_IN, CAN_HIJACK_IN, OWNED_IN)
    assert ws.sent == [_error("global admin role required")]
    assert hbm_recorder.calls == []
