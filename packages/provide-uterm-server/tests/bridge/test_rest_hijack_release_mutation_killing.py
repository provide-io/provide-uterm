#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``rest._hijack_release`` and ``rest._may_release_lease``.

Kill-suite only -- behavioural coverage of the REST release route lives in
``test_rest_lease_ownership.py`` (ownership matrix, via ``AsyncMock`` hub
stubs) and the broader REST hijack flow tests. Those suites check outcomes
(status codes, ``True``/``False``) but their hub/registry/authz doubles are
``AsyncMock(return_value=...)`` objects that answer identically regardless of
which argument they were called with, so they cannot distinguish a real call
from a mutant's call that substitutes ``None`` for an argument, mangles a
literal, or drops a keyword. Under mutmut 3.8 (2026-09-23) ``_hijack_release``
had 60 survivors and ``_may_release_lease`` had 2, all on: argument
substitution into hub/registry/authz calls, the ``should_resume and
check_still_hijacked(...)`` short-circuit, the compensating ``resume``
frame's literal keys/values, the ``notify_hijack_changed``/``metric``/
``logger.info``/``append_event`` calls, and every literal in the returned
``JSONResponse``/dict bodies.

Everything here drives the two module-level functions directly against a
strict fake ``TermHub`` that asserts on the exact arguments its methods
receive (mirroring the real signatures in
``bridge/hub/core_impl.py``/``bridge/hub/lease.py``) and strict fake
``registry``/``authz`` collaborators for ``_may_release_lease``. None of
these fakes use ``AsyncMock(return_value=...)``: a mock configured that way
would still return a value after an argument was silently dropped or
replaced with ``None``, so it cannot distinguish the real call from a
mutant's. A plain assert on each received argument can. The clock is pinned
via ``monkeypatch.setattr(rest, "time", ...)`` so the compensating resume
frame's ``ts`` is exact, and ``rest.logger`` is replaced with a recorder so
the exact ``logger.info`` call (format string plus every substitution
argument) can be pinned.

Documented equivalents:

- mutmut_52 (``should_resume = False`` -> ``should_resume = None`` in the
  re-check branch) is genuinely equivalent, for the same reason as the
  analogous ``_do_resume`` case documented in
  ``test_handle_hijack_release_mutation_killing.py``: ``should_resume`` is a
  local variable read only in the boolean context ``if should_resume:``
  immediately afterward, and never returned or passed anywhere else in the
  function. ``False`` and ``None`` are indistinguishable at every
  observation point available to a caller. Confirmed by exec'ing the
  extracted mutant body against the strict fake hub below: with
  ``should_resume=True`` and ``still_hijacked=True``, both the original and
  mutmut_52 produce identical ``unowned_sends == []`` and
  ``still_hijacked_checks == [worker_id]``. mutmut_53, which reassigns
  ``should_resume = True`` instead in that same branch, *is* observable (it
  leaves the compensating resume armed despite the concurrent re-acquire)
  and is killed below by
  ``test_concurrent_reacquire_cancels_resume_but_not_the_unconditional_tail``.

- mutmut_77 (``hub.notify_hijack_changed(worker_id, enabled=False, owner=None)``
  -> ``hub.notify_hijack_changed(worker_id, enabled=False, )``, i.e. the
  ``owner=None`` keyword dropped) is genuinely equivalent because the real
  ``notify_hijack_changed(self, worker_id, *, enabled, owner=None)``
  (``bridge/hub/core_impl.py``, ``bridge/hub/store.py``, ``bridge/hub/lease.py``,
  and the ``TermHub`` protocol in ``bridge/hub/__init__.py``) already
  defaults ``owner`` to ``None`` -- the exact value the source explicitly
  passes. Confirmed by exec'ing the extracted mutant body against a fake
  whose ``notify_hijack_changed`` mirrors that real default signature: the
  original and mutmut_77 produce the identical recorded call
  ``(worker_id, False, None)``. (Contrast with the unrelated ``data=None``
  default on ``append_event`` -- mutmut_97 drops the *explicit* non-None
  payload dict there, which is observably different from the default and is
  killed below.)
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import JSONResponse

