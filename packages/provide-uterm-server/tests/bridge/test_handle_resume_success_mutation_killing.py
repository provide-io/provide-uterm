#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for the success-path tail of ``_handle_resume``.

Covers ``browser_handlers._handle_resume`` from the point the single-use
resume token has been consumed (``if new_role != role: ...``) through the
final ``return owned_hijack``. The token-lookup/validation/creation half of
the function (everything up to and including ``store.create``/
``store.consume``) is covered by a sibling suite
(``test_handle_resume_gates_mutation_killing.py``); this file only needs that
half to *succeed*, so it reaches its own region.

The 86 mutants documented in ``resume_B.txt`` for this region survive because
existing behavioural suites drive ``_handle_resume`` through
``AsyncMock``-flavoured hubs that answer whatever they are called with (or
never reach this success path at all), so a ``worker_id`` forced to ``None``,
a snapshot ``.get(...)`` default silently changed, or a dropped
``notify_hijack_changed`` keyword argument all still make the call and never
surface as a failure.

Every test here drives ``_handle_resume`` directly. ``_select_resumed_role``
and ``_try_reclaim_hijack`` are monkeypatched to strict stand-ins that return
a chosen ``(new_role, can_hijack)`` / ``(owned_hijack, reclaimed_hijack,
competing_owner)`` — both have their own kill-suites, so recreating their
internal branching here would only blur which suite kills what. The hub's
``resume_store`` and collaborator methods are fakes whose signatures mirror
the real ``TermHub`` methods exactly (no ``*args``/``**kwargs`` catch-alls),
so a mutant that drops a required argument (e.g. ``notify_hijack_changed``'s
``worker_id`` or ``enabled``) raises ``TypeError`` on its own, and every other
call is simply recorded and compared against the exact expected tuple. The
hello frame and the ``hijack_state_msg_for`` frame are compared as the exact
``encode_control_frame(...)`` string a real browser would receive, which
catches both a value silently replaced and a keyword argument dropped
outright (``make_hello_frame`` takes ``**payload``, so a dropped kwarg is not
a ``TypeError`` — it is a key quietly missing from the wire frame).

Documented equivalents: none. Every one of the 86 ids in ``resume_B.txt`` is
killed below by an explicit input that makes the mutated call produce a
different observable result (a recorded argument, the exact frame text, or
the exact log call) from the unmutated function.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.frames import make_hello_frame
from provide.uterm.server.bridge.hub.resume import ResumeSession
from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import _handle_resume

if TYPE_CHECKING:
    from collections.abc import Callable

WID = "resume-worker"
OLD_TOKEN = "old-token-abc"
NEW_TOKEN = "new-token-xyz"


