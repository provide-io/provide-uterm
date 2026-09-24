#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``websockets_browser.resume_worker_on_disconnect``.

Kill-suite only — behavioural coverage of the browser disconnect flow lives
in ``test_browser_handlers_coverage.py``/``test_snapshot_redaction.py`` and
friends, which exercise ``ws_browser_term`` end to end against a real hub and
a real event loop. Those tests never inspect the *exact* compensating
``resume`` payload this function schedules, never check which two
``add_done_callback`` handlers get wired onto the fire-and-forget task, and
never drive the failure-log branch's guard or its log call's exact arguments.
That left all 36 of its mutants (string/dict-literal swaps in the resume
payload, ``None`` substitutions for ``worker_id``/the payload/the two
callbacks, and every rewrite of the ``not t.cancelled() and t.exception() is
not None`` guard plus the ``logger.warning`` call it guards) in the
``SURVIVED`` state.

``resume_worker_on_disconnect`` is a **sync** function that calls
``asyncio.create_task`` and wires two ``add_done_callback`` handlers onto the
result. Every test here replaces ``asyncio.create_task`` with a strict fake
(``_FakeTask``) that captures the wrapped coroutine and every registered
callback instead of a real ``asyncio.Task``. This lets the tests:

- run the coroutine synchronously (``coro.send(None)`` — it never awaits
  anything) to assert the *exact* ``hub.send_worker_if_unowned`` call, and
- invoke the captured warning-callback directly against a controlled
  ``cancelled()``/``exception()`` outcome, to assert the exact
  ``logger.warning`` call (or its absence) without needing a real event loop
  or a real task completion.

No documented equivalents: all 36 mutants are killed below.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from provide.uterm.server.bridge.routes import websockets_browser
from provide.uterm.server.bridge.routes.websockets_browser import resume_worker_on_disconnect

WID = "resume-worker"
NOW = 4242.0
EXPECTED_PAYLOAD = {
    "type": "control",
    "action": "resume",
    "owner": "dashboard",
    "lease_s": 0,
    "ts": NOW,
}
EXPECTED_WARNING_MESSAGE = "ws_disconnect_resume_failed worker_id=%s error=%s"


class _Hub:
    """Mirrors the two hub surfaces this function touches directly.

    ``send_worker_if_unowned`` asserts every argument (mirroring the real
    signature in ``hub/lease.py``/``hub/core_impl.py``) rather than answering
    unconditionally like an ``AsyncMock`` would — that unconditional-answer
    behaviour is exactly what would hide the ``worker_id``/payload
    ``None``-substitution mutants (ids 3, 4). ``_background_tasks`` is a real
    ``set()``, matching the hub's actual attribute type, so containment and
    ``.discard`` identity can be checked directly (ids 24, 25).
    """

    def __init__(self) -> None:
        self._background_tasks: set[Any] = set()
        self.send_calls: list[tuple[str, dict[str, Any]]] = []

    async def send_worker_if_unowned(self, worker_id: str, msg: dict[str, Any]) -> bool:
        assert worker_id == WID, f"send_worker_if_unowned got worker_id={worker_id!r}"
        assert isinstance(msg, dict), f"send_worker_if_unowned got msg={msg!r}"
        self.send_calls.append((worker_id, msg))
        return True


class _FakeTask:
    """Stands in for the ``asyncio.Task`` that ``asyncio.create_task`` returns.

    Captures every ``add_done_callback`` registration instead of actually
    scheduling anything, so both callbacks the function wires up can be
    inspected (for identity/substitution mutants) and invoked directly
    against a controlled ``cancelled()``/``exception()`` outcome (for the
    warning-guard mutants) — without needing a real event loop.
    """

    def __init__(self, coro: Any) -> None:
        self._coro = coro
        self.callbacks: list[Any] = []
        self.cancelled_value = False
        self.exception_value: BaseException | None = None

    def run(self) -> None:
        """Drive the wrapped coroutine to completion; it never awaits."""
        try:
            self._coro.send(None)
        except StopIteration:
            pass

    def add_done_callback(self, cb: Any) -> None:
        self.callbacks.append(cb)

    def cancelled(self) -> bool:
        return self.cancelled_value

    def exception(self) -> BaseException | None:
        return self.exception_value


class _FakeLogger:
    """Records every ``warning(...)`` call's exact args/kwargs."""

    def __init__(self) -> None:
        self.warnings: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warnings.append((args, kwargs))


def _install_fake_create_task(monkeypatch: Any) -> list[_FakeTask]:
    created: list[_FakeTask] = []

    def _fake_create_task(coro: Any) -> _FakeTask:
        task = _FakeTask(coro)
        created.append(task)
        return task

    monkeypatch.setattr(websockets_browser.asyncio, "create_task", _fake_create_task)
    return created


# ---------------------------------------------------------------------------
# The exact resume payload, and the background-task bookkeeping around it.
# ---------------------------------------------------------------------------


