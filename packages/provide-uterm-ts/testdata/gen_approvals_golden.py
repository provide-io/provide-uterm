#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``approvals`` port.

The store is plain logic, so what needs pinning is not arithmetic but the
*boundaries*: expiry is ``expires_at < now`` and pruning is
``expires_at + PRUNE_TTL < now``, both strict. A request that expires exactly
now is still pending. Those comparisons are one keystroke from wrong and a
hand-written expectation would just as easily encode the wrong one, so the
cleanup table is recorded from the reference rather than asserted from memory.

``cleanup_expired`` reads ``time.time()``, which the corpus stubs: an approval
store asserted against wall time is a flaky test.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_approvals_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest import mock

from provide.uterm.server.bridge.hub.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    InMemoryApprovalStore,
)

OUT = Path(__file__).with_name("approvals_golden.json")

NOW = 1_000_000.0
PRUNE_TTL = 3600.0

# (name, status, expires_at) — every case is evaluated against the same NOW.
CLEANUP_CASES: list[tuple[str, ApprovalStatus, float]] = [
    ("pending, long expired", ApprovalStatus.PENDING, NOW - 60.0),
    ("pending, expired by an epsilon", ApprovalStatus.PENDING, NOW - 1e-6),
    ("pending, expiring exactly now", ApprovalStatus.PENDING, NOW),
    ("pending, not yet expired", ApprovalStatus.PENDING, NOW + 60.0),
    ("approved, inside the prune window", ApprovalStatus.APPROVED, NOW - 60.0),
    ("approved, exactly at the prune edge", ApprovalStatus.APPROVED, NOW - PRUNE_TTL),
    ("approved, past the prune edge", ApprovalStatus.APPROVED, NOW - PRUNE_TTL - 1e-3),
    ("rejected, past the prune edge", ApprovalStatus.REJECTED, NOW - PRUNE_TTL - 60.0),
    ("timeout, past the prune edge", ApprovalStatus.TIMEOUT, NOW - PRUNE_TTL - 60.0),
    ("approved, not yet expired", ApprovalStatus.APPROVED, NOW + 60.0),
]


def _request(request_id: str, status: ApprovalStatus, expires_at: float) -> ApprovalRequest:
    """Build a request whose only interesting fields are status and expiry."""
    return ApprovalRequest(
        id=request_id,
        worker_id="w1",
        submitter_id="s1",
        command="ls",
        status=status,
        created_at=NOW - 120.0,
        expires_at=expires_at,
    )


def _cleanup_record() -> dict[str, Any]:
    """One store holding every case, cleaned once, then read back."""
    store = InMemoryApprovalStore()
    notified: list[str] = []
    store.on_expired = notified.append
    for index, (_, status, expires_at) in enumerate(CLEANUP_CASES):
        store.add(_request(f"r{index}", status, expires_at))

    with mock.patch("time.time", return_value=NOW):
        asyncio.run(store.cleanup_expired())

    outcomes = []
    for index, (name, status, expires_at) in enumerate(CLEANUP_CASES):
        req = store.get(f"r{index}")
        outcomes.append(
            {
                "name": name,
                "initial_status": status.value,
                "expires_at": expires_at,
                "present": req is not None,
                "status": None if req is None else req.status.value,
            }
        )
    return {"now": NOW, "prune_ttl": PRUNE_TTL, "outcomes": outcomes, "notified": notified}


def _claim_record() -> dict[str, Any]:
    """Claim transitions once and only once; resolve is the lenient sibling."""
    store = InMemoryApprovalStore()
    store.add(_request("r0", ApprovalStatus.PENDING, NOW + 60.0))
    first = store.claim("r0", ApprovalStatus.APPROVED)
    second = store.claim("r0", ApprovalStatus.REJECTED)
    unknown = store.claim("nope", ApprovalStatus.APPROVED)

    # resolve on an already-resolved request leaves it alone, and on an
    # unknown id does nothing at all rather than raising.
    store.resolve("r0", ApprovalStatus.REJECTED)
    store.resolve("nope", ApprovalStatus.APPROVED)

    pending = InMemoryApprovalStore()
    pending.add(_request("r1", ApprovalStatus.PENDING, NOW + 60.0))
    pending.resolve("r1", ApprovalStatus.REJECTED)

    return {
        "first_claim": first,
        "second_claim": second,
        "unknown_claim": unknown,
        "status_after_double_claim": store.get("r0").status.value,  # type: ignore[union-attr]
        "status_after_resolve": pending.get("r1").status.value,  # type: ignore[union-attr]
        "unknown_get_is_none": store.get("nope") is None,
    }


def _replacement_record() -> dict[str, Any]:
    """Adding the same id twice replaces rather than merges."""
    store = InMemoryApprovalStore()
    store.add(_request("r0", ApprovalStatus.PENDING, NOW + 60.0))
    store.add(_request("r0", ApprovalStatus.APPROVED, NOW + 120.0))
    req = store.get("r0")
    assert req is not None
    return {"status": req.status.value, "expires_at": req.expires_at}


def main() -> int:
    """Write the golden corpus and report the case count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_approvals_golden.py",
        "statuses": [status.value for status in ApprovalStatus],
        "cleanup": _cleanup_record(),
        "claim": _claim_record(),
        "replacement": _replacement_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CLEANUP_CASES)} cleanup cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
