#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Benchmark ControlFrameDecoder decode throughput before/after optimization.

Usage examples:

    uv run python scripts/benchmark_control_channel_memoryview.py --baseline-revision HEAD~1
    uv run python scripts/benchmark_control_channel_memoryview.py --passes 7 --frame-count 200000
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import types
from pathlib import Path
from statistics import median
from typing import Any

from provide.uterm import control_channel as live_cc

ROOT = Path(__file__).resolve().parent.parent
CONTROL_PATH = "packages/provide-uterm/src/provide/uterm/control_channel.py"


def _load_module_from_revision(revision: str, module_name: str) -> types.ModuleType:
    result = subprocess.run(
        ["git", "show", f"{revision}:{CONTROL_PATH}"],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    source = result.stdout
    module = types.ModuleType(module_name)
    module.__file__ = f"{CONTROL_PATH} ({revision})"
    module.__package__ = ""
    sys.modules[module_name] = module
    exec(compile(source, f"{CONTROL_PATH} ({revision})", "exec"), module.__dict__)  # noqa: S102
    return module


def _seeded_generator(seed: int) -> object:
    state = seed & 0xFFFFFFFF
    if state == 0:
        state = 0x9E3779B9

    while True:
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        state &= 0xFFFFFFFF
        yield state / 2**32


def _normalize_events(events: list[object]) -> list[tuple[str, Any]]:
    normalized: list[tuple[str, Any]] = []
    for item in events:
        kind = getattr(item, "kind", None)
        if kind == "data":
            normalized.append(("data", str(item.data)))  # pyodide-safe attr names
        elif kind == "control":
            normalized.append(("control", dict(item.control)))
        else:
            raise TypeError(f"unknown event type: {type(item)!r}")
    return normalized


def _decode_stream(module: types.ModuleType, chunks: list[str]) -> list[tuple[str, Any]]:
    decoder = module.ControlFrameDecoder()
    events = []
    for chunk in chunks:
        events.extend(decoder.feed(chunk))
    events.extend(decoder.finish())
    return _normalize_events(events)


def _chunk_stream(stream: str, *, chunk_size: int) -> list[str]:
    if chunk_size < 1:
        raise ValueError("chunk-size must be >= 1")
    return [stream[i : i + chunk_size] for i in range(0, len(stream), chunk_size)]


def _make_payload_text(size: int) -> str:
    return "x" * size


def _build_benchmark_stream(
    module: types.ModuleType,
    *,
    frame_count: int,
    control_ratio: float,
    data_size: int,
    control_size: int,
    seed: int,
) -> str:
    data_segment = _make_payload_text(max(0, data_size))
    control_payload = _make_payload_text(max(0, control_size))
    rng = _seeded_generator(seed)
    pieces: list[str] = []

    for idx in range(frame_count):
        if next(rng) < control_ratio:
            payload = {
                "type": "bench",
                "id": idx,
                "seed": seed,
                "payload": control_payload,
            }
            pieces.append(module.encode_control_frame(payload))
            continue

        segment = data_segment
        if segment and next(rng) < 0.01:
            midpoint = min(64, data_size // 2)
            segment = data_segment[:midpoint] + module.DLE + "DLE_ESC" + data_segment[midpoint:]
            segment = segment[:data_size]
        pieces.append(module.encode_terminal_data(segment))

    return "".join(pieces)


def _benchmark_module(module: types.ModuleType, chunks: list[str], *, passes: int) -> tuple[float, float, float, int]:
    elapsed: list[float] = []
    event_count = 0
    for _ in range(max(1, passes)):
        start = time.perf_counter()
        events = _decode_stream(module, chunks)
        elapsed.append(time.perf_counter() - start)
        event_count = len(events)
    return (
        sum(elapsed) / len(elapsed),
        median(elapsed),
        min(elapsed),
        event_count,
    )


def _run(
    live_module: types.ModuleType,
    baseline_module: types.ModuleType,
    *,
    frame_count: int,
    control_ratio: float,
    chunk_size: int,
    data_size: int,
    control_size: int,
    seed: int,
    passes: int,
    label_after: str,
    label_before: str,
) -> None:
    stream = _build_benchmark_stream(
        live_module,
        frame_count=frame_count,
        control_ratio=control_ratio,
        data_size=data_size,
        control_size=control_size,
        seed=seed,
    )
    chunks = _chunk_stream(stream, chunk_size=chunk_size)
    payload_bytes = len(stream.encode("utf-8"))
    if payload_bytes <= 0:
        raise ValueError("generated empty stream")

    _, baseline_median, baseline_min, baseline_events = _benchmark_module(
        baseline_module,
        chunks,
        passes=passes,
    )
    after_mean, after_median, after_min, after_events = _benchmark_module(
        live_module,
        chunks,
        passes=passes,
    )

    if baseline_events != after_events:
        raise RuntimeError(
            f"parity mismatch: {label_before} produced {baseline_events} events, {label_after} produced {after_events}"
        )
    if baseline_events == 0:
        raise RuntimeError("parity mismatch: both implementations emitted no events")

    # Full decode parity check (single deterministic run).
    if _decode_stream(baseline_module, chunks) != _decode_stream(live_module, chunks):
        raise RuntimeError("parity mismatch on decode payload contents")

    baseline_mib = payload_bytes / (baseline_median * 1024 * 1024)
    after_mib = payload_bytes / (after_median * 1024 * 1024)
    ratio = baseline_median / after_median if after_median > 0 else float("inf")
    delta = ((after_median - baseline_median) / baseline_median) * 100

    print(f"Generated stream: {payload_bytes} bytes, {frame_count} frames, chunk size {chunk_size}")
    print(f"Baseline ({label_before}): {baseline_median:.4f}s, {baseline_median:.4f}s median, {baseline_mib:.2f} MiB/s")
    print(f"After   ({label_after}):   {after_median:.4f}s, {after_median:.4f}s median, {after_mib:.2f} MiB/s")
    print(f"Events emitted: {baseline_events}")
    print(f"Median speedup: {ratio:.2f}x {'faster' if delta <= 0 else 'slower'} ({delta:.1f}%)")
    print(
        f"Stability  : {passes} runs, min-after={after_min:.4f}s, min-baseline={baseline_min:.4f}s, mean-after={after_mean:.4f}s"
    )

    summary = {
        "backend": "python",
        "generated_bytes": payload_bytes,
        "frame_count": frame_count,
        "chunk_size": chunk_size,
        "before_label": label_before,
        "after_label": label_after,
        "baseline_seconds": baseline_median,
        "median_seconds": after_median,
        "before_events": baseline_events,
        "before_mib_per_s": payload_bytes / (baseline_median * 1024 * 1024),
        "events": baseline_events,
        "mean_seconds": after_mean,
        "min_seconds": after_min,
        "speedup_vs_before": ratio,
        "mib_per_s": after_mib,
        "passes": passes,
    }
    print(json.dumps(summary))


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark memoryview ControlFrameDecoder optimizations.")
    parser.add_argument("--frame-count", type=int, default=200000, help="Number of frames to synthesize.")
    parser.add_argument(
        "--control-ratio", type=float, default=0.25, help="Ratio of control frames in generated stream."
    )
    parser.add_argument("--chunk-size", type=int, default=4096, help="Chunk size (chars) used when feeding decoder.")
    parser.add_argument("--data-size", type=int, default=256, help="Raw terminal-data segment size.")
    parser.add_argument("--control-size", type=int, default=128, help="Control payload body size.")
    parser.add_argument("--seed", type=int, default=1337, help="Deterministic stream seed.")
    parser.add_argument("--passes", type=int, default=5, help="Repeat each variant this many times.")
    parser.add_argument(
        "--baseline-revision",
        default="HEAD",
        help="Git revision to load baseline implementation from (default: HEAD).",
    )
    parser.add_argument("--before-label", default="baseline", help="Label for the baseline implementation.")
    parser.add_argument("--after-label", default="memoryview", help="Label for the current implementation.")
    args = parser.parse_args()

    baseline = _load_module_from_revision(args.baseline_revision, "provide.uterm.control_channel.baseline")
    _run(
        live_cc,
        baseline,
        frame_count=args.frame_count,
        control_ratio=args.control_ratio,
        chunk_size=args.chunk_size,
        data_size=args.data_size,
        control_size=args.control_size,
        seed=args.seed,
        passes=max(1, args.passes),
        label_after=args.after_label,
        label_before=args.before_label,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
