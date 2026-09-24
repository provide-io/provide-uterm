#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``rest._hijack_step``.

Kill-suite only -- behavioural coverage of the REST step endpoint lives in
``test_routes_coverage.py`` and friends (e.g. ``TestRestStepRateLimited``).
Those suites exercise ``_hijack_step`` through a real FastAPI ``TestClient``
against a real ``TermHub``, and either patch a single collaborator with
``patch.object(hub, "allow_rest_send_for", return_value=False)`` (which
answers the same regardless of what argument it is called with) or drive the
hub through its real lease machinery and only assert the response's
``status_code`` and the ``error`` key's presence -- never the exact client id
threaded through the rate-limit path, the exact control frame sent to the
worker, the ``ownership``-free lease/step semantics, the metric names, the
``append_event`` payload, or the full JSON body (including the wrong-case or
mangled-literal variants of "No worker connected for this session." /
"Invalid or expired hijack session."). That leaves 92 surviving mutants of
``_hijack_step`` (enumerated in the diffs handed to this suite), covering:

* every branch of ``_client_id = (http_request.client.host if
  http_request.client else None) or "unknown"`` (the ternary's condition
  forced true/false, ``or`` flipped to ``and``, the whole assignment
  replaced with ``None``, and the ``"unknown"`` literal mangled/cased);
* the rate-limited (429) branch: the ``allow_rest_send_for`` argument, the
  ``"rest_step_rate_limited_total"`` metric name, every argument of the
  ``logger.warning`` call, and every key/value/argument of the
  ``emit_telemetry("rate_limit.triggered", worker_id=..., metadata={...})``
  call;
* the "no session" (404) branch's exact error body;
* the ``error == "invalid_owner"`` branch: the comparison literal and its
  404 error body;
* the ``if not ok`` (409) branch's exact error body;
* the success path: every key/value of the ``{"type": "control", "action":
  "step", "owner": hs.owner, "lease_s": 0, "ts": time.time()}`` frame sent to
  ``send_owned_worker``, the ``logger.info`` call's arguments, every argument
  of ``append_event(worker_id, "hijack_step", {"hijack_id": hijack_id})``,
  the ``"hijack_steps_total"`` metric name, every argument of
  ``get_fresh_hijack_expiry``, and every key/value of the returned
  ``{"ok": True, "worker_id": ..., "hijack_id": ..., "lease_expires_at":
  ...}`` dict.

Everything here drives ``_hijack_step`` directly against a strict fake hub
that records the exact arguments of every call it receives (asserted by
value equality against the expected call, which catches a dropped argument,
a ``None`` substitution, or a swapped value just as surely as an inline
assert), a minimal fake ``Request`` exposing only ``.client.host``, and the
module's ``time`` pinned so both the step frame's ``ts`` and
``_mono_to_wall``'s conversion are exact literals rather than wall-clock
noise.

Documented equivalents: none. Every one of the 92 survivors changes either
the exact client id used for rate limiting/telemetry, the wire payload sent
to the worker, the branch taken on session/ownership/send failure, the JSON
body reported to the REST caller, or an argument recorded via a hub/logger
call -- all directly observable through the fakes below.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import JSONResponse

from provide.uterm.server.bridge.routes import rest
from provide.uterm.server.bridge.routes.rest import _hijack_step

WORKER_ID = "w-step"
HIJACK_ID = "hijack-step-1"
NOW = 1000.0
NO_WORKER = "No worker connected for this session."
INVALID_SESSION = "Invalid or expired hijack session."


