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

Every decision path reads ``time.time()`` — ``cleanup_expired`` to sweep,
``claim``/``resolve``/``claim_request`` to refuse a late decision — so the
clock is stubbed throughout. An approval store asserted against wall time is a
flaky test, and worse here: the fixtures expire at a fixed constant that real
wall time is far past, so an unstubbed block would quietly record the timeout
path in place of the behaviour it is named for.

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
    """Claim transitions once and only once; resolve is the lenient sibling.

    Every fixture here expires at the fixed ``NOW + 60``, which real wall time
    passed long ago — so the clock is pinned for the whole block. Without that
    the store would find each request already expired and the corpus would
    record the timeout path under the name of the exactly-once one.
    """
    with mock.patch("time.time", return_value=NOW):
        store = InMemoryApprovalStore()
        store.add(_request("r0", ApprovalStatus.PENDING, NOW + 60.0))
        stored = store.get("r0")
        assert stored is not None
        first = store.claim("r0", ApprovalStatus.APPROVED, expected_revision=stored.revision)
        second = store.claim("r0", ApprovalStatus.REJECTED, expected_revision=stored.revision)
        unknown = store.claim("nope", ApprovalStatus.APPROVED, expected_revision=1)

        # resolve on an already-resolved request leaves it alone, and on an
        # unknown id does nothing at all rather than raising.
        store.resolve("r0", ApprovalStatus.REJECTED, expected_revision=stored.revision)
        store.resolve("nope", ApprovalStatus.APPROVED, expected_revision=1)

        pending = InMemoryApprovalStore()
        pending.add(_request("r1", ApprovalStatus.PENDING, NOW + 60.0))
        stored_pending = pending.get("r1")
        assert stored_pending is not None
        pending.resolve("r1", ApprovalStatus.REJECTED, expected_revision=stored_pending.revision)

    return {
        "first_claim": first,
        "second_claim": second,
        "unknown_claim": unknown,
        "status_after_double_claim": store.get("r0").status.value,  # type: ignore[union-attr]
        "status_after_resolve": pending.get("r1").status.value,  # type: ignore[union-attr]
        "unknown_get_is_none": store.get("nope") is None,
    }


def _two_phase_record() -> dict[str, Any]:
    """The two-phase decision: pending → resolving → approved or refused.

    ``claim_request`` reserves one exact revision and hands back the request
    the caller now owns; ``finalize`` writes the outcome, and only from
    RESOLVING. Both are revision-gated, so a decision for a request id that
    was pruned and reused cannot land on the new one.
    """
    with mock.patch("time.time", return_value=NOW):
        store = InMemoryApprovalStore()
        store.add(_request("r0", ApprovalStatus.PENDING, NOW + 60.0))
        stored = store.get("r0")
        assert stored is not None
        claimed = store.claim_request("r0", ApprovalStatus.RESOLVING, expected_revision=stored.revision)
        assert claimed is not None
        # The losing racer of a simultaneous decision: the request is no
        # longer PENDING, so there is nothing left to reserve.
        second_claim = store.claim_request("r0", ApprovalStatus.RESOLVING, expected_revision=stored.revision)
        unknown_claim = store.claim_request("nope", ApprovalStatus.RESOLVING, expected_revision=1)
        status_after_claim = store.get("r0").status.value  # type: ignore[union-attr]

        stale_finalize = store.finalize("r0", ApprovalStatus.APPROVED, expected_revision=stored.revision + 99)
        finalized = store.finalize("r0", ApprovalStatus.APPROVED, expected_revision=stored.revision)
        refinalized = store.finalize("r0", ApprovalStatus.REFUSED, expected_revision=stored.revision)
        unknown_finalize = store.finalize("nope", ApprovalStatus.APPROVED, expected_revision=1)

        # Finalizing a request nobody reserved writes nothing: the two phases
        # are not optional.
        skipped = InMemoryApprovalStore()
        skipped.add(_request("r1", ApprovalStatus.PENDING, NOW + 60.0))
        skipped_stored = skipped.get("r1")
        assert skipped_stored is not None
        finalize_from_pending = skipped.finalize(
            "r1", ApprovalStatus.APPROVED, expected_revision=skipped_stored.revision
        )

        # The refusing half of the same machine.
        refusing = InMemoryApprovalStore()
        refusing.add(_request("r2", ApprovalStatus.PENDING, NOW + 60.0))
        refusing_stored = refusing.get("r2")
        assert refusing_stored is not None
        refusing.claim_request("r2", ApprovalStatus.RESOLVING, expected_revision=refusing_stored.revision)
        refusing.finalize("r2", ApprovalStatus.REFUSED, expected_revision=refusing_stored.revision)

        # Only approved and refused are outcomes; the rest are caller mistakes.
        rejected_statuses = []
        for status in ApprovalStatus:
            if status in {ApprovalStatus.APPROVED, ApprovalStatus.REFUSED}:
                continue
            try:
                store.finalize("r0", status, expected_revision=stored.revision)
            except ValueError as exc:
                rejected_statuses.append({"status": status.value, "message": str(exc)})

        # A reservation attempt that arrives after the window closed times the
        # request out instead, exactly as a plain claim does.
        expiring = InMemoryApprovalStore()
        expiring.add(_request("r3", ApprovalStatus.PENDING, NOW + 60.0))
        expiring_stored = expiring.get("r3")
        assert expiring_stored is not None

    with mock.patch("time.time", return_value=NOW + 61.0):
        expired_claim = expiring.claim_request(
            "r3", ApprovalStatus.RESOLVING, expected_revision=expiring_stored.revision
        )

    return {
        "claimed": {
            "id": claimed.id,
            "status": claimed.status.value,
            "revision": claimed.revision,
            "command": claimed.command,
        },
        "claim_snapshot_is_a_copy": store.get("r0") is not claimed,
        "second_claim_request_is_none": second_claim is None,
        "unknown_claim_request_is_none": unknown_claim is None,
        "status_after_claim_request": status_after_claim,
        "stale_revision_finalize": stale_finalize,
        "finalized": finalized,
        "status_after_finalize": store.get("r0").status.value,  # type: ignore[union-attr]
        "second_finalize": refinalized,
        "unknown_finalize": unknown_finalize,
        "finalize_from_pending": finalize_from_pending,
        "status_after_finalize_from_pending": skipped.get("r1").status.value,  # type: ignore[union-attr]
        "status_after_refuse": refusing.get("r2").status.value,  # type: ignore[union-attr]
        "rejected_finalize_statuses": rejected_statuses,
        "expired_claim_request_is_none": expired_claim is None,
        "status_after_expired_claim_request": expiring.get("r3").status.value,  # type: ignore[union-attr]
    }