class _WS:
    """A browser socket that records every frame sent to it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        assert isinstance(text, str), f"{self.name}.send_text got {text!r}"
        self.sent.append(text)

    def __repr__(self) -> str:
        return f"_WS({self.name})"


class _Logger:
    def __init__(self) -> None:
        self.info_calls: list[tuple[Any, ...]] = []

    def info(self, *args: Any, **kwargs: Any) -> None:
        assert not kwargs, f"logger.info got kwargs {kwargs!r}"
        self.info_calls.append(args)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        # Not exercised on this success path; kept permissive so an
        # unexpected warning fails a test assertion, not this recorder.
        pass


@pytest.fixture(autouse=True)
def _log(monkeypatch: pytest.MonkeyPatch) -> _Logger:
    """Replace the module logger so the closing ``logger.info`` is exact."""
    log = _Logger()
    monkeypatch.setattr(browser_handlers, "logger", log)
    return log


class _Store:
    """A resume-token store that always succeeds.

    The ``create``/``consume``/``get`` mutation ids live in the sibling
    "gates" suite (the token-creation half of ``_handle_resume``); this fake
    exists only to get the function past that gate and into the success-path
    region this file covers.
    """

    def __init__(self, session: ResumeSession, *, new_token: str) -> None:
        self._session = session
        self._new_token = new_token

    async def get(self, token: str) -> ResumeSession | None:
        assert token == OLD_TOKEN, f"store.get got token={token!r}"
        return self._session

    async def create(self, worker_id: str, role: str, ttl_s: float) -> str:
        assert worker_id == WID, f"store.create got worker_id={worker_id!r}"
        return self._new_token

    async def consume(self, token: str) -> ResumeSession | None:
        assert token == OLD_TOKEN, f"store.consume got token={token!r}"
        return self._session


class _Hub:
    """A hub whose collaborator methods mirror ``TermHub``'s real signatures
    exactly and simply record what they were called with. Each test compares
    the recorded calls against the exact expected tuple, which catches a
    value silently swapped for ``None``/a mangled literal just as well as a
    dropped argument that a loose ``AsyncMock`` would have silently accepted.
    """

    def __init__(
        self,
        ws: _WS,
        *,
        snapshot: dict[str, Any],
        hijack_state_msg: dict[str, Any],
        session: ResumeSession,
        new_token: str = NEW_TOKEN,
    ) -> None:
        self.ws = ws
        self.resume_store = _Store(session, new_token=new_token)
        self._on_resume = None
        self._resume_ttl_s = 30.0
        self._lock = asyncio.Lock()
        self._snapshot = snapshot
        self._hijack_state_msg = hijack_state_msg
        self.role_calls: list[tuple[Any, Any, Any]] = []
        self.bind_calls: list[tuple[Any, Any, bool]] = []
        self.broadcast_calls: list[Any] = []
        self.notify_calls: list[tuple[Any, Any, Any]] = []
        self.event_calls: list[tuple[Any, Any, Any]] = []

    async def wait_resume_token_ready(self, token: str, ws: Any) -> bool:
        return True

    async def set_browser_role(self, worker_id: str, ws: Any, role: str) -> None:
        self.role_calls.append((worker_id, ws, role))

    def _bind_resume_token_locked(self, ws: Any, token: str) -> None:
        # Record whether the lock was held *at call time* as data, rather
        # than asserting it, so the recorded tuple alone proves both the
        # arguments and the locking discipline.
        self.bind_calls.append((ws, token, self._lock.locked()))

    async def register_browser_state_snapshot(self, worker_id: str, ws: Any) -> dict[str, Any]:
        assert (worker_id, ws) == (WID, self.ws), f"register_browser_state_snapshot got {(worker_id, ws)!r}"
        return self._snapshot

    async def hijack_state_msg_for(self, worker_id: str, ws: Any) -> dict[str, Any]:
        assert (worker_id, ws) == (WID, self.ws), f"hijack_state_msg_for got {(worker_id, ws)!r}"
        return self._hijack_state_msg

    async def broadcast_hijack_state(self, worker_id: str) -> None:
        self.broadcast_calls.append(worker_id)

    def notify_hijack_changed(self, worker_id: str, *, enabled: bool, owner: str | None = None) -> None:
        self.notify_calls.append((worker_id, enabled, owner))

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> None:
        self.event_calls.append((worker_id, event_type, data))


def _session(*, was_hijack_owner: bool = False) -> ResumeSession:
    return ResumeSession(
        token=OLD_TOKEN,
        worker_id=WID,
        role="viewer",
        created_at=0.0,
        expires_at=100.0,
        was_hijack_owner=was_hijack_owner,
        wall_created_at=0.0,
    )


def _role_stub(new_role: str, can_hijack: bool) -> Callable[[str, str], tuple[str, bool]]:
    """A strict stand-in for ``_select_resumed_role`` (its own suite covers
    its role-priority logic): returns a fixed pair regardless of its real
    inputs, so this file can drive the tail of ``_handle_resume`` without
    recreating that logic.
    """

    def _select(role: str, session_role: str) -> tuple[str, bool]:
        return new_role, can_hijack

    return _select


def _reclaim_stub(
    owned_hijack: bool, reclaimed_hijack: bool, *, competing_owner: bool = False
) -> Callable[[Any, Any, str, Any, bool], Any]:
    """A strict stand-in for ``_try_reclaim_hijack`` (its own suite covers its
    reclaim logic): asserts the ``worker_id`` it is handed and returns a
    fixed ``(owned_hijack, reclaimed_hijack, competing_owner)``.
    """

    async def _reclaim(hub: Any, ws: Any, worker_id: str, session: Any, can_hijack: bool) -> tuple[bool, bool, bool]:
        assert worker_id == WID, f"_try_reclaim_hijack got worker_id={worker_id!r}"
        return owned_hijack, reclaimed_hijack, competing_owner

    return _reclaim


async def _resume(hub: _Hub, ws: _WS, *, role: str) -> bool:
    # The ``owned_hijack`` argument is unconditionally overwritten by
    # ``_try_reclaim_hijack``'s (stubbed) return value before this region
    # runs, so its starting value here is immaterial.
    return await _handle_resume(hub, ws, WID, role, {"token": OLD_TOKEN}, False)


_DEFAULT_HELLO_KWARGS: dict[str, Any] = {
    "worker_id": WID,
    "can_hijack": True,
    "hijacked": True,
    "hijacked_by_me": True,
    "worker_online": True,
    "input_mode": "distinct-input-mode",
    "role": "operator",
    "hijack_control": "ws",
    "hijack_step_supported": True,
    "capabilities": {"hijack_control": "ws", "hijack_step_supported": True},
    "resume_supported": True,
    "resume_token": NEW_TOKEN,
    "resumed": True,
}


def _expected_hello(**overrides: Any) -> str:
    kwargs = dict(_DEFAULT_HELLO_KWARGS)
    kwargs.update(overrides)
    return encode_control_frame(make_hello_frame(**kwargs))


# ---------------------------------------------------------------------------
# The role change and the token bind under the lock
# ---------------------------------------------------------------------------


async def test_a_role_change_calls_set_browser_role_and_binds_the_new_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_137 (``!=`` flipped to ``==``, which would skip the call
    here where the role actually changes), 138-140 (``set_browser_role``'s
    three arguments each forced ``None``), 141-143 (an argument dropped from
    the call, which raises ``TypeError`` against the real 3-positional
    signature before this fake's body ever runs), and 144/145
    (``_bind_resume_token_locked``'s ``ws``/``new_token`` forced ``None``,
    caught by comparing the recorded call - including whether the lock was
    held - against the exact expected tuple)."""
    ws = _WS("browser")
    hub = _Hub(ws, snapshot={}, hijack_state_msg={"type": "hijack_state"}, session=_session())
    monkeypatch.setattr(browser_handlers, "_select_resumed_role", _role_stub("operator", False))
    monkeypatch.setattr(browser_handlers, "_try_reclaim_hijack", _reclaim_stub(False, False))
    await _resume(hub, ws, role="viewer")
    assert hub.role_calls == [(WID, ws, "operator")]
    assert hub.bind_calls == [(ws, NEW_TOKEN, True)]


