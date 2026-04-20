#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import argparse
import os

import pytest


def _half_cpu_count() -> int:
    count = os.cpu_count() or 1
    return max(1, count // 2)


def _effective_workers(max_workers: int | None, passthrough: list[str]) -> int:
    half_cpus = _half_cpu_count()
    if max_workers is not None:
        return min(max(1, max_workers), half_cpus)
    if "--no-cov" in passthrough:
        return half_cpus
    return 1


def _build_pytest_cmd(max_workers: int | None, passthrough: list[str]) -> list[str]:
    workers = _effective_workers(max_workers, passthrough)
    return ["uv", "run", "pytest", "-n", str(workers), *passthrough]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pytest with worker count capped at half CPU count.")
    parser.add_argument("--max-workers", type=int, default=None, help="Requested worker count before half-CPU cap.")
    args, passthrough = parser.parse_known_args()
    cmd = _build_pytest_cmd(args.max_workers, passthrough)
    pytest_args = cmd[3:]
    print("+", " ".join(["pytest", *pytest_args]))
    return int(pytest.main(pytest_args))


if __name__ == "__main__":
    raise SystemExit(main())