from provide.uterm.server.bridge.routes import rest
from provide.uterm.server.bridge.routes.rest import _hijack_release, _may_release_lease

WID = "worker-1"
HID = "hijack-1"
OWNER = "alice"
NOW = 5555.0

REQ = SimpleNamespace(state=SimpleNamespace(uterm_principal=None))


async def _call(coro: Any) -> Any:
    """Every direct call goes through a bounded wait so a wrong fake that
    hangs (e.g. an ``async def`` never returning) fails fast instead of
    stalling the suite."""
    return await asyncio.wait_for(coro, 2)


def _hs(*, acquired_by: str | None = None, owner: str = OWNER) -> SimpleNamespace:
    return SimpleNamespace(acquired_by=acquired_by, owner=owner)


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest, "time", SimpleNamespace(time=lambda: NOW))


@pytest.fixture
def logger_recorder(monkeypatch: pytest.MonkeyPatch) -> _LoggerRecorder:
    recorder = _LoggerRecorder()
    monkeypatch.setattr(rest, "logger", recorder)
    return recorder


class _LoggerRecorder:
    """Strict stand-in for ``rest.logger``; records the exact positional call."""

    def __init__(self) -> None:
        self.info_calls: list[tuple[Any, ...]] = []

    def info(self, *args: Any) -> None:
        self.info_calls.append(args)


# ---------------------------------------------------------------------------
# _hijack_release: strict fake hub
# ---------------------------------------------------------------------------


class _Hub:
    """Strict fake mirroring the ``TermHub`` methods ``_hijack_release`` calls.

    Signatures mirror ``bridge/hub/core_impl.py``:
    ``get_rest_session(worker_id, hijack_id)``,
    ``release_rest_hijack(worker_id, hijack_id) -> (bool, bool)``,
    ``check_still_hijacked(worker_id) -> bool``,
    ``send_worker_if_unowned(worker_id, msg) -> bool``,
    ``notify_hijack_changed(worker_id, *, enabled, owner=None)``,
    ``metric(name)``, ``append_event(worker_id, event_type, data=None)``,
    ``broadcast_hijack_state(worker_id)``, ``prune_if_idle(worker_id)``.
    """

    def __init__(
        self,
        *,
        session: Any,
        released: bool = True,
        should_resume: bool = False,
        still_hijacked: bool = False,
    ) -> None:
        self._session = session
        self._released = released
        self._should_resume = should_resume
        self._still_hijacked = still_hijacked
        self.get_rest_session_calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, str]] = []
        self.still_hijacked_checks: list[str] = []
        self.unowned_sends: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, bool, str | None]] = []
        self.metrics: list[str] = []
        self.events: list[tuple[str, str, dict[str, Any] | None]] = []
        self.broadcasts: list[str] = []
        self.pruned: list[str] = []

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> Any:
        assert (worker_id, hijack_id) == (WID, HID), f"get_rest_session got {(worker_id, hijack_id)!r}"
        self.get_rest_session_calls.append((worker_id, hijack_id))
        return self._session

    async def release_rest_hijack(self, worker_id: str, hijack_id: str) -> tuple[bool, bool]:
        assert (worker_id, hijack_id) == (WID, HID), f"release_rest_hijack got {(worker_id, hijack_id)!r}"
        self.release_calls.append((worker_id, hijack_id))
        return self._released, self._should_resume

    async def check_still_hijacked(self, worker_id: str) -> bool:
        assert worker_id == WID, f"check_still_hijacked got worker_id={worker_id!r}"
        self.still_hijacked_checks.append(worker_id)
        return self._still_hijacked

    async def send_worker_if_unowned(self, worker_id: str, msg: dict[str, Any]) -> bool:
        assert worker_id == WID, f"send_worker_if_unowned got worker_id={worker_id!r}"
        self.unowned_sends.append((worker_id, msg))
        return True

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        assert worker_id == WID, f"notify_hijack_changed got worker_id={worker_id!r}"
        self.notifications.append((worker_id, enabled, owner))

    def metric(self, name: str) -> None:
        self.metrics.append(name)

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> Any:
        self.events.append((worker_id, event_type, data))

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcasts.append(worker_id)

    async def prune_if_idle(self, worker_id: str) -> None:
        self.pruned.append(worker_id)