def test_schedules_the_exact_resume_payload_and_tracks_the_task_until_done(monkeypatch: Any) -> None:
    """Kills mutmut_3/4 (``worker_id``/the payload dict replaced with ``None``
    in the ``send_worker_if_unowned`` call — caught by the fake's argument
    asserts, which leave ``send_calls`` empty instead of holding the expected
    call), mutmut_7-23 (every key/value literal in the resume payload: each
    key's or value's case, and ``lease_s: 0`` -> ``1`` — any one of them
    makes the recorded call compare unequal to the exact expected payload),
    mutmut_24 (``hub._background_tasks.add(_resume_task)`` -> ``.add(None)``
    — the created task itself would then not be a member of the set), and
    mutmut_25 (``_resume_task.add_done_callback(hub._background_tasks.discard)``
    -> ``.add_done_callback(None)`` — the first registered callback would not
    equal the set's bound ``discard`` method).
    """
    monkeypatch.setattr(websockets_browser, "time", SimpleNamespace(time=lambda: NOW))
    created = _install_fake_create_task(monkeypatch)
    hub = _Hub()

    result = resume_worker_on_disconnect(hub, WID)  # type: ignore[arg-type]

    assert result is None
    assert len(created) == 1
    task = created[0]

    # The task itself (not None) was added to the bookkeeping set.
    assert task in hub._background_tasks
    assert len(task.callbacks) == 2
    # The first callback is exactly the set's own discard method.
    assert task.callbacks[0] == hub._background_tasks.discard

    # Drive the wrapped coroutine to see the exact call it makes.
    task.run()
    assert hub.send_calls == [(WID, EXPECTED_PAYLOAD)]

    # Invoking the real discard callback removes the task, as production does
    # once the real task completes.
    task.callbacks[0](task)
    assert hub._background_tasks == set()

    # On a clean (non-cancelled, no-exception) completion the warning
    # callback must stay silent.
    fake_logger = _FakeLogger()
    monkeypatch.setattr(websockets_browser, "logger", fake_logger)
    task.callbacks[1](task)
    assert fake_logger.warnings == []


# ---------------------------------------------------------------------------
# The failure-log branch: guard logic and exact logger.warning call.
# ---------------------------------------------------------------------------


def test_warns_with_the_exact_message_when_not_cancelled_and_an_exception_occurred(monkeypatch: Any) -> None:
    """Kills mutmut_26 (the whole second ``add_done_callback`` argument
    replaced with ``None`` — invoking it here would raise ``TypeError``
    instead of the expected single warning call), mutmut_27 (the lambda body
    replaced with ``lambda t: None`` — it would silently record zero warning
    calls instead of one), mutmut_28 (``(not t.cancelled() and t.exception()
    is not None) and False`` — always False, so it would never warn here),
    mutmut_39 (the leading ``not`` dropped, i.e. ``t.cancelled() and
    t.exception() is not None`` — with ``cancelled=False`` this is False, so
    it would never warn here), mutmut_40 (``is not None`` -> ``is None`` —
    with a real exception this flips to False, so it would never warn here),
    and mutmut_30-37 (every mutation of the ``logger.warning`` call itself:
    the format string's case/content, each positional argument replaced with
    ``None`` or dropped, and the two-argument-only rewrites — any of these
    makes the recorded call's args tuple compare unequal to the exact
    expected 3-tuple).
    """
    created = _install_fake_create_task(monkeypatch)
    hub = _Hub()
    resume_worker_on_disconnect(hub, WID)  # type: ignore[arg-type]
    task = created[0]
    task._coro.close()  # unused here; avoids a "never awaited" ResourceWarning

    fake_logger = _FakeLogger()
    monkeypatch.setattr(websockets_browser, "logger", fake_logger)

    boom = RuntimeError("worker send exploded")
    task.cancelled_value = False
    task.exception_value = boom

    task.callbacks[1](task)

    assert fake_logger.warnings == [((EXPECTED_WARNING_MESSAGE, WID, boom), {})]


def test_does_not_warn_when_the_task_was_cancelled_with_no_exception(monkeypatch: Any) -> None:
    """Kills mutmut_29 (``(not t.cancelled() and t.exception() is not None)
    or True`` — always True, so it would warn here even though the real
    guard (``not cancelled()`` is False since ``cancelled=True``) must stay
    silent)."""
    created = _install_fake_create_task(monkeypatch)
    hub = _Hub()
    resume_worker_on_disconnect(hub, WID)  # type: ignore[arg-type]
    task = created[0]
    task._coro.close()  # unused here; avoids a "never awaited" ResourceWarning

    fake_logger = _FakeLogger()
    monkeypatch.setattr(websockets_browser, "logger", fake_logger)

    task.cancelled_value = True
    task.exception_value = None

    task.callbacks[1](task)

    assert fake_logger.warnings == []


def test_does_not_warn_when_a_cancelled_task_also_carries_an_exception(monkeypatch: Any) -> None:
    """Kills mutmut_38 (the guard's ``and`` swapped for ``or``: ``not
    t.cancelled() or t.exception() is not None`` — with ``cancelled=True``
    (so ``not cancelled()`` is False) and a real exception, the ``or`` makes
    this True and it would warn, even though the real ``and`` guard (False
    and True) must stay silent because the task was cancelled)."""
    created = _install_fake_create_task(monkeypatch)
    hub = _Hub()
    resume_worker_on_disconnect(hub, WID)  # type: ignore[arg-type]
    task = created[0]
    task._coro.close()  # unused here; avoids a "never awaited" ResourceWarning

    fake_logger = _FakeLogger()
    monkeypatch.setattr(websockets_browser, "logger", fake_logger)

    task.cancelled_value = True
    task.exception_value = RuntimeError("worker send exploded")

    task.callbacks[1](task)

    assert fake_logger.warnings == []