async def test_an_unchanged_role_skips_set_browser_role_but_still_binds_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of mutmut_137: when the resumed role equals the
    original role, ``set_browser_role`` must not be called at all, while the
    token bind under the lock still happens unconditionally."""
    ws = _WS("browser")
    hub = _Hub(ws, snapshot={}, hijack_state_msg={"type": "hijack_state"}, session=_session())
    monkeypatch.setattr(browser_handlers, "_select_resumed_role", _role_stub("viewer", False))
    monkeypatch.setattr(browser_handlers, "_try_reclaim_hijack", _reclaim_stub(False, False))
    await _resume(hub, ws, role="viewer")
    assert hub.role_calls == []
    assert hub.bind_calls == [(ws, NEW_TOKEN, True)]


# ---------------------------------------------------------------------------
# The hello frame: every value and every ``.get(key, default)`` default
# ---------------------------------------------------------------------------


async def test_the_hello_frame_carries_every_snapshot_value_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_155 (``worker_id`` forced ``None``), 157/159/160 (a
    snapshot-derived value forced ``None`` instead of read), 162-165 (the
    literal ``hijack_control``/``hijack_step_supported``/``capabilities``/
    ``resume_supported`` fields forced ``None``), 170/172/173/175/176/177/178
    (the same kwargs dropped from the call entirely - ``make_hello_frame``
    takes ``**payload``, so a dropped kwarg is a key quietly missing from the
    wire frame, not a ``TypeError``), 181/183/185/186 (the ``is_hijacked``
    lookup keyed on ``None``/``False``/a mangled or upper-cased name, all of
    which miss this snapshot's real key), the matching 195/197/199/200 for
    ``worker_online``, 202/204/206/207 for ``input_mode``, and 210-220 (the
    ``hijack_control``/``hijack_step_supported``/capabilities/
    ``resume_supported`` literals mangled, upper-cased or flipped)."""
    ws = _WS("browser")
    hub = _Hub(
        ws,
        snapshot={
            "is_hijacked": True,
            "hijacked_by_me": True,
            "worker_online": True,
            "input_mode": "distinct-input-mode",
        },
        hijack_state_msg={"type": "hijack_state", "marker": "distinct-hijack-state"},
        session=_session(),
    )
    monkeypatch.setattr(browser_handlers, "_select_resumed_role", _role_stub("operator", True))
    monkeypatch.setattr(browser_handlers, "_try_reclaim_hijack", _reclaim_stub(False, False))
    await _resume(hub, ws, role="viewer")
    assert ws.sent[0] == _expected_hello()
    assert ws.sent[1] == encode_control_frame({"type": "hijack_state", "marker": "distinct-hijack-state"})


