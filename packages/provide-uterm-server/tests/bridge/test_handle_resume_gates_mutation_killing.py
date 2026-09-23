#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for the first half of ``browser_handlers._handle_resume``.

Covers the ``resume_store is None`` guard, the malformed-token check, the
``wait_resume_token_ready`` detach wait and its timeout warning, the
non-destructive ``store.get`` lookup and wrong-worker rejection, the
``_on_resume`` callback gate, the ``_try_reclaim_hijack``/``_select_resumed_role``
call and the ``reclaim_required``/``open_mode``/``competing_owner``/
``allow_stale_owner_role_resume`` decision, the create/consume
``try/except BaseException`` block with its revoke + rollback cleanup, and the
``consumed is None`` branch. The success tail (role/state sync, the hello
frame, the reclaimed-hijack broadcast) is covered by a sibling suite.

Kill-suite only — behavioural coverage of resume lives in
``test_browser_handlers_resume.py`` and friends. Those suites drive a real
``TermHub`` end to end and never assert an argument to a collaborator call in
isolation, so an argument swapped for ``None``, a dropped keyword, or a
mangled log literal survives untouched. Every fake here is a strict
recorder: it never answers regardless of its arguments the way
``AsyncMock(return_value=...)`` does, so a substituted/dropped argument is
visible either as a raised assertion or as a mismatch against the recorded
call.

Measured against ``mutants/.../browser_handlers.py``'s
``x__handle_resume__mutmut_*`` family on 2026-09-23: this suite closes the 85
surviving mutants in its region (ids 10, 11, 14-24, 27-32, 38-45, 48-60, 68,
78, 79, 81, 82, 87-96, 98, 99, 101, 103, 104, 111-119, 121-136).

Documented equivalents:

- mutmut_5, 7: ``old_token = msg_b.get("token", "")``'s default changed to
  ``None`` (5) or omitted entirely, i.e. ``None`` (7). The default is only
  ever read when the ``"token"`` key is *absent*, and the very next line
  rejects any value that is not a truthy ``str`` as malformed. ``""`` and
  ``None`` are both non-truthy, so both defaults take the identical
  malformed-token branch with the identical logged reason; no input
  distinguishes them. (Contrast mutmut_10, which changes the default to the
  truthy ``"XXXX"`` — that one is very much alive and is killed below.)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.server.bridge.hub.ext import EVENT_RESUME_FAILED
from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import _handle_resume

WID = "resume-worker"
TOKEN = "old-token-abc"
TOKEN_NEW = "new-token-xyz"
TTL = 42.0
NOW = 1000.0
UUID = "11111111-2222-3333-4444-555555555555"