# ---------------------------------------------------------------------------
# _hijack_release
# ---------------------------------------------------------------------------


async def test_missing_session_returns_exact_404() -> None:
    """``hub.get_rest_session`` answers ``None`` (invalid/expired hijack_id):
    kills the ``JSONResponse(None, status_code=404)`` content-drop mutant
    (mutmut_7) and every case-flip/marker mutation of the ``"error"`` key and
    the ``"Invalid or expired hijack session."`` message (mutmut_11-15) by
    asserting the exact status code and the exact decoded body."""
    hub = _Hub(session=None)
    resp = await _call(_hijack_release(hub, REQ, WID, HID))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 404
    assert json.loads(resp.body) == {"error": "Invalid or expired hijack session."}
    assert hub.release_calls == []


async def test_release_denied_passes_exact_args_and_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_may_release_lease`` is replaced with a recorder so the exact
    ``(http_request, worker_id, hs)`` it is called with can be pinned --
    kills mutmut_19, which substitutes ``None`` for ``worker_id`` in that
    call. A denial (``False``) must short-circuit before
    ``hub.release_rest_hijack`` and return the exact 403 body, killing the
    ``JSONResponse(None, status_code=403)`` content-drop (mutmut_24) and
    every case-flip/marker mutation of ``"error"``/``"Not the lease
    owner."`` (mutmut_28-32)."""
    calls: list[tuple[Any, str, Any]] = []

    async def fake_may_release_lease(http_request: Any, worker_id: str, hs: Any) -> bool:
        calls.append((http_request, worker_id, hs))
        return False

    monkeypatch.setattr(rest, "_may_release_lease", fake_may_release_lease)
    hs = _hs()
    hub = _Hub(session=hs)
    resp = await _call(_hijack_release(hub, REQ, WID, HID))
    assert calls == [(REQ, WID, hs)]
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 403
    assert json.loads(resp.body) == {"error": "Not the lease owner."}
    assert hub.release_calls == []


async def test_hub_release_failure_returns_exact_404() -> None:
    """``hub.release_rest_hijack`` answers ``released=False`` (lease vanished
    between the ownership check and the release): kills the second
    occurrence's case-flip/marker mutation of the ``"Invalid or expired
    hijack session."`` message (mutmut_46) by asserting the exact decoded
    body, distinct from the first occurrence covered by
    ``test_missing_session_returns_exact_404``."""
    hs = _hs(acquired_by=None)
    hub = _Hub(session=hs, released=False)
    resp = await _call(_hijack_release(hub, REQ, WID, HID))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 404
    assert json.loads(resp.body) == {"error": "Invalid or expired hijack session."}


async def test_should_resume_false_skips_the_recheck_entirely(logger_recorder: _LoggerRecorder) -> None:
    """``released=True, should_resume=False``: the ``and`` in ``if
    should_resume and await hub.check_still_hijacked(worker_id):`` must
    short-circuit, so ``check_still_hijacked`` is never called. Kills
    mutmut_50, which flips that ``and`` to ``or`` -- under the mutant,
    ``check_still_hijacked`` would be invoked (and, answering ``False``
    here, leave ``should_resume`` unchanged) even though the real code never
    touches it. The unconditional tail still runs."""
    hs = _hs(acquired_by=None)
    hub = _Hub(session=hs, released=True, should_resume=False)
    result = await _call(_hijack_release(hub, REQ, WID, HID))
    assert hub.still_hijacked_checks == []
    assert hub.unowned_sends == []
    assert hub.notifications == [(WID, False, None)]
    assert hub.metrics == ["hijack_releases_total"]
    assert hub.broadcasts == [WID]
    assert hub.pruned == [WID]
    assert result == {"ok": True, "worker_id": WID, "hijack_id": HID}
    assert logger_recorder.info_calls == [("rest_release_ok worker_id=%s hijack_id=%s owner=%s", WID, HID, OWNER)]


