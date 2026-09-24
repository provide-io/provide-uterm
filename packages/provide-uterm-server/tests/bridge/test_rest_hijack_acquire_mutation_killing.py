#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``rest._hijack_acquire``.

Kill-suite only -- behavioural coverage of the REST acquire endpoint lives in
``test_acquire_atomic.py``, ``test_rest_lease_ownership.py`` and
``test_routes*.py``. Those suites drive the endpoint through a real ``TermHub``
via FastAPI's ``TestClient``: they prove a lease is or isn't granted, and that
ownership rules hold, but the hub underneath answers correctly for *any*
arguments it is given, so they cannot see a metric renamed, a log line's
format string mangled, a resume/pause frame's field dropped or its value
swapped, an event payload key mangled, or the wrong hub method argument
substituted with ``None``. ``_hijack_acquire`` was just extracted out of a
``@router.post``-decorated closure (mutmut skips decorated functions
entirely -- see ``docs/mutmut-survivors-triage.md`` Wave 9); on the resulting
module-level function mutmut found 127 survivors, every one of them on
exactly the things described above.

Everything here drives ``_hijack_acquire`` directly against a strict fake hub
that mirrors ``TermHub``'s real method signatures (see
``packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/``:
``connection.py.allow_rest_acquire_for``, ``store.py.metric``/
``notify_hijack_changed``/``clamp_lease``, ``core_impl.py.emit_telemetry``/
``send_worker_if_unowned``/``append_event``/``broadcast_hijack_state``/
``release_rest_hijack``, and ``core_delegates_lease.py.cleanup_expired_hijack``/
``try_acquire_rest_hijack``/``get_rest_session``). Several of the fake's
methods deliberately drop the real signature's defaults (``notify_hijack_changed``,
``append_event``, ``emit_telemetry``'s ``metadata``) because every call site in
``_hijack_acquire`` always supplies those arguments explicitly -- making them
mandatory on the fake turns a mutant that drops the argument into an
immediate ``TypeError`` instead of a silently-accepted ``None``. No method
uses ``AsyncMock(return_value=...)``: a mock configured that way would still
return a value after an argument was silently dropped or replaced, so it
cannot distinguish the real call from a mutant's. A recorder swapped in for
``rest.logger`` pins the exact format string and every substitution argument
of each ``logger.warning``/``logger.info`` call. ``rest.time`` and ``rest.uuid``
are monkeypatched to fixed values so every timestamp and the generated
``hijack_id`` are exact, letting response bodies and event/frame payloads be
asserted with a single ``==``.

Documented equivalents
-----------------------
- **mutmut_53** (``session_committed = False`` -> ``session_committed = None``):
  ``session_committed`` is read exactly once, at ``if not session_committed:``
  in the ``finally`` block, and is only ever *written* again as the literal
  ``True`` (never reset to ``False``). ``bool(None)`` and ``bool(False)`` are
  both falsy and the value is never compared by identity or type, so no
  input can make ``not None`` and ``not False`` disagree at that check --
  the mutant is behaviourally identical in every reachable state.
- **mutmut_146** (``error_msgs.get(err or "", str(err))`` -> ``error_msgs.get(err
  or "XXXX", str(err))``): the mutated fallback is only substituted for the
  dict-``get`` *key* when ``err`` is falsy, and neither ``""`` nor ``"XXXX"``
  is a key in ``error_msgs`` (whose keys are exactly ``"no_worker"``,
  ``"already_hijacked"``, ``"open_mode"``). Both fallbacks therefore always
  miss the dict and fall through to the same ``default=str(err)``, for every
  possible value of ``err`` -- there is no input that makes the two keys
  resolve differently.
