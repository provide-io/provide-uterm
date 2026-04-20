#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

from provide.terminal.ansi import colorize, strip_colors

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class PerfResult:
    colorize_ns: float
    strip_colors_ns: float


def _bench_ns_per_op(iterations: int, fn: Callable[[], object]) -> float:
    start = time.perf_counter_ns()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter_ns() - start
    return elapsed / iterations


def run_benchmarks(iterations: int) -> PerfResult:
    payload = "{-x}{+g}hello{-x} {-r}world{-x} {+c}ansi{-x}"
    return PerfResult(
        colorize_ns=_bench_ns_per_op(iterations, lambda: colorize(payload)),
        strip_colors_ns=_bench_ns_per_op(iterations, lambda: strip_colors(payload)),
    )


def run_benchmarks_stable(iterations: int, runs: int) -> PerfResult:
    samples: list[PerfResult] = [run_benchmarks(iterations) for _ in range(max(1, runs))]
    return PerfResult(
        colorize_ns=float(median(sample.colorize_ns for sample in samples)),
        strip_colors_ns=float(median(sample.strip_colors_ns for sample in samples)),
    )


def evaluate_thresholds(result: PerfResult, max_colorize_ns: float, max_strip_colors_ns: float) -> list[str]:
    failures: list[str] = []
    if result.colorize_ns > max_colorize_ns:
        failures.append(f"colorize_ns {result.colorize_ns:.2f} > {max_colorize_ns:.2f}")
    if result.strip_colors_ns > max_strip_colors_ns:
        failures.append(f"strip_colors_ns {result.strip_colors_ns:.2f} > {max_strip_colors_ns:.2f}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Run terminal performance smoke benchmarks.")
    parser.add_argument("--iterations", type=int, default=250_000)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--enforce", action="store_true", help="Fail if thresholds are exceeded.")
    parser.add_argument("--max-colorize-ns", type=float, default=4_500.0)
    parser.add_argument("--max-strip-colors-ns", type=float, default=3_500.0)
    parser.add_argument(
        "--ci-threshold-multiplier",
        type=float,
        default=1.5,
        help="Multiplier applied to thresholds when CI is detected.",
    )
    args = parser.parse_args()

    result = run_benchmarks_stable(args.iterations, args.runs)
    ci_detected = bool(os.getenv("CI"))
    multiplier = args.ci_threshold_multiplier if ci_detected else 1.0
    print(
        {
            "iterations": args.iterations,
            "runs": args.runs,
            "colorize_ns": round(result.colorize_ns, 2),
            "strip_colors_ns": round(result.strip_colors_ns, 2),
            "enforced": args.enforce,
            "ci_detected": ci_detected,
            "threshold_multiplier": multiplier,
        }
    )

    failures = evaluate_thresholds(
        result,
        max_colorize_ns=args.max_colorize_ns * multiplier,
        max_strip_colors_ns=args.max_strip_colors_ns * multiplier,
    )
    if failures:
        print({"threshold_failures": failures})
        return 1 if args.enforce else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
