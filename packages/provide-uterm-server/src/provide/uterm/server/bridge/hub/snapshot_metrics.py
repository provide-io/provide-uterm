#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Counters for the ways a committed snapshot fails to reach its client.

Logs answer "what happened to THIS frame"; counters answer "how often, and is
it getting worse". The 2026-08-14 investigation needed both and had neither:
the delivery failures were silent, and once traced they arrived as 9,881 log
lines with no aggregate to compare against.

Instruments resolve lazily — ``counter()`` returns a working in-process
fallback when no meter is configured, and upgrades itself once
``setup_telemetry`` installs a real provider, so importing this module is safe
before telemetry setup.
"""

from __future__ import annotations

from provide.telemetry.metrics import counter

snapshot_commit_dropped = counter(
    "uterm.snapshot.commit_dropped",
    description="Snapshots refused at commit because the sending connection no longer owns the worker id.",
)

snapshot_broadcast_no_browsers = counter(
    "uterm.snapshot.broadcast_no_browsers",
    description="Snapshots committed and broadcast with no eligible browser to receive them.",
)

snapshot_broadcast_send_failed = counter(
    "uterm.snapshot.broadcast_send_failed",
    description="Snapshot sends that raised or timed out on a browser socket.",
)

snapshot_wait_timeout = counter(
    "uterm.snapshot.wait_timeout",
    description="Snapshot polls that returned nothing fresh within their window.",
)

__all__ = [
    "snapshot_broadcast_no_browsers",
    "snapshot_broadcast_send_failed",
    "snapshot_commit_dropped",
    "snapshot_wait_timeout",
]