class _FakeClient:
    """Stand-in for Starlette's ``Request.client`` (only ``.host`` is read)."""

    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Stand-in for ``Request`` exposing only the ``.client`` attribute that
    ``_hijack_step`` reads to compute ``_client_id``."""

    def __init__(self, client: _FakeClient | None) -> None:
        self.client = client


class _Hub:
    """Strict fake mirroring the ``TermHub`` surface ``_hijack_step`` calls.

    Every call is recorded with its exact arguments (including keyword
    arguments the source never actually varies, such as ``browser_ws`` on
    ``send_owned_worker``) so a test can assert the full call list by value
    equality -- a dropped argument, a ``None`` substitution, or a swapped
    literal changes the recorded tuple/dict and fails the assertion.
    """

    def __init__(
        self,
        *,
        allowed: bool = True,
        session: Any | None = None,
        send_result: tuple[bool, str | None] = (True, None),
        fresh_expiry: float = NOW,
    ) -> None:
        self._allowed = allowed
        self._session = session
        self._send_result = send_result
        self._fresh_expiry = fresh_expiry
        self.allow_calls: list[str] = []
        self.metrics: list[str] = []
        self.telemetry_calls: list[dict[str, Any]] = []
        self.get_rest_session_calls: list[tuple[str, str]] = []
        self.send_owned_worker_calls: list[dict[str, Any]] = []
        self.append_event_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.get_fresh_hijack_expiry_calls: list[tuple[str, str, float]] = []

    def allow_rest_send_for(self, client_id: str) -> bool:
        self.allow_calls.append(client_id)
        return self._allowed

    def metric(self, name: str, value: int = 1) -> None:
        assert value == 1, f"metric got value={value!r}"
        self.metrics.append(name)

    async def emit_telemetry(
        self,
        event_type: str,
        *,
        worker_id: str,
        principal: str | None = None,
        role: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert principal is None, f"emit_telemetry got principal={principal!r}"
        assert role is None, f"emit_telemetry got role={role!r}"
        self.telemetry_calls.append({"event_type": event_type, "worker_id": worker_id, "metadata": metadata})

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> Any:
        self.get_rest_session_calls.append((worker_id, hijack_id))
        return self._session

    async def send_owned_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        browser_ws: Any = None,
        rest_hijack_id: str | None = None,
        ownership_generation: int | None = None,
        source: Any = None,
    ) -> tuple[bool, str | None]:
        assert browser_ws is None, f"send_owned_worker got browser_ws={browser_ws!r}"
        assert ownership_generation is None, f"send_owned_worker got ownership_generation={ownership_generation!r}"
        assert source is None, f"send_owned_worker got source={source!r}"
        self.send_owned_worker_calls.append({"worker_id": worker_id, "msg": msg, "rest_hijack_id": rest_hijack_id})
        return self._send_result

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.append_event_calls.append((worker_id, event_type, data))
        return {}

    async def get_fresh_hijack_expiry(self, worker_id: str, hijack_id: str, fallback: float) -> float:
        self.get_fresh_hijack_expiry_calls.append((worker_id, hijack_id, fallback))
        return self._fresh_expiry


class _LoggerRecorder:
    """Strict stand-in for ``rest.logger``: records the exact positional call
    made to ``warning``/``info`` (format string plus every substitution)."""

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


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # time.time() == time.monotonic() == NOW makes `_mono_to_wall(x) == x`
    # exactly, so the returned `lease_expires_at` and the step frame's `ts`
    # are exact literals instead of wall-clock noise.
    monkeypatch.setattr(rest, "time", SimpleNamespace(time=lambda: NOW, monotonic=lambda: NOW))


async def test_rate_limited_with_no_client_uses_unknown_client_id(
    logger_recorder: _LoggerRecorder,
) -> None:
    """``http_request.client`` is ``None`` -> ``_client_id`` must be exactly
    ``"unknown"`` (kills mutmut_1/2/4/5/6: the assignment forced to ``None``,
    ``or`` flipped to ``and``, the ternary condition forced true -- which
    would crash dereferencing ``None.host`` -- and the ``"unknown"`` literal
    mangled/cased). That id then has to survive unchanged through
    ``allow_rest_send_for`` (kills mutmut_8: argument forced ``None``), the
    ``"rest_step_rate_limited_total"`` metric (kills mutmut_9/10/11: name
    forced ``None``/mangled/cased), the exact ``logger.warning`` call (kills
    mutmut_13/14/15/20: each ``%s`` substitution forced ``None`` and the
    format string mangled), and the ``emit_telemetry`` call's event type,
    worker_id and metadata (kills mutmut_22/23/24/27/28/29/30/31/32/33/34/35:
    the event type forced ``None``/mangled/cased, ``worker_id`` forced
    ``None``, ``metadata`` forced ``None`` or dropped, and every key/value of
    the metadata dict mangled/cased). The response itself must be the exact
    429 rate-limited body, and no other hub method may be touched."""
    http_request = _FakeRequest(client=None)
    hub = _Hub(allowed=False)

    resp = await _hijack_step(hub, http_request, WORKER_ID, HIJACK_ID)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 429
    assert json.loads(resp.body) == {"error": "rate_limited"}

    assert hub.allow_calls == ["unknown"]
    assert hub.metrics == ["rest_step_rate_limited_total"]
    assert logger_recorder.warning_calls == [
        ("rest_step_rate_limited worker_id=%s hijack_id=%s client=%s", WORKER_ID, HIJACK_ID, "unknown")
    ]
    assert hub.telemetry_calls == [
        {
            "event_type": "rate_limit.triggered",
            "worker_id": WORKER_ID,
            "metadata": {"client_id": "unknown", "limit_type": "rest_step"},
        }
    ]
    assert logger_recorder.info_calls == []
    assert hub.get_rest_session_calls == []
    assert hub.send_owned_worker_calls == []
    assert hub.append_event_calls == []
    assert hub.get_fresh_hijack_expiry_calls == []


async def test_no_session_returns_exact_404_body() -> None:
    """``get_rest_session`` returns ``None`` -> kills mutmut_51 (the body
    replaced with ``None``) and mutmut_55/56/57/58/59 (the ``"error"`` key
    and the "Invalid or expired hijack session." value mangled/cased).
    Nothing past ``get_rest_session`` may run."""
    http_request = _FakeRequest(client=_FakeClient(host="203.0.113.5"))
    hub = _Hub(allowed=True, session=None)

    resp = await _hijack_step(hub, http_request, WORKER_ID, HIJACK_ID)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 404
    assert json.loads(resp.body) == {"error": INVALID_SESSION}

    assert hub.get_rest_session_calls == [(WORKER_ID, HIJACK_ID)]
    assert hub.send_owned_worker_calls == []
    assert hub.append_event_calls == []
    assert hub.metrics == []


async def test_invalid_owner_returns_exact_404_body(logger_recorder: _LoggerRecorder) -> None:
    """``send_owned_worker`` reports ``error == "invalid_owner"`` -> kills
    mutmut_84/85 (the comparison literal mangled/cased, which would instead
    fall through to the ``if not ok`` 409 branch since ``ok`` is ``False``
    here) and mutmut_86/87/88/89/90/91/92/93/94/95 (the returned body
    replaced with ``None``, its status forced ``None``/405, the dict
    dropped entirely, or the ``"error"``/message literal mangled/cased).
    The success path (metric, append_event, logger.info) must not run."""
    http_request = _FakeRequest(client=_FakeClient(host="203.0.113.5"))
    hs = SimpleNamespace(owner="bob", lease_expires_at=42.0)
    hub = _Hub(allowed=True, session=hs, send_result=(False, "invalid_owner"))

    resp = await _hijack_step(hub, http_request, WORKER_ID, HIJACK_ID)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 404
    assert json.loads(resp.body) == {"error": INVALID_SESSION}

    assert hub.send_owned_worker_calls == [
        {
            "worker_id": WORKER_ID,
            "msg": {"type": "control", "action": "step", "owner": "bob", "lease_s": 0, "ts": NOW},
            "rest_hijack_id": HIJACK_ID,
        }
    ]
    assert hub.append_event_calls == []
    assert hub.metrics == []
    assert logger_recorder.info_calls == []


async def test_send_failure_returns_exact_409_body(logger_recorder: _LoggerRecorder) -> None:
    """``send_owned_worker`` reports ``ok=False`` with an error other than
    ``"invalid_owner"`` -> kills mutmut_97/98/99/100 (the 409 body replaced
    with ``None``, its status forced ``None``, the dict dropped, or the
    ``status_code`` kwarg dropped entirely -- which defaults FastAPI's
    ``JSONResponse`` to 200) and mutmut_101/102/103/104/105/106 (the
    ``"error"``/message literal mangled/cased, and 409 forced to 410). The
    success path must not run."""
    http_request = _FakeRequest(client=_FakeClient(host="203.0.113.5"))
    hs = SimpleNamespace(owner="carol", lease_expires_at=42.0)
    hub = _Hub(allowed=True, session=hs, send_result=(False, "no_worker"))

    resp = await _hijack_step(hub, http_request, WORKER_ID, HIJACK_ID)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    assert json.loads(resp.body) == {"error": NO_WORKER}

    assert hub.append_event_calls == []
    assert hub.metrics == []
    assert logger_recorder.info_calls == []


async def test_success_sends_exact_frame_and_returns_exact_dict(
    logger_recorder: _LoggerRecorder,
) -> None:
    """The full success path, with a real (non-``"unknown"``) client host so
    a wrong ``_client_id`` computation is observable in the ``logger.info``
    call -- kills mutmut_3 (the ternary condition forced false, which would
    substitute ``"unknown"`` for the real host here). Kills
    mutmut_68/69/70/71/76/77/78/79/80/81/82 (every key and value of the
    ``{"type": "control", "action": "step", "owner": ..., "lease_s": 0,
    "ts": ...}`` frame mangled/cased, including ``lease_s`` changed from
    ``0`` to ``1``). Kills mutmut_108/109/110/115 (the ``logger.info`` call's
    ``worker_id``/``hijack_id``/``client`` arguments forced ``None`` and its
    format string mangled). Kills mutmut_117/118/119/122/123/124/125/126
    (every argument of ``append_event(worker_id, "hijack_step",
    {"hijack_id": hijack_id})`` forced ``None``/dropped/mangled/cased,
    including the omitted third argument, which would default to ``None``
    instead of the exact ``{"hijack_id": ...}`` dict). Kills
    mutmut_127/128/129 (the ``"hijack_steps_total"`` metric name forced
    ``None``/mangled/cased). Kills mutmut_131/132/133 (each argument of
    ``get_fresh_hijack_expiry`` forced ``None``). Kills
    mutmut_137/138/139/140/141/142/143/144/145 (every key/value of the
    returned dict mangled/cased/flipped)."""
    client_host = "203.0.113.5"
    http_request = _FakeRequest(client=_FakeClient(host=client_host))
    hs = SimpleNamespace(owner="alice", lease_expires_at=555.0)
    fresh_expiry = 777.0
    hub = _Hub(allowed=True, session=hs, send_result=(True, None), fresh_expiry=fresh_expiry)

    result = await _hijack_step(hub, http_request, WORKER_ID, HIJACK_ID)

    assert result == {
        "ok": True,
        "worker_id": WORKER_ID,
        "hijack_id": HIJACK_ID,
        "lease_expires_at": fresh_expiry,
    }

    assert hub.send_owned_worker_calls == [
        {
            "worker_id": WORKER_ID,
            "msg": {"type": "control", "action": "step", "owner": "alice", "lease_s": 0, "ts": NOW},
            "rest_hijack_id": HIJACK_ID,
        }
    ]
    assert logger_recorder.info_calls == [
        ("rest_step_ok worker_id=%s hijack_id=%s client=%s", WORKER_ID, HIJACK_ID, client_host)
    ]
    assert logger_recorder.warning_calls == []
    assert hub.telemetry_calls == []
    assert hub.append_event_calls == [(WORKER_ID, "hijack_step", {"hijack_id": HIJACK_ID})]
    assert hub.metrics == ["hijack_steps_total"]
    assert hub.get_fresh_hijack_expiry_calls == [(WORKER_ID, HIJACK_ID, 555.0)]