async def test_concurrent_reacquire_cancels_resume_but_not_the_unconditional_tail() -> None:
    """``released=True, should_resume=True, still_hijacked=True``:
    ``check_still_hijacked`` is called with the exact worker_id (kills
    mutmut_51, which passes ``None`` instead), and answering ``True`` (a
    concurrent hijack_acquire raced in) must cancel the resume: no
    compensating ``resume`` frame is sent. This kills mutmut_53, which
    reassigns ``should_resume = True`` instead of ``False`` in that
    branch -- under the mutant, ``send_worker_if_unowned`` would fire
    despite the fresh concurrent hijack. (mutmut_52, which assigns ``None``
    instead of ``False``, is a documented equivalent -- see module
    docstring.) The unconditional notify/metric/broadcast/prune tail still
    runs even though the resume was cancelled, per the source comment."""
    hs = _hs(acquired_by=None)
    hub = _Hub(session=hs, released=True, should_resume=True, still_hijacked=True)
    result = await _call(_hijack_release(hub, REQ, WID, HID))
    assert hub.still_hijacked_checks == [WID]
    assert hub.unowned_sends == []
    assert hub.notifications == [(WID, False, None)]
    assert hub.metrics == ["hijack_releases_total"]
    assert hub.broadcasts == [WID]
    assert hub.pruned == [WID]
    assert result == {"ok": True, "worker_id": WID, "hijack_id": HID}


async def test_clean_release_sends_exact_resume_frame_and_full_tail(logger_recorder: _LoggerRecorder) -> None:
    """``released=True, should_resume=True, still_hijacked=False`` (no
    concurrent re-acquire): the full happy path runs end to end. This one
    test pins, by exact equality, every remaining survivor on the tail:

    - ``send_worker_if_unowned(worker_id, {...})``: the exact worker_id
      (mutmut_54) and every key/value of the resume frame -- ``"type"``
      (mutmut_58/59), ``"control"`` (mutmut_60/61), ``"action"``
      (mutmut_62/63), ``"resume"`` (mutmut_64/65), ``"owner"``
      (mutmut_66/67), ``"lease_s"`` (mutmut_68/69), its value ``0``
      (mutmut_70), and ``"ts"`` (mutmut_71/72), pinned via the clock fixture.
    - ``notify_hijack_changed(worker_id, enabled=False, owner=None)``: the
      exact worker_id (mutmut_73) and ``enabled`` value (mutmut_74).
      (mutmut_77, which drops the ``owner=None`` kwarg, is a documented
      equivalent -- see module docstring.)
    - ``metric("hijack_releases_total")`` (mutmut_79/80/81).
    - ``logger.info(...)``: the exact format string and all three
      substitution arguments (mutmut_83/84/85/90).
    - ``append_event(worker_id, "hijack_released", {...})``: worker_id
      (mutmut_92), the event name (mutmut_93/98/99), the payload dict
      including the dropped-argument case (mutmut_94/97), and its
      ``"hijack_id"``/``"owner"`` keys (mutmut_100/101/102/103).
    - ``broadcast_hijack_state(worker_id)`` (mutmut_104).
    - ``prune_if_idle(worker_id)`` (mutmut_105).
    - the final returned dict's ``"worker_id"``/``"hijack_id"`` keys
      (mutmut_109/110/111/112).
    """
    hs = _hs(acquired_by=None, owner=OWNER)
    hub = _Hub(session=hs, released=True, should_resume=True, still_hijacked=False)
    result = await _call(_hijack_release(hub, REQ, WID, HID))
    assert hub.still_hijacked_checks == [WID]
    assert hub.unowned_sends == [
        (WID, {"type": "control", "action": "resume", "owner": OWNER, "lease_s": 0, "ts": NOW})
    ]
    assert hub.notifications == [(WID, False, None)]
    assert hub.metrics == ["hijack_releases_total"]
    assert logger_recorder.info_calls == [("rest_release_ok worker_id=%s hijack_id=%s owner=%s", WID, HID, OWNER)]
    assert hub.events == [(WID, "hijack_released", {"hijack_id": HID, "owner": OWNER})]
    assert hub.broadcasts == [WID]
    assert hub.pruned == [WID]
    assert result == {"ok": True, "worker_id": WID, "hijack_id": HID}


