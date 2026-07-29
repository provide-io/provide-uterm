#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the manager's process supervision.

Two decisions here, both of which decide what a compromised worker can do.

* **A worker never inherits the operator token.** The manager holds a token
  that can spawn and kill the whole fleet; a worker only needs to report about
  itself. When a fleet secret is configured, the worker's environment gets a
  token derived from that secret and bound to its own agent id, and the raw
  secret is stripped so it never reaches the child — a worker then holds
  something it cannot use to impersonate anybody else.
* **Every agent gets an identifier nobody else has.** The allocator scans what
  is already known, continues past the highest, and never hands out a name
  already in use — a repeated id would put two agents' reports in one place.

# uv-package: provide-uterm-platform

Usage (from the repository root)::

    uv run --package provide-uterm-platform python \\
        packages/provide-uterm-ts/testdata/gen_managerprocess_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.manager.auth import derive_agent_token
from provide.uterm.manager.process_impl import AgentProcessManager

OUT = Path(__file__).resolve().parent / "managerprocess_golden.json"

IDS: list[str] = [
    "agent_000",
    "agent_001",
    "agent_042",
    "agent_999",
    "agent_1000",
    "agent_0001",
    "agent_",
    "agent",
    "agent_abc",
    "agent_-1",
    "agent_ 1",
    " agent_001 ",
    "AGENT_001",
    "worker_001",
    "",
    "agent_001x",
    "xagent_001",
]

# known agents, known processes -> the id allocated next
ALLOCATIONS: list[tuple[str, list[str], list[str]]] = [
    ("nothing known", [], []),
    ("one agent", ["agent_000"], []),
    ("a gap in the middle", ["agent_000", "agent_002"], []),
    ("agents and processes", ["agent_000"], ["agent_001"]),
    ("the same id in both", ["agent_000"], ["agent_000"]),
    ("a high id", ["agent_120"], []),
    ("ids nobody parses", ["worker-a", "something"], []),
    ("a mixture", ["agent_003", "worker-a"], ["agent_007"]),
    ("an id past three digits", ["agent_1000"], []),
]


class FakeConfig:
    # The names of the variables, not the tokens in them.
    auth_token_env_var = "UTERM_MANAGER_API_TOKEN"  # noqa: S105
    auth_worker_token_env_var = "UTERM_MANAGER_WORKER_TOKEN"  # noqa: S105


class FakeManager:
    def __init__(self, agents: list[str], processes: list[str]) -> None:
        self.agents = dict.fromkeys(agents)
        self.processes = dict.fromkeys(processes)
        self.config = FakeConfig()


def _allocator(agents: list[str], processes: list[str]) -> AgentProcessManager:
    return AgentProcessManager(FakeManager(agents, processes))


def _scoped(name: str, env: dict[str, str], agent_id: str, worker_token: str) -> dict[str, Any]:
    import os

    manager = _allocator([], [])
    previous = os.environ.get(FakeConfig.auth_worker_token_env_var)
    if worker_token:
        os.environ[FakeConfig.auth_worker_token_env_var] = worker_token
    else:
        os.environ.pop(FakeConfig.auth_worker_token_env_var, None)
    scoped = dict(env)
    try:
        manager._scope_worker_tokens(scoped, agent_id)
    finally:
        if previous is None:
            os.environ.pop(FakeConfig.auth_worker_token_env_var, None)
        else:
            os.environ[FakeConfig.auth_worker_token_env_var] = previous
    return {"name": name, "env": env, "agent_id": agent_id, "worker_token": worker_token, "scoped": scoped}


def main() -> None:
    operator = FakeConfig.auth_token_env_var
    worker_var = FakeConfig.auth_worker_token_env_var

    corpus = {
        "operator_var": operator,
        "worker_var": worker_var,
        "parsed": [{"id": value, "index": AgentProcessManager._parse_agent_index(value)} for value in IDS],
        "allocations": [
            {
                "name": name,
                "agents": agents,
                "processes": processes,
                "allocated": [_allocator(agents, processes).allocate_agent_id()],
                "next_index": _allocator(agents, processes).sync_next_agent_index(),
            }
            for name, agents, processes in ALLOCATIONS
        ],
        "scoped": [
            _scoped(
                "a fleet secret configured",
                {operator: "omnipotent", worker_var: "fleet-secret", "PATH": "/usr/bin"},
                "agent_001",
                "fleet-secret",
            ),
            _scoped(
                "no fleet secret configured",
                {operator: "omnipotent", "PATH": "/usr/bin"},
                "agent_001",
                "",
            ),
            _scoped(
                "the raw secret in the environment but none configured",
                {operator: "omnipotent", worker_var: "left-over"},
                "agent_001",
                "",
            ),
            _scoped(
                "another agent",
                {operator: "omnipotent", worker_var: "fleet-secret"},
                "agent_002",
                "fleet-secret",
            ),
            _scoped(
                "a secret that is only spaces",
                {operator: "omnipotent", worker_var: "   "},
                "agent_001",
                "   ",
            ),
            _scoped("nothing in the environment at all", {}, "agent_001", "fleet-secret"),
        ],
        "derived": {
            "agent_001": derive_agent_token("fleet-secret", "agent_001"),
            "agent_002": derive_agent_token("fleet-secret", "agent_002"),
        },
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['allocations'])} allocations)")


if __name__ == "__main__":
    main()