class _Logger:
    """Captures every ``warning``/``info`` call's exact args and kwargs."""

    def __init__(self) -> None:
        self.warning_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.info_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warning_calls.append((args, kwargs))

    def info(self, *args: Any, **kwargs: Any) -> None:
        self.info_calls.append((args, kwargs))


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch) -> _Logger:
    """Fix the clock/uuid (unused by this region, kept for parity with the
    module's other kill-suites) and capture the module logger."""
    monkeypatch.setattr(browser_handlers, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(browser_handlers, "uuid", SimpleNamespace(uuid4=lambda: UUID))
    log = _Logger()
    monkeypatch.setattr(browser_handlers, "logger", log)
    return log


class _Store:
    """Resume-token store double; every call is recorded for exact checks."""

    def __init__(
        self,
        *,
        get_result: Any = None,
        create_result: str | None = None,
        create_raises: BaseException | None = None,
        consume_result: Any = None,
        consume_raises: BaseException | None = None,
        revoke_raises: BaseException | None = None,
    ) -> None:
        self.get_calls: list[str] = []
        self.create_calls: list[tuple[str, str, float]] = []
        self.consume_calls: list[str] = []
        self.revoke_calls: list[str] = []
        self._get_result = get_result
        self._create_result = create_result
        self._create_raises = create_raises
        self._consume_result = consume_result
        self._consume_raises = consume_raises
        self._revoke_raises = revoke_raises

    async def get(self, token: str) -> Any:
        self.get_calls.append(token)
        return self._get_result

    async def create(self, worker_id: str, role: str, ttl_s: float) -> str | None:
        self.create_calls.append((worker_id, role, ttl_s))
        if self._create_raises is not None:
            raise self._create_raises
        return self._create_result

    async def consume(self, token: str) -> Any:
        self.consume_calls.append(token)
        if self._consume_raises is not None:
            raise self._consume_raises
        return self._consume_result

    async def revoke(self, token: str) -> None:
        self.revoke_calls.append(token)
        if self._revoke_raises is not None:
            raise self._revoke_raises


class _Hub:
    """Hub double exposing only the collaborators this region touches."""

    def __init__(
        self,
        *,
        store: _Store,
        wait_ready: bool = True,
        on_resume: Any = None,
        is_open_mode: bool = False,
        allow_stale: bool = False,
    ) -> None:
        self.resume_store = store
        self._resume_ttl_s = TTL
        self._on_resume = on_resume
        self.allow_stale_owner_role_resume = allow_stale
        self.wait_ready_calls: list[tuple[str, Any]] = []
        self.open_mode_calls: list[str] = []
        self._wait_ready = wait_ready
        self._is_open_mode = is_open_mode

    async def wait_resume_token_ready(self, token: str, ws: Any) -> bool:
        self.wait_ready_calls.append((token, ws))
        return self._wait_ready

    async def is_input_open_mode(self, worker_id: str) -> bool:
        self.open_mode_calls.append(worker_id)
        return self._is_open_mode


class _OnResume:
    """A scripted ``_on_resume`` callback that records its exact arguments."""

    def __init__(self, result: bool) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._result = result

    async def __call__(self, token: str, session: Any) -> bool:
        self.calls.append((token, session))
        return self._result


class _SelectRole:
    """A scripted ``_select_resumed_role`` that records its exact arguments."""

    def __init__(self, new_role: str, can_hijack: bool) -> None:
        self.calls: list[tuple[str, str]] = []
        self._result = (new_role, can_hijack)

    def __call__(self, role: str, session_role: str) -> tuple[str, bool]:
        self.calls.append((role, session_role))
        return self._result


class _TryReclaim:
    """A scripted ``_try_reclaim_hijack`` that records its exact arguments."""

    def __init__(self, owned: bool, reclaimed: bool, competing: Any) -> None:
        self.calls: list[tuple[Any, Any, Any, Any, Any]] = []
        self._result = (owned, reclaimed, competing)

    async def __call__(
        self, hub: Any, ws: Any, worker_id: str, session: Any, can_hijack: bool
    ) -> tuple[bool, bool, Any]:
        self.calls.append((hub, ws, worker_id, session, can_hijack))
        return self._result


class _Rollback:
    """A scripted ``_rollback_reclaimed_hijack`` that records its arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, str]] = []

    async def __call__(self, hub: Any, ws: Any, worker_id: str) -> None:
        self.calls.append((hub, ws, worker_id))


WS = SimpleNamespace(name="browser-ws")


def _session(*, worker_id: str = WID, role: str = "viewer", was_hijack_owner: bool = False) -> Any:
    return SimpleNamespace(worker_id=worker_id, role=role, was_hijack_owner=was_hijack_owner)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    select_role: tuple[str, bool],
    reclaim: tuple[bool, bool, Any],
) -> tuple[_SelectRole, _TryReclaim, _Rollback]:
    """Replace the three helpers this region calls out to (each has its own
    dedicated kill-suite) with strict, argument-recording fakes."""
    select = _SelectRole(*select_role)
    reclaim_fake = _TryReclaim(*reclaim)
    rollback = _Rollback()
    monkeypatch.setattr(browser_handlers, "_select_resumed_role", select)
    monkeypatch.setattr(browser_handlers, "_try_reclaim_hijack", reclaim_fake)
    monkeypatch.setattr(browser_handlers, "_rollback_reclaimed_hijack", rollback)
    return select, reclaim_fake, rollback


# ---------------------------------------------------------------------------
# The malformed-token guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("msg_b", [{}, {"token": ""}, {"token": 123}])
async def test_a_malformed_token_is_rejected_without_touching_the_store(
    msg_b: dict[str, Any], _pinned: _Logger
) -> None:
    """Kills mutmut_10 (a missing-key default of the truthy ``"XXXX"`` would
    slip past the guard instead of being rejected), 11 (``or`` -> ``and``: an
    empty string alone, or a non-str truthy token alone, must each still be
    rejected on their own) and pins the exact warning (14-21)."""
    store = _Store()
    hub = _Hub(store=store)
    result = await _handle_resume(hub, WS, WID, "viewer", msg_b, owned_hijack=True)
    assert result is True
    assert _pinned.warning_calls == [((EVENT_RESUME_FAILED,), {"worker_id": WID, "reason": "token_malformed"})]
    assert store.get_calls == []


# ---------------------------------------------------------------------------
# wait_resume_token_ready and its timeout warning
# ---------------------------------------------------------------------------


async def test_a_ready_detach_wait_skips_the_timeout_warning(_pinned: _Logger) -> None:
    """Kills mutmut_22 (the leading ``not`` dropped: a ready wait would then
    wrongly log the timeout warning) and pins the exact args to
    ``wait_resume_token_ready`` (23/24)."""
    store = _Store(get_result=None)
    hub = _Hub(store=store, wait_ready=True)
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert result is True
    assert hub.wait_ready_calls == [(TOKEN, WS)]
    assert store.get_calls == [TOKEN]
    assert _pinned.warning_calls == [((EVENT_RESUME_FAILED,), {"worker_id": WID, "reason": "token_invalid"})]


async def test_a_timed_out_detach_wait_logs_and_still_proceeds(_pinned: _Logger) -> None:
    """Kills mutmut_27-32: the exact timeout warning (message and
    worker_id), and confirms the resume still proceeds to the token lookup
    afterward rather than aborting."""
    store = _Store(get_result=None)
    hub = _Hub(store=store, wait_ready=False)
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert result is True
    assert hub.wait_ready_calls == [(TOKEN, WS)]
    assert _pinned.warning_calls == [
        (("ws_browser_resume_detach_wait_timeout worker_id=%s", WID), {}),
        ((EVENT_RESUME_FAILED,), {"worker_id": WID, "reason": "token_invalid"}),
    ]


# ---------------------------------------------------------------------------
# store.get + the wrong-worker rejection
# ---------------------------------------------------------------------------


async def test_a_resume_for_a_different_worker_is_rejected_without_consuming(_pinned: _Logger) -> None:
    """Kills mutmut_38-45: the exact ``token_invalid`` warning when the
    stored session belongs to a different worker. The token is looked up but
    never consumed, so the legitimate browser can still resume with it."""
    store = _Store(get_result=_session(worker_id="some-other-worker"))
    hub = _Hub(store=store)
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert result is True
    assert store.get_calls == [TOKEN]
    assert store.consume_calls == []
    assert _pinned.warning_calls == [((EVENT_RESUME_FAILED,), {"worker_id": WID, "reason": "token_invalid"})]


# ---------------------------------------------------------------------------
# The `_on_resume` callback gate
# ---------------------------------------------------------------------------


async def test_a_rejecting_resume_callback_is_honored_without_consuming(_pinned: _Logger) -> None:
    """Kills mutmut_49/50/51/52 (the exact args passed to ``_on_resume``) and
    53-60 (the exact ``callback_rejected`` warning). The token is not
    consumed."""
    session = _session(was_hijack_owner=False)
    on_resume = _OnResume(False)
    store = _Store(get_result=session)
    hub = _Hub(store=store, on_resume=on_resume)
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert result is True
    assert on_resume.calls == [(TOKEN, session)]
    assert store.consume_calls == []
    assert _pinned.warning_calls == [((EVENT_RESUME_FAILED,), {"worker_id": WID, "reason": "callback_rejected"})]


async def test_an_accepting_resume_callback_proceeds_past_the_gate(
    monkeypatch: pytest.MonkeyPatch, _pinned: _Logger
) -> None:
    """Kills mutmut_48 (the ``not`` dropped: an accepting callback would then
    be treated as a rejection and the resume would be wrongly aborted here)."""
    session = _session(was_hijack_owner=False)
    on_resume = _OnResume(True)
    store = _Store(get_result=session, create_result=TOKEN_NEW, consume_result=None)
    hub = _Hub(store=store, on_resume=on_resume)
    _install(monkeypatch, select_role=("viewer", False), reclaim=(False, False, False))
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    # Reached the "consumed is None" ending further down, proving the gate
    # let it through rather than aborting with `callback_rejected`.
    assert result is False
    assert on_resume.calls == [(TOKEN, session)]
    assert store.create_calls == [(WID, "viewer", TTL)]
    assert not any(kwargs.get("reason") == "callback_rejected" for _args, kwargs in _pinned.warning_calls)


# ---------------------------------------------------------------------------
# The reclaim decision: reclaim_required, and skipping the fail block
# ---------------------------------------------------------------------------


async def test_reclaim_required_and_satisfied_skips_the_fail_block(
    monkeypatch: pytest.MonkeyPatch, _pinned: _Logger
) -> None:
    """Kills mutmut_68 (``ws`` replaced by ``None`` in the reclaim attempt)
    and 79 (``and`` -> ``or`` in ``reclaim_required and not reclaimed_hijack``):
    with both true, the real check must skip the fail block; the ``or``
    mutant would wrongly enter it."""
    session = _session(was_hijack_owner=True)
    store = _Store(get_result=session, create_result=TOKEN_NEW, consume_result=None)
    hub = _Hub(store=store)
    _, reclaim_fake, _rollback = _install(monkeypatch, select_role=("admin", True), reclaim=(True, True, False))
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert reclaim_fake.calls == [(hub, WS, WID, session, True)]
    assert not any(
        kwargs.get("reason") in ("competing_owner", "reclaim_failed") for _args, kwargs in _pinned.warning_calls
    )
    assert result is False  # reached the "consumed is None" ending, not an early reclaim-fail return


async def test_reclaim_required_needs_both_owner_and_can_hijack(
    monkeypatch: pytest.MonkeyPatch, _pinned: _Logger
) -> None:
    """Kills mutmut_78 (``and`` -> ``or``): with ``was_hijack_owner=True`` but
    ``can_hijack=False``, ``reclaim_required`` must be False and the fail
    block must never run, regardless of the reclaim outcome."""
    session = _session(was_hijack_owner=True)
    store = _Store(get_result=session, create_result=TOKEN_NEW, consume_result=None)
    hub = _Hub(store=store)
    _install(monkeypatch, select_role=("viewer", False), reclaim=(False, False, False))
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert not any(
        kwargs.get("reason") in ("competing_owner", "reclaim_failed") for _args, kwargs in _pinned.warning_calls
    )
    assert result is False


# ---------------------------------------------------------------------------
# The reclaim decision: the fail block's open_mode check and reason literal
# ---------------------------------------------------------------------------


async def test_a_failed_reclaim_without_a_competitor_reports_reclaim_failed(
    monkeypatch: pytest.MonkeyPatch, _pinned: _Logger
) -> None:
    """Kills mutmut_81/82 (``open_mode`` short-circuited to ``None`` without
    ever calling the hub, or called with the wrong worker id), 87 (``reason``
    forced ``None``), 89 (the ternary forced to always answer
    ``"competing_owner"``), 92/93 (the ``"reclaim_failed"`` literal mangled)
    and 94-99 (the exact warning)."""
    session = _session(was_hijack_owner=True)
    store = _Store(get_result=session)
    hub = _Hub(store=store, is_open_mode=False, allow_stale=False)
    _install(monkeypatch, select_role=("admin", True), reclaim=(False, False, False))
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert result is False
    assert hub.open_mode_calls == [WID]
    assert _pinned.warning_calls == [((EVENT_RESUME_FAILED,), {"worker_id": WID, "reason": "reclaim_failed"})]
    assert store.create_calls == []
    assert store.consume_calls == []


async def test_a_failed_reclaim_with_a_competitor_reports_competing_owner(
    monkeypatch: pytest.MonkeyPatch, _pinned: _Logger
) -> None:
    """Kills mutmut_88 (the ternary forced to always answer
    ``"reclaim_failed"``), 90/91 (the ``"competing_owner"`` literal mangled)
    and re-pins 94-99 with the other reason value."""
    session = _session(was_hijack_owner=True)
    store = _Store(get_result=session)
    hub = _Hub(store=store, is_open_mode=False, allow_stale=False)
    _install(monkeypatch, select_role=("admin", True), reclaim=(False, False, True))
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert result is False
    assert _pinned.warning_calls == [((EVENT_RESUME_FAILED,), {"worker_id": WID, "reason": "competing_owner"})]
    assert store.create_calls == []
    assert store.consume_calls == []


# ---------------------------------------------------------------------------
# create/consume under `try`, and the `except BaseException` cleanup
# ---------------------------------------------------------------------------


class _CreateError(RuntimeError):
    """Distinct exception type so a test can pin exactly what propagates."""


class _ConsumeError(RuntimeError):
    """Distinct exception type so a test can pin exactly what propagates."""


class _RevokeError(RuntimeError):
    """Distinct exception type; must never be what a caller sees."""


async def test_a_create_failure_leaves_the_replacement_token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_101 (``new_token``'s initial value changed from ``None``
    to the truthy ``""``): when ``store.create`` itself raises before
    assigning, the cleanup's ``if new_token is not None`` must see the
    pre-``try`` value and skip revoking a token that was never issued."""
    session = _session(was_hijack_owner=False)
    store = _Store(get_result=session, create_raises=_CreateError("create failed"))
    hub = _Hub(store=store)
    _, _, rollback = _install(monkeypatch, select_role=("viewer", False), reclaim=(False, False, False))
    with pytest.raises(_CreateError):
        await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert store.revoke_calls == []
    assert rollback.calls == []


async def test_a_consume_failure_after_create_revokes_and_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_103/104 (the exact args to ``store.create``), 111 (the
    ``is not None`` check inverted, which would then skip revoking a token
    that genuinely was issued) and 113-119 (the exact args to ``store.revoke``
    and ``_rollback_reclaimed_hijack``)."""
    session = _session(was_hijack_owner=True)
    store = _Store(get_result=session, create_result=TOKEN_NEW, consume_raises=_ConsumeError("consume failed"))
    hub = _Hub(store=store)
    _, _, rollback = _install(monkeypatch, select_role=("admin", True), reclaim=(True, True, False))
    with pytest.raises(_ConsumeError):
        await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert store.create_calls == [(WID, "admin", TTL)]
    assert store.revoke_calls == [TOKEN_NEW]
    assert rollback.calls == [(hub, WS, WID)]


async def test_a_revoke_failure_during_cleanup_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutmut_112 (``suppress(Exception)`` -> ``suppress(None)``): if
    the cleanup's own ``store.revoke`` also fails, that failure must not
    replace the original ``store.consume`` failure that triggered the
    cleanup. ``suppress(None)`` cannot swallow anything (``issubclass(exc,
    (None,))`` itself raises), so the mutant lets a different exception
    propagate."""
    session = _session(was_hijack_owner=False)
    store = _Store(
        get_result=session,
        create_result=TOKEN_NEW,
        consume_raises=_ConsumeError("consume failed"),
        revoke_raises=_RevokeError("revoke failed"),
    )
    hub = _Hub(store=store)
    _install(monkeypatch, select_role=("viewer", False), reclaim=(False, False, False))
    with pytest.raises(_ConsumeError):
        await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)


