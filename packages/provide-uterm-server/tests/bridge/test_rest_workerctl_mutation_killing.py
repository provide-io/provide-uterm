#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``rest_workerctl._set_input_mode`` and
``rest_workerctl._disconnect_worker``.

Kill-suite only -- both functions were, until this refactor, closures nested
directly inside ``@router.post``-decorated endpoints. mutmut skips decorated
functions outright (see docs/mutmut-survivors-triage.md Wave 9 and the
``routes/`` regression it describes), so the file had exactly zero mutants
and no mutant list has ever been generated for it. They have now been pulled
out to module-level ``_set_input_mode``/``_disconnect_worker`` functions so
mutmut can reach them; this suite is written ahead of that first measurement
run and pins every mutation mutmut's standard operator set is known to
generate against this exact source: literal string mangling ("XX..XX" and
case flips) and number changes (404 -> 405, etc.) in the returned bodies and
status codes, ``None`` substitution for each argument passed to the hub
methods and to the logger calls, dropped arguments (which shift arity
against the collaborators' real signatures), ``==``/``!=`` flips on the
``err == "not_found"`` comparison, and ``not`` removal on the ``if not ok:``
guards.

Every hub collaborator below is a strict fake that asserts on the exact
positional arguments it receives, mirroring the real signatures:
``TermHub.set_input_mode(self, worker_id: str, mode: InputMode) -> tuple[bool, str | None]``
and ``TermHub.disconnect_worker(self, worker_id: str) -> bool``
(see ``packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/__init__.py``
lines 205-206, and the concrete implementations in ``core_impl.py``). None of
these fakes use ``AsyncMock(return_value=...)``: a mock configured that way
would still return a value after an argument was silently dropped or
replaced with ``None``, so it cannot distinguish the real call from a
mutant's call. A plain assert on each received argument can.

The ``logger`` recorder pins the exact format string and every positional
argument passed to ``logger.warning``/``logger.info``, so a mutant that
mangles the log message string, swaps one ``%s`` substitution for another,
or drops an argument is caught even though the log line has no effect on
the function's return value.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from provide.uterm.server.bridge.routes import rest_workerctl
from provide.uterm.server.bridge.routes.rest_workerctl import (
    _disconnect_worker,
    _set_input_mode,
    register_workerctl_routes,
)

WID = "w1"


# ---------------------------------------------------------------------------
# logger recorder
# ---------------------------------------------------------------------------


class _LoggerRecorder:
    """Strict stand-in for ``rest_workerctl.logger``.

    Records the exact positional call made to ``warning``/``info`` so a test
    can assert the full tuple -- format string plus every substitution
    argument -- rather than merely "was called".
    """

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
    monkeypatch.setattr(rest_workerctl, "logger", recorder)
    return recorder


# ---------------------------------------------------------------------------
# _set_input_mode
# ---------------------------------------------------------------------------


class _InputModeHub:
    """Strict fake mirroring ``TermHub.set_input_mode(worker_id, mode)``."""

    def __init__(self, *, worker_id: str, mode: str, result: tuple[bool, str | None]) -> None:
        self._worker_id = worker_id
        self._mode = mode
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def set_input_mode(self, worker_id: str, mode: str) -> tuple[bool, str | None]:
        assert worker_id == self._worker_id, f"set_input_mode got worker_id={worker_id!r}"
        assert mode == self._mode, f"set_input_mode got mode={mode!r}"
        self.calls.append((worker_id, mode))
        return self._result


async def test_set_input_mode_success_returns_exact_dict(logger_recorder: _LoggerRecorder) -> None:
    """Success path: kills argument-order/None-substitution mutants on the
    ``hub.set_input_mode(worker_id, input_mode)`` call (the fake asserts both
    positional args exactly), kills ``not`` removal on ``if not ok:`` (a
    mutant that inverts the guard would route this ``ok=True`` case into the
    error branch instead of returning the success dict), and pins every key
    and value of the returned dict literally -- including which value goes
    to which key -- so a mutant that swaps ``input_mode``/``worker_id`` field
    values, or mangles the ``"ok"``/``"input_mode"``/``"worker_id"`` string
    literals, fails the equality. Also pins the exact ``logger.info`` call,
    including its format string and both substitution arguments, so a
    mutant that mangles ``"rest_input_mode_ok worker_id=%s mode=%s"`` or
    drops/reorders an argument is caught even though it has no effect on the
    return value."""
    hub = _InputModeHub(worker_id=WID, mode="open", result=(True, None))
    result = await _set_input_mode(hub, WID, "open")
    assert result == {"ok": True, "input_mode": "open", "worker_id": WID}
    assert hub.calls == [(WID, "open")]
    assert logger_recorder.info_calls == [("rest_input_mode_ok worker_id=%s mode=%s", WID, "open")]
    assert logger_recorder.warning_calls == []


async def test_set_input_mode_not_found_returns_404(logger_recorder: _LoggerRecorder) -> None:
    """``err == "not_found"`` path: kills the ``404 -> <other int>`` number
    mutant, kills a flip of ``==`` to ``!=`` on ``err == "not_found"`` (which
    would instead select the 409 branch for this exact error string), and
    kills string-literal mangling of ``"not_found"`` and of the returned
    error message ``"No worker registered."``. Also pins the exact
    ``logger.warning`` call (format string plus all three substitution
    arguments: worker_id, mode, err)."""
    hub = _InputModeHub(worker_id=WID, mode="open", result=(False, "not_found"))
    result = await _set_input_mode(hub, WID, "open")
    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
    assert json.loads(result.body) == {"error": "No worker registered."}
    assert logger_recorder.warning_calls == [
        ("rest_input_mode_error worker_id=%s mode=%s err=%s", WID, "open", "not_found")
    ]
    assert logger_recorder.info_calls == []


async def test_set_input_mode_other_error_returns_409(logger_recorder: _LoggerRecorder) -> None:
    """Any non-``"not_found"`` error path: kills the ``409 -> <other int>``
    number mutant and pins the exact ``"Cannot switch to open while hijack
    is active."`` literal. Uses a distinct err string ("busy") from the
    404 test so a mutant that hardcodes status 404 regardless of ``err``
    cannot pass both tests, and pins the ``logger.warning`` call with that
    exact err value threaded through."""
    hub = _InputModeHub(worker_id=WID, mode="open", result=(False, "busy"))
    result = await _set_input_mode(hub, WID, "open")
    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    assert json.loads(result.body) == {"error": "Cannot switch to open while hijack is active."}
    assert logger_recorder.warning_calls == [("rest_input_mode_error worker_id=%s mode=%s err=%s", WID, "open", "busy")]
    assert logger_recorder.info_calls == []


# ---------------------------------------------------------------------------
# _disconnect_worker
# ---------------------------------------------------------------------------


class _DisconnectHub:
    """Strict fake mirroring ``TermHub.disconnect_worker(worker_id) -> bool``."""

    def __init__(self, *, worker_id: str, result: bool) -> None:
        self._worker_id = worker_id
        self._result = result
        self.calls: list[str] = []

    async def disconnect_worker(self, worker_id: str) -> bool:
        assert worker_id == self._worker_id, f"disconnect_worker got worker_id={worker_id!r}"
        self.calls.append(worker_id)
        return self._result


async def test_disconnect_worker_success_returns_exact_dict(logger_recorder: _LoggerRecorder) -> None:
    """Success path: kills argument-substitution mutants on the
    ``hub.disconnect_worker(worker_id)`` call, kills ``not`` removal on
    ``if not ok:`` (an inverted guard would route this ``ok=True`` case into
    the 404 branch), and pins the exact returned dict literally. Also pins
    the exact ``logger.info`` call (format string plus the worker_id
    argument)."""
    hub = _DisconnectHub(worker_id=WID, result=True)
    result = await _disconnect_worker(hub, WID)
    assert result == {"ok": True, "worker_id": WID}
    assert hub.calls == [WID]
    assert logger_recorder.info_calls == [("rest_disconnect_ok worker_id=%s", WID)]
    assert logger_recorder.warning_calls == []


async def test_disconnect_worker_failure_returns_404(logger_recorder: _LoggerRecorder) -> None:
    """Failure path: kills the ``404 -> <other int>`` number mutant and pins
    the exact ``"No worker connected."`` error literal. Also pins the exact
    ``logger.warning`` call (format string plus the worker_id argument)."""
    hub = _DisconnectHub(worker_id=WID, result=False)
    result = await _disconnect_worker(hub, WID)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
    assert json.loads(result.body) == {"error": "No worker connected."}
    assert logger_recorder.warning_calls == [("rest_disconnect_no_worker worker_id=%s", WID)]
    assert logger_recorder.info_calls == []


# ---------------------------------------------------------------------------
# register_workerctl_routes
# ---------------------------------------------------------------------------


def test_register_workerctl_routes_registers_exact_paths() -> None:
    """Pins that exactly two POST routes are registered, at exactly the
    literal paths ``/worker/{worker_id}/input_mode`` and
    ``/worker/{worker_id}/disconnect_worker`` -- catching path-string
    mangling and a dropped/duplicated route registration."""
    router = APIRouter()
    register_workerctl_routes(hub=object(), router=router)
    routes_by_path = {route.path: route for route in router.routes}
    assert set(routes_by_path) == {
        "/worker/{worker_id}/input_mode",
        "/worker/{worker_id}/disconnect_worker",
    }
    for path in ("/worker/{worker_id}/input_mode", "/worker/{worker_id}/disconnect_worker"):
        assert routes_by_path[path].methods == {"POST"}


async def test_registered_input_mode_endpoint_delegates_to_module_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint registered at ``/worker/{worker_id}/input_mode`` must call
    module-level ``_set_input_mode`` with exactly ``(hub, worker_id,
    request.input_mode)`` -- not some other hub, not a different worker_id,
    and not e.g. the whole request object. Catches a dropped/None-substituted
    argument in the closure itself, independent of ``_set_input_mode``'s own
    body (which is monkeypatched out here)."""
    from provide.uterm.server.bridge.models import InputModeRequest

    calls: list[tuple[Any, str, str]] = []

    async def fake_set_input_mode(hub: Any, worker_id: str, input_mode: str) -> Any:
        calls.append((hub, worker_id, input_mode))
        return {"ok": True, "input_mode": input_mode, "worker_id": worker_id}

    monkeypatch.setattr(rest_workerctl, "_set_input_mode", fake_set_input_mode)

    sentinel_hub = object()
    router = APIRouter()
    register_workerctl_routes(hub=sentinel_hub, router=router)

    endpoint = next(r for r in router.routes if r.path == "/worker/{worker_id}/input_mode").endpoint
    result = await endpoint(worker_id=WID, request=InputModeRequest(input_mode="open"))

    assert calls == [(sentinel_hub, WID, "open")]
    assert result == {"ok": True, "input_mode": "open", "worker_id": WID}


async def test_registered_disconnect_endpoint_delegates_to_module_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint registered at ``/worker/{worker_id}/disconnect_worker``
    must call module-level ``_disconnect_worker`` with exactly ``(hub,
    worker_id)``. Catches a dropped/None-substituted argument in the closure
    itself, independent of ``_disconnect_worker``'s own body (which is
    monkeypatched out here)."""
    calls: list[tuple[Any, str]] = []

    async def fake_disconnect_worker(hub: Any, worker_id: str) -> Any:
        calls.append((hub, worker_id))
        return {"ok": True, "worker_id": worker_id}

    monkeypatch.setattr(rest_workerctl, "_disconnect_worker", fake_disconnect_worker)

    sentinel_hub = object()
    router = APIRouter()
    register_workerctl_routes(hub=sentinel_hub, router=router)

    endpoint = next(r for r in router.routes if r.path == "/worker/{worker_id}/disconnect_worker").endpoint
    result = await endpoint(worker_id=WID)

    assert calls == [(sentinel_hub, WID)]
    assert result == {"ok": True, "worker_id": WID}