def _notify_record() -> dict[str, Any]:
    """A failed claim on an expired request, and when its timeout is heard.

    The decision call queues the snapshot rather than delivering it — the
    reference runs no listener code inside a decision — and the route that
    lost calls ``notify_expired()`` at once, so the browser learns the request
    timed out then and not whenever the next cleanup sweep happens to run. The
    queue is keyed by (id, revision), so however many drains follow, the
    expiry goes out exactly once.
    """
    seen: list[dict[str, Any]] = []
    store = InMemoryApprovalStore()
    store.subscribe_expired(
        lambda req: seen.append({"id": req.id, "revision": req.revision, "status": req.status.value})
    )
    with mock.patch("time.time", return_value=NOW):
        store.add(_request("r0", ApprovalStatus.PENDING, NOW + 60.0))
        stored = store.get("r0")
        assert stored is not None

    with mock.patch("time.time", return_value=NOW + 61.0):
        claimed = store.claim("r0", ApprovalStatus.APPROVED, expected_revision=stored.revision)
        during_decision = list(seen)
        asyncio.run(store.notify_expired())
        after_notify = list(seen)
        # The losing racer's own claim finds a request that is already TIMEOUT,
        # so it queues nothing new and a second delivery hands out nothing.
        second_claim = store.claim("r0", ApprovalStatus.REJECTED, expected_revision=stored.revision)
        asyncio.run(store.notify_expired())
        after_second_notify = list(seen)
        # Neither does the cleanup sweep that eventually comes along.
        asyncio.run(store.cleanup_expired())
        after_cleanup = list(seen)

    return {
        "claim_succeeded": claimed,
        "second_claim_succeeded": second_claim,
        "notified_during_decision": during_decision,
        "notified_after_notify_expired": after_notify,
        "notified_after_second_notify": after_second_notify,
        "notified_after_cleanup": after_cleanup,
        "status": store.get("r0").status.value,  # type: ignore[union-attr]
    }


def _replacement_record() -> dict[str, Any]:
    """Adding a live duplicate id is rejected; the original request survives."""
    store = InMemoryApprovalStore()
    store.add(_request("r0", ApprovalStatus.PENDING, NOW + 60.0))
    duplicate_accepted = store.add(_request("r0", ApprovalStatus.APPROVED, NOW + 120.0))
    assert not duplicate_accepted
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
        "two_phase": _two_phase_record(),
        "notify": _notify_record(),
        "replacement": _replacement_record(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CLEANUP_CASES)} cleanup cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