async def test_the_hello_frame_defaults_every_missing_snapshot_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_182/184/187 (the ``is_hijacked`` default forced to
    ``None``, dropped, or forced ``True``), the matching 189/191/194 for
    ``hijacked_by_me``, 196/198/201 for ``worker_online``, and
    203/205/208/209 for ``input_mode``: all twelve only diverge from the real
    ``.get(key, default)`` default when the snapshot dict has no such key at
    all, which is exactly this test's setup."""
    ws = _WS("browser")
    hub = _Hub(
        ws,
        snapshot={},
        hijack_state_msg={"type": "hijack_state", "marker": "distinct-hijack-state"},
        session=_session(),
    )
    monkeypatch.setattr(browser_handlers, "_select_resumed_role", _role_stub("operator", True))
    monkeypatch.setattr(browser_handlers, "_try_reclaim_hijack", _reclaim_stub(False, False))
    await _resume(hub, ws, role="viewer")
    assert ws.sent[0] == _expected_hello(
        hijacked=False,
        hijacked_by_me=False,
        worker_online=False,
        input_mode="hijack",
    )
    assert ws.sent[1] == encode_control_frame({"type": "hijack_state", "marker": "distinct-hijack-state"})


# ---------------------------------------------------------------------------
# The reclaimed-hijack broadcast/notify/event, and the closing log line
# ---------------------------------------------------------------------------


async def test_a_reclaimed_hijack_broadcasts_notifies_and_logs_the_exact_event(
    monkeypatch: pytest.MonkeyPatch, _log: _Logger
) -> None:
    """Kills mutmut_228 (``broadcast_hijack_state``'s ``worker_id`` forced
    ``None``), 229-231 (``notify_hijack_changed``'s three arguments each
    forced ``None``), 232/233 (``worker_id``/``enabled`` dropped from that
    call, which raises ``TypeError`` against the real required-argument
    signature before this fake's body runs), 234 (``owner`` dropped, which -
    unlike 232/233 - falls back to its own ``None`` default rather than
    raising, and is caught by the exact-tuple comparison), 235-237
    (``enabled``/``owner`` flipped or mangled), 238-240 (``append_event``'s
    three arguments each forced ``None``), 241/242 (``worker_id``/
    ``event_type`` dropped, which shifts every later positional argument),
    243 (``data`` dropped, defaulting to ``None`` rather than raising),
    244-249 (the event name and payload mangled or upper-cased), and
    251-253/258 (the closing ``logger.info`` call: each of its three
    positional arguments forced ``None``, and the format string mangled)."""
    ws = _WS("browser")
    hub = _Hub(ws, snapshot={}, hijack_state_msg={"type": "hijack_state"}, session=_session())
    monkeypatch.setattr(browser_handlers, "_select_resumed_role", _role_stub("operator", True))
    monkeypatch.setattr(browser_handlers, "_try_reclaim_hijack", _reclaim_stub(True, True))
    result = await _resume(hub, ws, role="viewer")
    assert result is True
    assert hub.broadcast_calls == [WID]
    assert hub.notify_calls == [(WID, True, "dashboard")]
    assert hub.event_calls == [(WID, "hijack_acquired", {"owner": "dashboard_resume"})]
    assert _log.info_calls == [("ws_browser_resumed worker_id=%s role=%s hijack=%s", WID, "operator", True)]


async def test_a_non_reclaimed_hijack_skips_the_broadcast_but_still_logs(
    monkeypatch: pytest.MonkeyPatch, _log: _Logger
) -> None:
    """Not required to kill any id in ``resume_B.txt`` (the ``if
    reclaimed_hijack:`` condition's own mutants, and ``hijack_state_msg_for``'s
    call arguments, belong to ids 221-227 - out of this file's assigned
    region), but pins the boundary the test above relies on: with no
    reclaim, none of the broadcast/notify/event calls fire, and the closing
    log line still reports the unreclaimed ``owned_hijack``."""
    ws = _WS("browser")
    hub = _Hub(ws, snapshot={}, hijack_state_msg={"type": "hijack_state"}, session=_session())
    monkeypatch.setattr(browser_handlers, "_select_resumed_role", _role_stub("operator", False))
    monkeypatch.setattr(browser_handlers, "_try_reclaim_hijack", _reclaim_stub(False, False))
    result = await _resume(hub, ws, role="viewer")
    assert result is False
    assert (hub.broadcast_calls, hub.notify_calls, hub.event_calls) == ([], [], [])
    assert _log.info_calls == [("ws_browser_resumed worker_id=%s role=%s hijack=%s", WID, "operator", False)]
