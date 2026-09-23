#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for four small ``browser_handlers`` functions:
``_select_resumed_role``, ``_is_noop_policy_gate``, ``_handle_heartbeat`` and
``_handle_snapshot_req``.

Kill-suite only -- behavioural coverage of these lives in
``test_browser_handlers_*.py`` and ``test_handle_resume_*_mutation_killing.py``,
and none of it reaches the branches mutmut 3.8 (2026-09-23) left surviving:

- ``test_browser_handlers_coverage.py``'s ``TestSnapshotReq``/``TestHeartbeat``
  drive a real, in-process ``TermHub`` on the real wall clock. They assert
  only that a frame *was* sent (``assert_called``/``assert_not_called``) or
  that a key is present (``"lease_expires_at" in payload``), never the exact
  ``wall_expires``/``ts`` values or the exact ``broadcast_hijack_state``
  argument -- so a sign flip in the lease-to-wall-clock conversion, or a
  broadcast to the wrong worker id, is invisible to it.
- ``test_handle_resume_success_mutation_killing.py`` and
  ``test_handle_resume_gates_mutation_killing.py`` monkeypatch
  ``browser_handlers._select_resumed_role`` itself with a scripted stub
  (``_role_stub``/``select``) precisely so ``_handle_resume``'s own branches
  can be tested in isolation -- which means neither suite ever executes
  ``_select_resumed_role``'s real body.