# ---------------------------------------------------------------------------
# The `consumed is None` branch (no exception, just a losing/expired token)
# ---------------------------------------------------------------------------


async def test_an_unconsumed_token_revokes_the_replacement_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, _pinned: _Logger
) -> None:
    """Kills mutmut_121 (``store.revoke``'s arg replaced by ``None``),
    122-127 (the exact args to ``_rollback_reclaimed_hijack``), 128-135 (the
    exact ``token_invalid`` warning) and 136 (``return False`` -> ``return
    True``)."""
    session = _session(was_hijack_owner=True)
    store = _Store(get_result=session, create_result=TOKEN_NEW, consume_result=None)
    hub = _Hub(store=store)
    _, _, rollback = _install(monkeypatch, select_role=("admin", True), reclaim=(True, True, False))
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert result is False
    assert store.consume_calls == [TOKEN]
    assert store.revoke_calls == [TOKEN_NEW]
    assert rollback.calls == [(hub, WS, WID)]
    assert _pinned.warning_calls == [((EVENT_RESUME_FAILED,), {"worker_id": WID, "reason": "token_invalid"})]


async def test_an_unconsumed_token_without_a_reclaim_skips_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the ``reclaimed_hijack`` guard on this branch: with
    no reclaim in play, only the revoke happens, never the rollback."""
    session = _session(was_hijack_owner=False)
    store = _Store(get_result=session, create_result=TOKEN_NEW, consume_result=None)
    hub = _Hub(store=store)
    _, _, rollback = _install(monkeypatch, select_role=("viewer", False), reclaim=(False, False, False))
    result = await _handle_resume(hub, WS, WID, "viewer", {"token": TOKEN}, owned_hijack=True)
    assert result is False
    assert store.revoke_calls == [TOKEN_NEW]
    assert rollback.calls == []
