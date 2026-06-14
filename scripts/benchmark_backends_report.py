#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report formatting helpers for backend benchmark results."""

from __future__ import annotations

from typing import Any


def _delta(a: float, b: float) -> str:
    if a == 0:
        return "N/A"
    pct = ((b - a) / a) * 100
    return f"{pct:+.0f}%"


def print_comparison(fa: dict[str, Any], cf: dict[str, Any]) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {'Metric':<25} {'FastAPI':>10} {'CF Worker':>10} {'Delta':>8}")
    print(f"  {'-' * 25} {'-' * 10} {'-' * 10} {'-' * 8}")

    rows = [
        ("WS handshake p50", fa["handshake"]["p50"], cf["handshake"]["p50"], "ms"),
        ("WS handshake p95", fa["handshake"]["p95"], cf["handshake"]["p95"], "ms"),
        ("Hijack cycle p50", fa["hijack"]["p50"], cf["hijack"]["p50"], "ms"),
        ("Hijack ops/sec", fa["hijack"]["ops_per_sec"], cf["hijack"]["ops_per_sec"], ""),
        ("Broadcast fps", fa["broadcast"]["fps"], cf["broadcast"]["fps"], ""),
        ("Broadcast lag p95", fa["broadcast"]["lag_p95"], cf["broadcast"]["lag_p95"], "ms"),
    ]
    all_tiers = sorted(set(fa["scaling"]) | set(cf["scaling"]), key=int)
    for key in all_tiers:
        fa_v = fa["scaling"].get(key, {}).get("p50", 0)
        cf_v = cf["scaling"].get(key, {}).get("p50", 0)
        suffix = ""
        if key not in cf["scaling"]:
            suffix = " (FA only)"
        elif key not in fa["scaling"]:
            suffix = " (CF only)"
        rows.append((f"Scale@{key} p50{suffix}", fa_v, cf_v, "ms"))

    for label, fa_v, cf_v, unit in rows:
        suffix = unit if unit else ""
        print(f"  {label:<25} {fa_v:>9.1f}{suffix} {cf_v:>9.1f}{suffix} {_delta(fa_v, cf_v):>8}")
