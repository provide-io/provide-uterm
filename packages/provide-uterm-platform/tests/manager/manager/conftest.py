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
def _mutation_blanket_process_mock(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
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
    # spawn_swarm's inter-group ``await asyncio.sleep(group_delay)`` is unreached by
    # the ORIGINAL code in single-group tests, but group-math mutants (e.g. ``+``->``-``
    # / ``<``->``<=``) DO reach it and would sleep the real group_delay (often the 60s
    # default) -> mutmut ``timeout`` instead of a clean kill. Replace process_impl's
    # asyncio.sleep with a zero-DELAY sleep that STILL YIELDS (``await real_sleep(0)``)
    # so task scheduling keeps working (a plain no-op mock breaks tests that sleep to
    # yield). EXCLUDE the monitor tests: they drive ``monitor_processes`` as a real
    # background task and rely on real elapsed time / multiple scheduler slices.
    if "monitor" not in request.node.nodeid.lower():
        _real_sleep = asyncio.sleep

        async def _zero_delay_sleep(_delay: float = 0, *args: object, **kwargs: object) -> None:
            await _real_sleep(0)

        monkeypatch.setattr("provide.uterm.manager.process_impl.asyncio.sleep", _zero_delay_sleep)