# ---------------------------------------------------------------------------
# _may_release_lease
# ---------------------------------------------------------------------------


class _StrictRegistry:
    """Strict fake mirroring ``registry.get_definition(session_id) -> SessionDefinition | None``
    (``server/registry.py``)."""

    def __init__(self, *, worker_id: str, session: Any) -> None:
        self._worker_id = worker_id
        self._session = session
        self.calls: list[str] = []

    async def get_definition(self, worker_id: str) -> Any:
        assert worker_id == self._worker_id, f"get_definition got worker_id={worker_id!r}"
        self.calls.append(worker_id)
        return self._session


class _StrictAuthz:
    """Strict fake mirroring ``authz.is_admin(principal) -> bool`` (``server/authorization.py``)."""

    def __init__(self, *, principal: Any, is_admin: bool) -> None:
        self._principal = principal
        self._is_admin = is_admin
        self.calls: list[Any] = []

    async def is_admin(self, principal: Any) -> bool:
        assert principal is self._principal, f"is_admin got principal={principal!r}"
        self.calls.append(principal)
        return self._is_admin


def _admin_fallback_request(*, is_admin: bool) -> tuple[SimpleNamespace, _StrictRegistry, _StrictAuthz]:
    """A request that reaches the third branch of ``_may_release_lease``: the
    requester is neither the acquirer nor found as the session owner (the
    registry lookup answers ``None``), so authorisation falls through to the
    admin check."""
    principal = SimpleNamespace(subject_id="admin-1")
    registry = _StrictRegistry(worker_id=WID, session=None)
    authz = _StrictAuthz(principal=principal, is_admin=is_admin)
    req = SimpleNamespace(
        state=SimpleNamespace(uterm_principal=principal),
        app=SimpleNamespace(state=SimpleNamespace(uterm_registry=registry, uterm_authz=authz)),
    )
    return req, registry, authz


async def test_acquirer_short_circuit_touches_neither_registry_nor_authz() -> None:
    """``hs.acquired_by == requester``: the first ``or`` operand's own match
    returns ``True`` immediately. Documents the baseline that the admin
    fallback test below is contrasted against -- neither collaborator is
    reached when the acquirer themself releases."""
    req, registry, authz = _admin_fallback_request(is_admin=True)
    hs = _hs(acquired_by="admin-1")
    assert await _call(_may_release_lease(req, WID, hs)) is True
    assert registry.calls == []
    assert authz.calls == []


async def test_admin_fallback_passes_exact_worker_id_and_exact_principal() -> None:
    """A non-acquirer, non-owner requester falls through to the registry
    lookup and then the admin check. Kills mutmut_8, which substitutes
    ``None`` for ``worker_id`` in ``registry.get_definition(worker_id)``,
    and mutmut_14, which substitutes ``None`` for
    ``http_request.state.uterm_principal`` in ``authz.is_admin(...)`` --
    both caught by the strict fakes asserting their exact received
    argument."""
    req, registry, authz = _admin_fallback_request(is_admin=True)
    hs = _hs(acquired_by="someone-else")
    assert await _call(_may_release_lease(req, WID, hs)) is True
    assert registry.calls == [WID]
    assert authz.calls == [req.state.uterm_principal]
