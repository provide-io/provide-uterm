#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from provide.uterm.ansi import normalize_colors
from provide.uterm.screen import strip_ansi

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class PerfResult:
    normalize_colors_ns: float
    strip_ansi_ns: float


def _bench_ns_per_op(iterations: int, fn: Callable[[], object]) -> float:
    start = time.perf_counter_ns()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter_ns() - start
    return elapsed / iterations


def run_benchmarks(iterations: int) -> PerfResult:
    payload = "~1hello~0 {F196}world |04red|00"
    return PerfResult(
        normalize_colors_ns=_bench_ns_per_op(iterations, lambda: normalize_colors(payload)),
        strip_ansi_ns=_bench_ns_per_op(iterations, lambda: strip_ansi(payload)),
    )


def run_benchmarks_stable(iterations: int, runs: int) -> PerfResult:
    """Best-of-*runs*, because benchmark noise only ever runs one way.

    A contended runner makes an operation look slower; nothing makes it look
    faster than it is. So the minimum across samples is the closest estimate of
    the true per-op cost, and the same reason `timeit` documents min over mean.

    This used to take the median, and the gate called it with the default of
    one run -- a median of a single sample. That failed `quality (3.11)` on
    `strip_ansi_ns 9668.52 > 6750.00` while the other three cells of the same
    commit measured 2581, 1929 and 2691, and `normalize_colors` on the failing
    cell was in line with its siblings. One sample, one busy runner, one red
    gate on code nobody had touched.
    """
    samples: list[PerfResult] = [run_benchmarks(iterations) for _ in range(max(1, runs))]
    return PerfResult(
        normalize_colors_ns=min(sample.normalize_colors_ns for sample in samples),
        strip_ansi_ns=min(sample.strip_ansi_ns for sample in samples),
    )


def evaluate_thresholds(result: PerfResult, max_normalize_ns: float, max_strip_ns: float) -> list[str]:
    failures: list[str] = []
    if result.normalize_colors_ns > max_normalize_ns:
        failures.append(f"normalize_colors_ns {result.normalize_colors_ns:.2f} > {max_normalize_ns:.2f}")
    if result.strip_ansi_ns > max_strip_ns:
        failures.append(f"strip_ansi_ns {result.strip_ansi_ns:.2f} > {max_strip_ns:.2f}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Run terminal performance smoke benchmarks.")
    parser.add_argument("--iterations", type=int, default=250_000)
    # Several samples so one contended slice cannot decide the gate; the
    # aggregate is the minimum, see run_benchmarks_stable.
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--enforce", action="store_true", help="Fail if thresholds are exceeded.")
    parser.add_argument("--max-normalize-ns", type=float, default=6_000.0)
    parser.add_argument("--max-strip-ns", type=float, default=4_500.0)
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
            "normalize_colors_ns": round(result.normalize_colors_ns, 2),
            "strip_ansi_ns": round(result.strip_ansi_ns, 2),
            "enforced": args.enforce,
            "ci_detected": ci_detected,
            "threshold_multiplier": multiplier,
        }
    )

    failures = evaluate_thresholds(
        result,
        max_normalize_ns=args.max_normalize_ns * multiplier,
        max_strip_ns=args.max_strip_ns * multiplier,
    )
    if failures:
        print({"threshold_failures": failures})
        return 1 if args.enforce else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