"""

from __future__ import annotations

import asyncio
import json
import uuid as uuid_mod
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import JSONResponse

from provide.uterm.server.bridge.models import HijackAcquireRequest
from provide.uterm.server.bridge.routes import rest
from provide.uterm.server.bridge.routes.rest import _hijack_acquire

WID = "worker-1"
CLIENT_HOST = "203.0.113.5"
WALL_NOW = 1_700_000_000.0
MONO_NOW = 555.5
FIXED_HIJACK_ID = "11111111-1111-1111-1111-111111111111"
REQUEST = HijackAcquireRequest(owner="dashboard", lease_s=42)

RESUME_PAYLOAD: dict[str, Any] = {
    "type": "control",
    "action": "resume",
    "owner": "dashboard",
    "lease_s": 0,
    "hijack_id": FIXED_HIJACK_ID,
    "ts": WALL_NOW,
}
EXPECTED_TRY_ACQUIRE_CALL: dict[str, Any] = {
    "worker_id": WID,
    "owner": "dashboard",
    "lease_s": 42,
    "hijack_id": FIXED_HIJACK_ID,
    "now": MONO_NOW,
}


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest, "time", SimpleNamespace(time=lambda: WALL_NOW, monotonic=lambda: MONO_NOW))


@pytest.fixture(autouse=True)
def _pinned_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest, "uuid", SimpleNamespace(uuid4=lambda: uuid_mod.UUID(FIXED_HIJACK_ID)))


# ---------------------------------------------------------------------------
# logger recorder
# ---------------------------------------------------------------------------


class _LoggerRecorder:
    """Strict stand-in for ``rest.logger``: records exact positional calls."""

    def __init__(self) -> None:
        self.warning_calls: list[tuple[Any, ...]] = []
        self.info_calls: list[tuple[Any, ...]] = []

    def warning(self, *args: Any) -> None:
        self.warning_calls.append(args)

    def info(self, *args: Any) -> None:
        self.info_calls.append(args)


@pytest.fixture
def logger_recorder(monkeypatch: pytest.MonkeyPatch) -> _LoggerRecorder:
    recorder = _LoggerRecorder()
    monkeypatch.setattr(rest, "logger", recorder)
    return recorder


# ---------------------------------------------------------------------------
# fake Request
# ---------------------------------------------------------------------------


class _Client:
    def __init__(self, host: str) -> None:
        self.host = host


class _Principal:
    def __init__(self, subject_id: str) -> None:
        self.subject_id = subject_id


class _State:
    def __init__(self, principal: _Principal | None) -> None:
        self.uterm_principal = principal


class _FakeRequest:
    """Minimal stand-in for ``fastapi.Request`` exposing only what
    ``_hijack_acquire``/``_principal_subject`` read: ``.client.host`` and
    ``.state.uterm_principal.subject_id``."""

    def __init__(self, *, client_host: str | None = CLIENT_HOST, principal: _Principal | None = None) -> None:
        self.client = _Client(client_host) if client_host is not None else None
        self.state = _State(principal)


# ---------------------------------------------------------------------------
# fake hub
# ---------------------------------------------------------------------------


class _Hub:
    """Strict fake mirroring the exact ``TermHub`` methods ``_hijack_acquire``
    calls. Every method records the arguments it received; several drop the
    real signature's defaults so a dropped argument raises ``TypeError``
    rather than silently falling back to a value that might coincide with
    the real one."""

    def __init__(
        self,
        *,
        rate_limited: bool = False,
        acquire_result: tuple[bool, str | None] = (True, None),
        acquire_raises: BaseException | None = None,
        get_rest_session_result: Any = None,
        release_result: tuple[bool, bool] = (True, False),
        release_raises: BaseException | None = None,
    ) -> None:
        self._rate_limited = rate_limited
        self._acquire_result = acquire_result
        self._acquire_raises = acquire_raises
        self._get_rest_session_result = get_rest_session_result
        self._release_result = release_result
        self._release_raises = release_raises

        self.allow_calls: list[str] = []
        self.metrics: list[str] = []
        self.emit_telemetry_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.cleanup_calls: list[str] = []
        self.try_acquire_calls: list[dict[str, Any]] = []
        self.send_unowned_calls: list[tuple[str, dict[str, Any]]] = []
        self.notify_calls: list[tuple[str, bool, str | None]] = []
        self.append_event_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.broadcast_calls: list[str] = []
        self.get_rest_session_calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, str]] = []

    def allow_rest_acquire_for(self, client_id: str) -> bool:
        assert client_id in (CLIENT_HOST, "unknown"), f"allow_rest_acquire_for got {client_id!r}"
        self.allow_calls.append(client_id)
        return not self._rate_limited

    def metric(self, name: str, value: int = 1) -> None:
        self.metrics.append(name)

    async def emit_telemetry(
        self,
        event_type: str,
        *,
        worker_id: str,
        metadata: dict[str, Any],
        principal: str | None = None,
        role: str | None = None,
    ) -> None:
        self.emit_telemetry_calls.append((event_type, worker_id, dict(metadata)))

    async def cleanup_expired_hijack(self, worker_id: str) -> bool:
        assert worker_id == WID, f"cleanup_expired_hijack got {worker_id!r}"
        self.cleanup_calls.append(worker_id)
        return False

    def clamp_lease(self, lease_s: int) -> int:
        assert lease_s == REQUEST.lease_s, f"clamp_lease got {lease_s!r}"
        return lease_s

    async def try_acquire_rest_hijack(
        self,
        worker_id: str,
        *,
        owner: str,
        lease_s: int,
        hijack_id: str,
        now: float,
    ) -> tuple[bool, str | None]:
        assert worker_id == WID, f"try_acquire_rest_hijack got worker_id={worker_id!r}"
        self.try_acquire_calls.append(
            {"worker_id": worker_id, "owner": owner, "lease_s": lease_s, "hijack_id": hijack_id, "now": now}
        )
        if self._acquire_raises is not None:
            raise self._acquire_raises
        return self._acquire_result

    async def send_worker_if_unowned(self, worker_id: str, msg: dict[str, Any]) -> bool:
        assert worker_id == WID, f"send_worker_if_unowned got worker_id={worker_id!r}"
        self.send_unowned_calls.append((worker_id, dict(msg)))
        return True

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None) -> None:
        self.notify_calls.append((worker_id, enabled, owner))

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        self.append_event_calls.append((worker_id, event_type, dict(data)))
        return {}

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcast_calls.append(worker_id)

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> Any:
        self.get_rest_session_calls.append((worker_id, hijack_id))
        return self._get_rest_session_result

    async def release_rest_hijack(self, worker_id: str, hijack_id: str) -> tuple[bool, bool]:
        self.release_calls.append((worker_id, hijack_id))
        if self._release_raises is not None:
            raise self._release_raises
        return self._release_result


async def _call(hub: _Hub, http_request: _FakeRequest, worker_id: str, request: HijackAcquireRequest | None) -> Any:
    return await asyncio.wait_for(_hijack_acquire(hub, http_request, worker_id, request), timeout=2)


# ---------------------------------------------------------------------------
# rate limiting + _client_id
# ---------------------------------------------------------------------------


async def test_rate_limited_short_circuits_with_the_connecting_hosts_ip(logger_recorder: _LoggerRecorder) -> None:
    """Kills mutmut_1/2/3 (``_client_id`` collapsing to ``None``/using ``and``
    instead of ``or``/always taking the ``else`` branch -- each would send a
    different client id than the real host). Kills mutmut_8-11 (the rate
    limit check's argument and the metric's name), mutmut_13/14/18 (the
    ``logger.warning`` call's exact format string and both arguments) and
    mutmut_20-22/25-33 (the ``rate_limit.triggered`` telemetry event: its
    type, the dropped/None-substituted ``worker_id``/``metadata`` kwargs, and
    every key and value inside ``metadata``)."""
    hub = _Hub(rate_limited=True)
    http_request = _FakeRequest(client_host=CLIENT_HOST)
    result = await _call(hub, http_request, WID, None)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 429
    assert json.loads(result.body) == {"error": "rate_limited"}
    assert hub.allow_calls == [CLIENT_HOST]
    assert hub.metrics == ["rest_acquire_rate_limited_total"]
    assert logger_recorder.warning_calls == [("rest_acquire_rate_limited client=%s worker_id=%s", CLIENT_HOST, WID)]
    assert hub.emit_telemetry_calls == [
        ("rate_limit.triggered", WID, {"client_id": CLIENT_HOST, "limit_type": "rest_acquire"})
    ]


async def test_client_id_falls_back_to_unknown_when_there_is_no_client(logger_recorder: _LoggerRecorder) -> None:
    """Kills mutmut_4 (the ternary's condition forced to always-true, which
    would dereference ``http_request.client.host`` on a ``None`` client
    instead of taking the ``else`` branch) and mutmut_5/6 (the ``"unknown"``
    fallback string mangled -- only observable when there is no client)."""
    hub = _Hub(rate_limited=True)
    http_request = _FakeRequest(client_host=None)
    result = await _call(hub, http_request, WID, None)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 429
    assert hub.allow_calls == ["unknown"]
    assert logger_recorder.warning_calls == [("rest_acquire_rate_limited client=%s worker_id=%s", "unknown", WID)]
    assert hub.emit_telemetry_calls == [
        ("rate_limit.triggered", WID, {"client_id": "unknown", "limit_type": "rest_acquire"})
    ]


# ---------------------------------------------------------------------------
# failed acquire: already_hijacked / no_worker / open_mode / unmapped
# ---------------------------------------------------------------------------


async def test_conflict_already_hijacked_counts_a_metric_and_sends_no_resume(
    logger_recorder: _LoggerRecorder,
) -> None:
    """Kills mutmut_46 (``cleanup_expired_hijack`` called with ``None``),
    mutmut_49/50 (``hijack_id`` replaced by ``None``/``str(None)``) and
    mutmut_57 (``owner=None`` passed to ``try_acquire_rest_hijack``) via the
    exact recorded call. Kills mutmut_67-69 (the ``err == "already_hijacked"``
    dispatch flipped/mangled -- which would route this case into the
    "no_worker" log line and metric instead) and mutmut_96/97 (the *second*,
    independent ``err != "already_hijacked"`` check that guards the
    compensating resume -- a mangled string there would send a resume for
    the legitimate owner's own session). Kills mutmut_70-72 (the
    ``hijack_conflicts_total`` metric name/argument), mutmut_74-76/81 (the
    ``rest_acquire_conflict`` log line's exact format string and all three
    arguments) and mutmut_125-129/141/145 (the ``"already_hijacked"`` error
    message key/value and the ``error_msgs.get(...)`` key/default swap --
    ``err or ""`` vs ``err and ""`` resolve differently only when ``err`` is
    truthy and is itself a real key, which is exactly this case)."""
    hub = _Hub(acquire_result=(False, "already_hijacked"))
    http_request = _FakeRequest(client_host=CLIENT_HOST)
    result = await _call(hub, http_request, WID, REQUEST)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    assert json.loads(result.body) == {"error": "Worker is already hijacked."}
    assert hub.cleanup_calls == [WID]
    assert hub.try_acquire_calls == [EXPECTED_TRY_ACQUIRE_CALL]
    assert hub.metrics == ["hijack_conflicts_total"]
    assert logger_recorder.warning_calls == [
        ("rest_acquire_conflict worker_id=%s owner=%s client=%s", WID, "dashboard", CLIENT_HOST)
    ]
    assert hub.send_unowned_calls == []


async def test_no_worker_error_resumes_the_worker_and_reports_missing(logger_recorder: _LoggerRecorder) -> None:
    """Kills mutmut_84-86/91 (the ``rest_acquire_no_worker`` log line's exact
    format string and all three arguments) and mutmut_110-118 (the exact
    compensating resume payload sent because a "no_worker" error is not
    "already_hijacked": ``owner``, ``lease_s`` staying ``0``, ``hijack_id``
    and ``ts`` -- every key and every value). Kills mutmut_120-124 (the
    ``"no_worker"`` error message key/value)."""
    hub = _Hub(acquire_result=(False, "no_worker"))
    http_request = _FakeRequest(client_host=CLIENT_HOST)
    result = await _call(hub, http_request, WID, REQUEST)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    assert json.loads(result.body) == {"error": "No worker connected for this session."}
    assert logger_recorder.warning_calls == [
        ("rest_acquire_no_worker worker_id=%s owner=%s client=%s", WID, "dashboard", CLIENT_HOST)
    ]
    assert hub.send_unowned_calls == [(WID, RESUME_PAYLOAD)]
    assert hub.metrics == []


async def test_open_mode_error_resumes_and_reports_open_mode() -> None:
    """Kills mutmut_130-134 (the ``"open_mode"`` error message key/value)."""
    hub = _Hub(acquire_result=(False, "open_mode"))
    http_request = _FakeRequest(client_host=CLIENT_HOST)
    result = await _call(hub, http_request, WID, REQUEST)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    assert json.loads(result.body) == {"error": "Hijack not available in open input mode."}
    assert hub.send_unowned_calls == [(WID, RESUME_PAYLOAD)]


async def test_unmapped_error_falls_back_to_the_raw_error_string() -> None:
    """An error string that is none of the three known keys: kills
    mutmut_142-144/147 (the ``error_msgs.get(key, default)`` default value
    dropped/hardcoded to ``None``/``"None"`` -- only observable on a miss,
    since a hit ignores the default entirely)."""
    hub = _Hub(acquire_result=(False, "weird_error"))
    http_request = _FakeRequest(client_host=CLIENT_HOST)
    result = await _call(hub, http_request, WID, REQUEST)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    assert json.loads(result.body) == {"error": "weird_error"}


# ---------------------------------------------------------------------------
# successful acquire
# ---------------------------------------------------------------------------


async def test_successful_acquire_returns_the_exact_body_and_side_effects() -> None:
    """Kills mutmut_149-151 (the ``hijack_acquires_total`` metric name),
    mutmut_153-155/157/164 (the ``rest_acquire_ok`` log line's exact format
    string and its ``worker_id``/``hijack_id``/``owner``/``client``
    arguments), mutmut_166-168/171/172 (``notify_hijack_changed``'s
    ``worker_id``/``enabled``/dropped-``owner`` arguments), mutmut_173-186
    (the ``hijack_acquired`` event: its type, the dropped payload, and every
    key of ``hijack_id``/``owner``/``lease_s``), mutmut_187 (broadcasting to
    ``None`` instead of ``worker_id``), mutmut_188-190 (``get_rest_session``
    called with ``None``/omitted entirely), mutmut_194 (the acquiring
    principal hardcoded to ``None`` instead of read from the request) and
    mutmut_201/202/207 (the final response's ``worker_id`` key and
    ``lease_expires_at`` computed by subtracting instead of adding the
    lease)."""
    acquired_session = SimpleNamespace(acquired_by=None)
    hub = _Hub(acquire_result=(True, None), get_rest_session_result=acquired_session)
    http_request = _FakeRequest(client_host=CLIENT_HOST, principal=_Principal("alice-1"))
    result = await _call(hub, http_request, WID, REQUEST)
    assert result == {
        "ok": True,
        "worker_id": WID,
        "hijack_id": FIXED_HIJACK_ID,
        "lease_expires_at": WALL_NOW + 42,
        "owner": "dashboard",
    }
    assert hub.metrics == ["hijack_acquires_total"]
    assert hub.notify_calls == [(WID, True, "dashboard")]
    assert hub.append_event_calls == [
        (WID, "hijack_acquired", {"hijack_id": FIXED_HIJACK_ID, "owner": "dashboard", "lease_s": 42})
    ]
    assert hub.broadcast_calls == [WID]
    assert hub.get_rest_session_calls == [(WID, FIXED_HIJACK_ID)]
    assert acquired_session.acquired_by == "alice-1"
    assert hub.release_calls == []


async def test_successful_acquire_logs_the_exact_info_line(logger_recorder: _LoggerRecorder) -> None:
    """Isolates the ``rest_acquire_ok`` log line (its own test so the success
    body-and-side-effects assertions above stay readable): kills
    mutmut_153-155/157/164 again from a second, independent angle -- a
    differently-shaped ``request``/client than the body test."""
    hub = _Hub(acquire_result=(True, None), get_rest_session_result=SimpleNamespace(acquired_by=None))
    http_request = _FakeRequest(client_host=CLIENT_HOST)
    await _call(hub, http_request, WID, REQUEST)
    assert logger_recorder.info_calls == [
        (
            "rest_acquire_ok worker_id=%s hijack_id=%s owner=%s lease_s=%d client=%s",
            WID,
            FIXED_HIJACK_ID,
            "dashboard",
            42,
            CLIENT_HOST,
        )
    ]
    assert logger_recorder.warning_calls == []


# ---------------------------------------------------------------------------
# finally: compensating resume on an exception before commit
# ---------------------------------------------------------------------------


async def test_disconnect_during_acquire_sends_the_compensating_resume() -> None:
    """A ``CancelledError`` raised out of ``try_acquire_rest_hijack`` before
    ``session_committed`` is ever set to ``True`` (simulating a client
    disconnect while the worker is paused): kills mutmut_228-236 (the
    ``finally`` block's own resume payload -- ``owner``, ``lease_s`` staying
    ``0`` and ``ts``, a separate dict literal from the one in the failure
    branch and mutated with distinct mutmut ids), mutmut_237-242
    (``notify_hijack_changed``'s ``worker_id``/dropped ``enabled``/dropped
    ``worker_id``/dropped ``enabled``/dropped ``owner``/``enabled`` flipped to
    ``True``) and mutmut_243 (broadcasting to ``None``)."""
    hub = _Hub(acquire_raises=asyncio.CancelledError(), release_result=(True, False))
    http_request = _FakeRequest(client_host=CLIENT_HOST)
    with pytest.raises(asyncio.CancelledError):
        await _call(hub, http_request, WID, REQUEST)
    assert hub.release_calls == [(WID, FIXED_HIJACK_ID)]
    assert hub.send_unowned_calls == [(WID, RESUME_PAYLOAD)]
    assert hub.notify_calls == [(WID, False, None)]
    assert hub.broadcast_calls == [WID]


async def test_compensating_resume_failure_is_logged_and_original_error_reraised(
    logger_recorder: _LoggerRecorder,
) -> None:
    """The ``finally`` block's own cleanup fails (``release_rest_hijack``
    raises an ``OSError``) while an unrelated error is already propagating
    from ``try_acquire_rest_hijack``: kills mutmut_245/246 (the
    ``hijack_acquire_compensating_resume_failed`` log call's ``worker_id``/
    ``exc`` arguments dropped to ``None``) and mutmut_250 (its format string
    mangled). Also confirms the *original* exception -- not the cleanup
    failure -- is what ultimately propagates, per the docstring's stated
    contract."""
    release_exc = OSError("release boom")
    hub = _Hub(acquire_raises=RuntimeError("outer boom"), release_raises=release_exc)
    http_request = _FakeRequest(client_host=CLIENT_HOST)
    with pytest.raises(RuntimeError, match="outer boom"):
        await _call(hub, http_request, WID, REQUEST)
    assert logger_recorder.warning_calls == [
        ("hijack_acquire_compensating_resume_failed worker_id=%s: %s", WID, release_exc)
    ]