- ``test_handle_input_mutation_killing.py`` (and every other caller) only
  ever passes a real ``NoOpPolicyGate()`` instance to ``_is_noop_policy_gate``,
  which is caught by the ``isinstance`` check on the first line. The
  duck-typed ``type(gate).__name__``/``__module__`` fallback below it -- the
  whole point of the function, per its own docstring ("including module
  aliases") -- is never reached by any existing test.

Everything here drives each function directly: ``_handle_heartbeat`` against
a strict hub with the clock pinned so ``wall_expires``/``ts`` are exact,
``_handle_snapshot_req`` against a strict hub that asserts every argument,
``_is_noop_policy_gate`` against real/subclass/duck-typed gate objects built
with ``type(...)``, and ``_select_resumed_role`` (a pure function) against
concrete ``(role, session_role)`` inputs, including invalid role strings.

Documented equivalents (to go into ``mutation_equivalents.toml`` when
``browser_handlers.py`` joins the perimeter):

- ``_select_resumed_role`` mutmut_5, 7, 8: the *session-role* side's
  ``_ROLE_PRIORITY.get(session_role, 0)`` default argument mangled to
  ``None``, omitted (``get(session_role, )``) or changed to ``1``. The
  guard clause ``session_role in VALID_ROLES and ...`` short-circuits on
  ``and``, so this ``.get(...)`` only ever executes once ``session_role``
  is already known to be a member of ``VALID_ROLES`` -- and ``VALID_ROLES``
  (``{"viewer", "operator", "admin"}``) is exactly the key set of
  ``_ROLE_PRIORITY``. The key is therefore always present and the default
  is unreachable for any input, valid or invalid; no ``(role, session_role)``
  pair can distinguish these three mutants from the original. (Contrast
  with the *requested-role* side's ``_ROLE_PRIORITY.get(role, 0)``, which
  has no such guard -- ``role`` is never checked against ``VALID_ROLES`` --
  so mutmut_11/13/14, the equivalent mutations on that side, are real and
  killed below.)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.frames import make_heartbeat_ack_frame
from provide.uterm.server.bridge.hub.ext import NoOpPolicyGate
from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import (
    _handle_heartbeat,
    _handle_snapshot_req,
    _is_noop_policy_gate,
    _select_resumed_role,
)

WID = "small-fns-worker"


class _WS:
    """A browser socket that records every frame sent to it."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        assert isinstance(text, str), f"send_text got {text!r}"
        self.sent.append(text)


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``browser_handlers.time`` for the whole module.

    Only ``_handle_heartbeat`` reads it; the other three functions under test
    never touch ``time``, so patching it unconditionally is harmless to them.
    """
    monkeypatch.setattr(browser_handlers, "time", SimpleNamespace(time=lambda: NOW, monotonic=lambda: MONO))


# ---------------------------------------------------------------------------
# _select_resumed_role
# ---------------------------------------------------------------------------

_ROLE_MATRIX: list[tuple[str, str, tuple[str, bool]]] = [
    ("viewer", "viewer", ("viewer", False)),
    ("viewer", "operator", ("viewer", False)),
    ("viewer", "admin", ("viewer", False)),
    ("operator", "viewer", ("viewer", False)),
    ("operator", "operator", ("operator", False)),
    ("operator", "admin", ("operator", False)),
    ("admin", "viewer", ("viewer", False)),
    ("admin", "operator", ("operator", False)),
    ("admin", "admin", ("admin", True)),
]


@pytest.mark.parametrize(("role", "session_role", "expected"), _ROLE_MATRIX)
def test_select_resumed_role_picks_the_lower_priority_role(
    role: str, session_role: str, expected: tuple[str, bool]
) -> None:
    """Pins the exact ``(role, can_hijack)`` tuple for every combination of a
    valid requested role and a valid session role, including the diagonal
    where the two priorities are equal. General regression coverage for the
    ``and``/``<=`` branch; the targeted tests below isolate each individual
    surviving mutant."""
    assert _select_resumed_role(role, session_role) == expected


def test_an_invalid_session_role_is_never_adopted() -> None:
    """Kills mutmut_1 (``new_role`` initialised to ``None`` instead of
    ``role``) and mutmut_2 (the leading ``and`` weakened to ``or``). With an
    invalid ``session_role`` the guard is False and ``new_role`` must stay at
    its initial value of ``role``: mutmut_1 corrupts that initial value to
    ``None``, and mutmut_2's ``or`` would let the numeric comparison alone
    (0 <= 2) adopt the invalid session role anyway."""
    assert _select_resumed_role("admin", "guest") == ("admin", True)


def test_a_higher_priority_session_role_is_not_adopted() -> None:
    """Kills mutmut_4: the session role's own priority lookup forced to key
    ``None`` (default 0) instead of ``session_role``. ``session_role``
    ("operator", priority 1) outranks ``role`` ("viewer", priority 0), so it
    must NOT be adopted; the mutant's forced-0 lookup makes 0 <= 0 true and
    adopts it anyway."""
    assert _select_resumed_role("viewer", "operator") == ("viewer", False)


def test_a_lower_priority_session_role_is_adopted() -> None:
    """Kills mutmut_10: the requested role's priority lookup forced to key
    ``None`` (default 0) instead of ``role``. ``role`` ("admin", priority 2)
    outranks ``session_role`` ("operator", priority 1), so the session role
    must be adopted; the mutant's forced-0 lookup makes 1 <= 0 false and
    keeps "admin" instead."""
    assert _select_resumed_role("admin", "operator") == ("operator", False)


def test_an_invalid_requested_role_defaults_to_zero_priority() -> None:
    """Kills mutmut_9 (``<=`` weakened to ``<``) and mutmut_11/mutmut_13 (the
    *requested*-role priority default changed to ``None``/omitted, which
    raises ``TypeError`` comparing ``int <= None``). ``role`` is invalid, so
    its priority defaults to 0 -- an equal-priority collision with
    "viewer"'s real priority of 0 that exists only because this default
    (unlike the session-role side) is reachable; see the module docstring."""
    assert _select_resumed_role("guest", "viewer") == ("viewer", False)


def test_an_invalid_requested_role_still_beats_a_higher_session_role() -> None:
    """Kills mutmut_14: the requested role's priority default changed from 0
    to 1. ``role`` is invalid (priority defaults to 0) and "operator" has
    priority 1, so the session role must not be adopted; the mutant's
    forced-1 default makes 1 <= 1 true and wrongly adopts "operator"."""
    assert _select_resumed_role("root", "operator") == ("root", False)


# ---------------------------------------------------------------------------
# _is_noop_policy_gate
# ---------------------------------------------------------------------------


def _fake_gate_class(name: str, module: str) -> type:
    """Build a class with the given ``__name__``/``__module__``, mimicking a
    second import of ``NoOpPolicyGate`` reached through a different module
    path -- the "module aliases" the function's docstring refers to."""
    cls = type(name, (), {})
    cls.__module__ = module
    return cls


def test_a_real_instance_is_recognized() -> None:
    assert _is_noop_policy_gate(NoOpPolicyGate()) is True


def test_a_subclass_instance_is_recognized() -> None:
    class _Sub(NoOpPolicyGate):
        pass

    assert _is_noop_policy_gate(_Sub()) is True


def test_a_same_named_class_in_the_right_module_alias_is_recognized() -> None:
    """Kills mutmut_3 (``gate_type`` forced to ``type(None)``), mutmut_5
    (the name's ``==`` flipped to ``!=``), mutmut_6/7/8 (the "NoOpPolicyGate"
    literal mangled/lower-cased/upper-cased), mutmut_9 (``endswith(None)``,
    which raises ``TypeError``) and mutmut_10/11 (the ".bridge.hub.ext"
    suffix mangled/upper-cased). A class with the right name and module
    suffix that is NOT itself a ``NoOpPolicyGate`` subclass only reaches
    ``True`` by passing every one of those literal comparisons; get any of
    them wrong and it must return ``False`` (or raise) instead."""
    gate_cls = _fake_gate_class("NoOpPolicyGate", "other.pkg.bridge.hub.ext")
    gate = gate_cls()
    assert not isinstance(gate, NoOpPolicyGate)
    assert _is_noop_policy_gate(gate) is True


def test_a_differently_named_class_in_the_right_module_is_rejected() -> None:
    """Kills mutmut_4: the ``and`` between the name check and the module
    check weakened to ``or``. The module suffix matches but the name does
    not, so the real function must reject it; the mutant's ``or`` would
    accept it on the module check alone."""
    gate = _fake_gate_class("SomeOtherGate", "other.pkg.bridge.hub.ext")()
    assert _is_noop_policy_gate(gate) is False


def test_a_matching_name_in_the_wrong_module_is_rejected() -> None:
    """The other half of the ``and``: the right name in a module that does
    not end with ``.bridge.hub.ext`` must also be rejected."""
    gate = _fake_gate_class("NoOpPolicyGate", "other.pkg.elsewhere")()
    assert _is_noop_policy_gate(gate) is False


def test_an_unrelated_object_is_rejected() -> None:
    assert _is_noop_policy_gate(object()) is False
    assert _is_noop_policy_gate(None) is False


# ---------------------------------------------------------------------------
# _handle_heartbeat
# ---------------------------------------------------------------------------

NOW = 2000.0
MONO = 100.0
LEASE = 130.0
WALL_EXPIRES = NOW + (LEASE - MONO)  # 2030.0


class _HeartbeatHub:
    """A hub whose collaborators raise on any unexpected argument."""

    def __init__(self, ws: _WS, *, lease_expires_at: float | None) -> None:
        self.ws = ws
        self._lease_expires_at = lease_expires_at
        self.touch_calls: list[tuple[str, Any]] = []
        self.broadcasts: list[str] = []

    async def touch_if_owner(self, worker_id: str, ws: Any) -> float | None:
        assert worker_id == WID, f"touch_if_owner got worker_id={worker_id!r}"
        assert ws is self.ws, f"touch_if_owner got ws={ws!r}"
        self.touch_calls.append((worker_id, ws))
        return self._lease_expires_at

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        assert worker_id == WID, f"broadcast_hijack_state got {worker_id!r}"
        self.broadcasts.append(worker_id)


async def test_heartbeat_owner_gets_the_exact_ack_and_a_broadcast() -> None:
    """Kills mutmut_8 (the outer ``+`` before the parens flipped to ``-``),
    mutmut_9 (the inner ``-`` flipped to ``+``), mutmut_13/15 (``ts`` forced
    to ``None``/omitted -- both fall back to *frames.py*'s own, unpatched
    ``time.time()``, which can never equal our pinned clock) and mutmut_16
    (``broadcast_hijack_state`` called with ``None`` instead of the worker
    id)."""
    ws = _WS()
    hub = _HeartbeatHub(ws, lease_expires_at=LEASE)
    await _handle_heartbeat(hub, ws, WID)
    expected = encode_control_frame(make_heartbeat_ack_frame(WALL_EXPIRES, ts=NOW))
    assert hub.touch_calls == [(WID, ws)]
    assert ws.sent == [expected]
    assert hub.broadcasts == [WID]


async def test_heartbeat_from_a_non_owner_sends_nothing() -> None:
    """A non-owner (``touch_if_owner`` returns ``None``) gets no ack and
    triggers no broadcast."""
    ws = _WS()
    hub = _HeartbeatHub(ws, lease_expires_at=None)
    await _handle_heartbeat(hub, ws, WID)
    assert ws.sent == []
    assert hub.broadcasts == []


# ---------------------------------------------------------------------------
# _handle_snapshot_req
# ---------------------------------------------------------------------------


class _SnapshotHub:
    """A hub whose collaborators raise on any unexpected argument."""

    def __init__(self, ws: _WS, *, owner_lease: float | None, still_hijacked: bool) -> None:
        self.ws = ws
        self._owner_lease = owner_lease
        self._still_hijacked = still_hijacked
        self.touch_calls: list[tuple[str, Any]] = []
        self.check_calls: list[str] = []
        self.snapshot_calls: list[str] = []

    async def touch_if_owner(self, worker_id: str, ws: Any) -> float | None:
        assert worker_id == WID, f"touch_if_owner got worker_id={worker_id!r}"
        assert ws is self.ws, f"touch_if_owner got ws={ws!r}"
        self.touch_calls.append((worker_id, ws))
        return self._owner_lease

    async def check_still_hijacked(self, worker_id: str) -> bool:
        assert worker_id == WID, f"check_still_hijacked got {worker_id!r}"
        self.check_calls.append(worker_id)
        return self._still_hijacked

    async def request_snapshot(self, worker_id: str) -> None:
        assert worker_id == WID, f"request_snapshot got {worker_id!r}"
        self.snapshot_calls.append(worker_id)


async def test_snapshot_req_owner_requests_without_checking_hijack_state() -> None:
    """Kills mutmut_1 (``is_owner`` forced to ``None``, which is falsy and
    takes the non-owner branch instead of the owner one), mutmut_2/3
    (``touch_if_owner``'s ``worker_id``/``ws`` argument replaced with
    ``None``) and mutmut_7 (the owner branch's ``request_snapshot(None)``)."""
    ws = _WS()
    hub = _SnapshotHub(ws, owner_lease=42.0, still_hijacked=True)
    await _handle_snapshot_req(hub, ws, WID)
    assert hub.touch_calls == [(WID, ws)]
    assert hub.snapshot_calls == [WID]
    assert hub.check_calls == []


async def test_snapshot_req_non_owner_is_suppressed_while_still_hijacked() -> None:
    """A non-owner is forwarded no snapshot while the hijack is still
    active."""
    ws = _WS()
    hub = _SnapshotHub(ws, owner_lease=None, still_hijacked=True)
    await _handle_snapshot_req(hub, ws, WID)
    assert hub.check_calls == [WID]
    assert hub.snapshot_calls == []


async def test_snapshot_req_non_owner_requests_once_the_hijack_has_ended() -> None:
    """A non-owner's snapshot request goes through once nothing is hijacked."""
    ws = _WS()
    hub = _SnapshotHub(ws, owner_lease=None, still_hijacked=False)
    await _handle_snapshot_req(hub, ws, WID)
    assert hub.check_calls == [WID]
    assert hub.snapshot_calls == [WID]
