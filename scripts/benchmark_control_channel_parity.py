#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Run control-frame decode benchmarks across available backends.

This command focuses on a single parity-relevant hotspot:
- Python baseline/optimized stream decoder comparison (ControlFrameDecoder)
- C#/Go/TypeScript equivalent control-frame decoder throughput
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CSPROJECT = (
    ROOT
    / "packages"
    / "provide-uterm-csharp"
    / "benchmarks"
    / "ControlChannelDecoderBench"
    / "ControlChannelDecoderBench.csproj"
)
PY_BENCH = ROOT / "scripts" / "benchmark_control_channel_memoryview.py"
GO_ROOT = ROOT / "packages" / "provide-uterm-go"
TS_PACKAGE = ROOT / "packages" / "provide-uterm-ts"


def _run(
    cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _parse_python_benchmark(out: str) -> dict[str, Any]:
    payload = next((line for line in out.strip().splitlines()[::-1] if line.startswith("{") and line.endswith("}")), "")
    if payload:
        data = json.loads(payload)
        if isinstance(data, dict):
            return {
                "backend": data["backend"],
                "generated_bytes": int(data["generated_bytes"]),
                "frame_count": int(data["frame_count"]),
                "chunk_size": int(data["chunk_size"]),
                "median_seconds": float(data["median_seconds"]),
                "mean_seconds": float(data.get("mean_seconds", 0)),
                "min_seconds": float(data.get("min_seconds", 0)),
                "events": int(data["events"]),
                "mib_per_s": float(data["mib_per_s"]),
                "speedup_vs_before": float(data["speedup_vs_before"]),
                "before_label": str(data["before_label"]),
                "after_label": str(data["after_label"]),
                "baseline_seconds": float(data["baseline_seconds"]),
                "before_mib_per_s": float(data["before_mib_per_s"]),
            }

    size_m = re.search(r"Generated stream:\s+(\d+) bytes,\s+(\d+) frames,\s+chunk size\s+(\d+)", out)
    if not size_m:
        raise ValueError("python benchmark output did not include stream summary")

    after_m = re.search(
        r"After\s+\((?P<label>[^)]+)\):\s+(?P<seconds>[0-9]+\.[0-9]+)s,\s+[0-9]+\.[0-9]+s median,\s+(?P<mib>[0-9]+\.[0-9]+) MiB/s",
        out,
    )
    if not after_m:
        raise ValueError("python benchmark output did not include 'After' result line")

    baseline_m = re.search(r"Baseline\s+\((?P<label>[^)]+)\):\s+(?P<seconds>[0-9]+\.[0-9]+)s", out)
    speedup_m = re.search(r"Median speedup:\s+(?P<ratio>[0-9.]+)x", out)
    events_m = re.search(r"Events emitted:\s+(\d+)", out)

    mib = float(after_m.group("mib"))
    if mib <= 0.0:
        raise ValueError("python benchmark output did not include MiB/s line")

    return {
        "backend": "python",
        "generated_bytes": int(size_m.group(1)),
        "frame_count": int(size_m.group(2)),
        "chunk_size": int(size_m.group(3)),
        "after_label": after_m.group("label"),
        "median_seconds": float(after_m.group("seconds")),
        "before_label": baseline_m.group("label") if baseline_m else "baseline",
        "before_seconds": float(baseline_m.group("seconds")) if baseline_m else 0.0,
        "speedup_vs_before": float(speedup_m.group("ratio")) if speedup_m else 0.0,
        "events": int(events_m.group(1)) if events_m else 0,
        "mib_per_s": mib,
        "before_mib_per_s": mib * float(speedup_m.group("ratio")) if speedup_m else mib,
    }


def _parse_csharp_benchmark(out: str) -> dict[str, Any]:
    payload = next((line for line in out.strip().splitlines()[::-1] if line.startswith("{") and line.endswith("}")), "")
    if not payload:
        raise ValueError("csharp benchmark output did not include JSON summary")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("csharp benchmark JSON summary was not an object")
    return data


def _parse_go_benchmark(out: str) -> dict[str, Any]:
    payload = next((line for line in out.strip().splitlines()[::-1] if line.startswith("{") and line.endswith("}")), "")
    if not payload:
        raise ValueError("go benchmark output did not include JSON summary")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("go benchmark JSON summary was not an object")
    return data


def _parse_typescript_benchmark(out: str) -> dict[str, Any]:
    payload = next((line for line in out.strip().splitlines()[::-1] if line.startswith("{") and line.endswith("}")), "")
    if not payload:
        raise ValueError("typescript benchmark output did not include JSON summary")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("typescript benchmark JSON summary was not an object")
    return data


def run_python(args: argparse.Namespace) -> dict[str, Any]:
    result = _run(
        [
            "uv",
            "run",
            "python",
            str(PY_BENCH),
            "--baseline-revision",
            args.baseline_revision,
            "--frame-count",
            str(args.frame_count),
            "--passes",
            str(args.passes),
            "--chunk-size",
            str(args.chunk_size),
            "--data-size",
            str(args.data_size),
            "--control-size",
            str(args.control_size),
            "--control-ratio",
            str(args.control_ratio),
        ],
    )
    if result.returncode != 0:
        raise RuntimeError(f"python benchmark failed:\n{result.stdout}\n{result.stderr}")
    return _parse_python_benchmark(result.stdout)


def run_csharp(args: argparse.Namespace) -> dict[str, Any]:
    result = _run(
        [
            "dotnet",
            "run",
            "--project",
            str(CSPROJECT),
            "--configuration",
            args.dotnet_configuration,
            "--",
            "--frame-count",
            str(args.frame_count),
            "--control-ratio",
            str(args.control_ratio),
            "--chunk-size",
            str(args.chunk_size),
            "--data-size",
            str(args.data_size),
            "--control-size",
            str(args.control_size),
            "--passes",
            str(args.passes),
            "--seed",
            str(args.seed),
        ],
        cwd=ROOT / "packages" / "provide-uterm-csharp",
    )
    if result.returncode != 0:
        raise RuntimeError(f"csharp benchmark failed:\n{result.stdout}\n{result.stderr}")
    return _parse_csharp_benchmark(result.stdout)


def run_go(args: argparse.Namespace) -> dict[str, Any]:
    env = os.environ.copy()
    env["GOWORK"] = "off"
    result = _run(
        [
            "go",
            "run",
            "./benchmarks/controlchannel/main.go",
            "--frame-count",
            str(args.frame_count),
            "--control-ratio",
            str(args.control_ratio),
            "--chunk-size",
            str(args.chunk_size),
            "--data-size",
            str(args.data_size),
            "--control-size",
            str(args.control_size),
            "--passes",
            str(args.passes),
            "--seed",
            str(args.seed),
        ],
        cwd=GO_ROOT,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"go benchmark failed:\n{result.stdout}\n{result.stderr}")
    return _parse_go_benchmark(result.stdout)


def run_typescript(args: argparse.Namespace) -> dict[str, Any]:
    result = _run(
        [
            "npm",
            "--prefix",
            str(TS_PACKAGE),
            "run",
            "benchmark:control-channel",
            "--",
            "--frame-count",
            str(args.frame_count),
            "--control-ratio",
            str(args.control_ratio),
            "--chunk-size",
            str(args.chunk_size),
            "--data-size",
            str(args.data_size),
            "--control-size",
            str(args.control_size),
            "--passes",
            str(args.passes),
            "--seed",
            str(args.seed),
        ],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"typescript benchmark failed:\n{result.stdout}\n{result.stderr}")
    return _parse_typescript_benchmark(result.stdout)


def _print_result_table(results: list[dict[str, Any]]) -> None:
    rows: list[tuple[str, str, float, float, int]] = []
    for item in results:
        if item["backend"] == "python":
            rows.append(
                (
                    item["backend"],
                    item["before_label"],
                    item["baseline_seconds"],
                    item["before_mib_per_s"],
                    item["events"],
                )
            )
            rows.append(
                (item["backend"], item["after_label"], item["median_seconds"], item["mib_per_s"], item["events"])
            )
            continue
        rows.append((item["backend"], "single", item["median_seconds"], item["mib_per_s"], item["events"]))

    print("Backend       Variant      Median (s)   MiB/s     Events")
    print("--------      --------     ----------   -------   ------")
    for backend, variant, median_s, mib, events in rows:
        print(f"{backend:<12} {variant:<11} {median_s:<11.4f} {mib:<9.2f} {events:<8d}")

    py = next((item for item in results if item["backend"] == "python"), None)
    if py:
        print(f"\nPython before/after speedup: {py['speedup_vs_before']:.2f}x")
        after = float(py["median_seconds"])
        go = next((item for item in results if item["backend"] == "go"), None)
        ts = next((item for item in results if item["backend"] == "typescript"), None)
        cs = next((item for item in results if item["backend"] == "csharp"), None)
        for label, result in [("Go", go), ("TypeScript", ts), ("C#", cs)]:
            if result is None:
                continue
            ratio = after / result["median_seconds"] if result["median_seconds"] else 0.0
            print(f"Python/ {label} median ratio: {ratio:.2f}x")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark control-frame decode across backends.")
    parser.add_argument(
        "--backends",
        nargs="*",
        default=["python", "csharp", "go", "typescript"],
        choices=["python", "csharp", "go", "typescript"],
    )
    parser.add_argument("--baseline-revision", default="HEAD~1", help="Python baseline git revision.")
    parser.add_argument("--frame-count", type=int, default=200000, help="Number of frames to synthesize.")
    parser.add_argument("--passes", type=int, default=5, help="Benchmark passes per variant.")
    parser.add_argument("--chunk-size", type=int, default=4096, help="Chunk size for decoder input.")
    parser.add_argument("--data-size", type=int, default=256, help="Terminal-data segment size.")
    parser.add_argument("--control-size", type=int, default=128, help="Control payload size.")
    parser.add_argument("--control-ratio", type=float, default=0.25, help="Ratio of control frames in stream.")
    parser.add_argument("--seed", type=int, default=1337, help="Deterministic stream seed.")
    parser.add_argument(
        "--dotnet-configuration",
        default="Release",
        choices=["Debug", "Release"],
        help="Benchmark dotnet build configuration.",
    )
    args = parser.parse_args()

    results: list[dict[str, Any]] = []
    for backend in args.backends:
        if backend == "python":
            results.append(run_python(args))
        elif backend == "csharp":
            results.append(run_csharp(args))
        elif backend == "go":
            results.append(run_go(args))
        elif backend == "typescript":
            results.append(run_typescript(args))

    if not results:
        print("No benchmark backends selected.")
        return 1

    _print_result_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
