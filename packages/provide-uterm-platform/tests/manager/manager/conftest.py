#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared fixtures for the manager test suite.

The autouse ``_mutation_blanket_process_mock`` fixture exists ONLY for the mutmut
mutation gate on ``manager/process_impl.py``. A mutant that defeats one of
``spawn_agent``'s early guards (``max_agents`` / ``Path.exists`` / policy gate)
falls through to the real ``asyncio.to_thread(self._spawn_process, ...)`` ->
``subprocess.Popen``; likewise a ``_stop_process_tree`` mutant can reach the real
``os.killpg``/``os.getpgid``. In a mutmut-forked worker that real child leaks into
mutmut's ``os.wait()`` reaper (KeyError -> whole-run crash) and a mutated PID could
signal an unrelated process group. The per-test mocks in the individual suites do
NOT cover guard-defeat mutants, so we blanket-mock the OS spawn/kill primitives for
the whole module — but ONLY during a mutation run, keyed on ``MUTANT_UNDER_TEST``
(mutmut sets it for every phase: 'stats'/'fail'/mutant-name; it is ABSENT under
normal pytest, so real-spawn tests like ``test_process.py`` are untouched).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mutation_blanket_process_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    if not os.environ.get("MUTANT_UNDER_TEST"):
        return

    def _fake_popen(*_args: object, **_kwargs: object) -> MagicMock:
        proc = MagicMock()
        proc.pid = 4242
        proc.wait.return_value = 0
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(os, "killpg", MagicMock(), raising=False)
    monkeypatch.setattr(os, "getpgid", MagicMock(return_value=4242), raising=False)
    fake_async_proc = MagicMock()
    fake_async_proc.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=fake_async_proc),
        raising=False,
    )
    # Replace process_impl's asyncio.sleep with a zero-DELAY sleep that STILL YIELDS
    # (``await real_sleep(0)``) for EVERY manager test during a mutation run. Two
    # timeout-class hazards this removes: (1) spawn_swarm's inter-group
    # ``asyncio.sleep(group_delay)`` — unreached by the original in single-group tests
    # but reached by group-math mutants, which would sleep the real (often 60s) delay
    # -> mutmut ``timeout``; (2) monitor_processes' loop runs with health_check_interval
    # 0, i.e. a ``sleep(0)`` busy-spin that its covering tests drive for a REAL 0.05s
    # window (``create_task`` + ``await asyncio.sleep(0.05)`` + ``cancel``) — cheap
    # locally but CPU-starved/slow on a loaded CI runner, so a borderline mutant flaked
    # to ``timeout`` there (passed locally). The zero-delay-but-yielding sleep makes the
    # monitor loop run a single deterministic iteration (its ``_handle_*`` mocks still
    # fire once) and the test return immediately, killing the flakiness. It is NOT a
    # no-op mock (that would break tests that sleep to yield, e.g. start_spawn_swarm).
    # ``min(delay, 0)`` clamps the real (positive) delay to 0 — instant — WHILE keeping
    # asyncio.sleep's real type semantics: a sleep-arg mutant like ``sleep(None)`` still
    # raises TypeError (min(None, 0) raises), so it stays killable rather than masked.
    _real_sleep = asyncio.sleep

    async def _zero_delay_sleep(_delay: float = 0, *args: object, **kwargs: object) -> None:
        await _real_sleep(min(_delay, 0))

    monkeypatch.setattr("provide.uterm.manager.process_impl.asyncio.sleep", _zero_delay_sleep)
