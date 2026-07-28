#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript control plane.

The in-memory backend is not a toy: it has to behave like the SQLite one, or a
deployment that develops against memory and ships on SQLite finds out the
difference in production. Three behaviours carry that weight.

**Optimistic concurrency.** Two overlapping transactions that write the same
key cannot both succeed. The memory backend detects that at commit time so a
lease-acquire race yields exactly one winner on either backend — the same
outcome SQLite's ``BEGIN IMMEDIATE`` produces by serialising.

**Key-level merges.** A transaction applies only the keys *it* changed, so two
transactions touching different rows both commit. Merging whole tables would
have the later commit silently undo the earlier one.

**Reaping and the audit head.** The reap predicate mirrors SQLite's — strict
``<``, and a null timestamp never matches — so both backends prune exactly the
same rows. The audit head only ever moves forward: a lower sequence is a
no-op, which is what stops a rollback being accepted as an update.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_controlplane_golden.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from provide.uterm.control.plane.approval.types import ApprovalRecord
from provide.uterm.control.plane.bootstrap import bootstrap_control_plane
from provide.uterm.control.plane.capability import EngineCapabilities
from provide.uterm.control.plane.errors import ControlPlaneConflictError
from provide.uterm.control.plane.lease.types import LeaseRecord
from provide.uterm.control.plane.memory.engine import MemoryControlPlane
from provide.uterm.control.plane.session.types import SessionRecord
from provide.uterm.control.plane.token.types import ResumeTokenRecord, SessionTokenRecord
from provide.uterm.control.plane.types import ControlPlaneConfig

OUT = Path(__file__).with_name("controlplane_golden.json")

NOW = 1_000.0


def _session(session_id: str, *, state: str = "running", deleted_at: float | None = None) -> SessionRecord:
    """A session record with the fields a test cares about."""
    return SessionRecord(
        session_id=session_id,
        display_name=session_id,
        connector_type="shell",
        owner="alice",
        visibility="operator",
        lifecycle_state=state,  # type: ignore[arg-type]
        created_at=1.0,
        updated_at=1.0,
        deleted_at=deleted_at,
    )


def _approval(approval_id: str, *, state: str = "pending", created_at: float = 1.0, resolved_at: float | None = None):
    """An approval record with the fields a test cares about."""
    return ApprovalRecord(
        approval_id=approval_id,
        session_id="s",
        command="rm -rf /",
        requested_by="alice",
        state=state,  # type: ignore[arg-type]
        created_at=created_at,
        resolved_at=resolved_at,
    )


def _resume(value: str, expires_at: float, *, role: str = "viewer", revoked_at: float | None = None):
    """A resume token. The values are fixtures, not credentials."""
    return ResumeTokenRecord(
        token_value=value,  # a fixture, not a credential
        session_id="s",
        role=role,
        created_at=1.0,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


def _session_token(kind: str, value: str, *, created_at: float = 1.0):
    """A session token. The values are fixtures, not credentials."""
    return SessionTokenRecord(
        session_id="s",
        token_kind=kind,  # a fixture, not a credential
        token_value=value,  # a fixture, not a credential
        created_at=created_at,
        expires_at=None,
    )


def _lease(
    session_id: str, owner: str, *, expires_at: float = NOW + 60, deleted_at: float | None = None
) -> LeaseRecord:
    """A lease record with the fields a test cares about."""
    return LeaseRecord(
        session_id=session_id,
        hijack_id=f"h-{owner}",
        owner=owner,
        lease_expires_at=expires_at,
        created_at=NOW,
        deleted_at=deleted_at,
    )


async def _record_conflict() -> dict[str, Any]:
    """Two transactions racing for the same lease. Exactly one may win."""
    plane = MemoryControlPlane(ControlPlaneConfig())
    first = await plane.begin()
    second = await plane.begin()
    await plane.lease_store(first).put_lease(_lease("s1", "alice"))
    await plane.lease_store(second).put_lease(_lease("s1", "bob"))
    await first.commit()
    try:
        await second.commit()
        loser = None
    except ControlPlaneConflictError as exc:
        loser = str(exc)

    settled = await plane.begin()
    winner = await plane.lease_store(settled).get_lease("s1")
    return {
        "loser_error": loser,
        "winner": winner.owner if winner else None,
        "second_is_closed": second.closed,
    }


async def _record_disjoint() -> dict[str, Any]:
    """Two transactions touching different keys. Both must survive."""
    plane = MemoryControlPlane(ControlPlaneConfig())
    first = await plane.begin()
    second = await plane.begin()
    await plane.lease_store(first).put_lease(_lease("s1", "alice"))
    await plane.lease_store(second).put_lease(_lease("s2", "bob"))
    await first.commit()
    await second.commit()
    check = await plane.begin()
    store = plane.lease_store(check)
    one = await store.get_lease("s1")
    two = await store.get_lease("s2")
    return {"s1": one.owner if one else None, "s2": two.owner if two else None}


async def _record_isolation() -> dict[str, Any]:
    """What an uncommitted transaction is, and is not, visible to."""
    plane = MemoryControlPlane(ControlPlaneConfig())
    writer = await plane.begin()
    await plane.lease_store(writer).put_lease(_lease("s1", "alice"))

    reader = await plane.begin()
    before_commit = await plane.lease_store(reader).get_lease("s1")
    own_read = await plane.lease_store(writer).get_lease("s1")

    await writer.commit()
    after_commit_same_reader = await plane.lease_store(reader).get_lease("s1")
    fresh = await plane.begin()
    after_commit_fresh = await plane.lease_store(fresh).get_lease("s1")

    rolled_back = await plane.begin()
    await plane.lease_store(rolled_back).put_lease(_lease("s2", "bob"))
    await rolled_back.rollback()
    post_rollback = await plane.begin()
    rolled = await plane.lease_store(post_rollback).get_lease("s2")

    return {
        "another_transaction_cannot_see_it": before_commit is None,
        "its_own_writes_are_visible_to_it": own_read.owner if own_read else None,
        "a_reader_that_started_earlier_still_cannot": after_commit_same_reader is None,
        "a_new_transaction_can": after_commit_fresh.owner if after_commit_fresh else None,
        "a_rollback_leaves_nothing": rolled is None,
        "committing_twice_is_a_no_op": await _commit_twice(plane),
    }


async def _commit_twice(plane: MemoryControlPlane) -> bool:
    """A second commit on a closed transaction does nothing rather than raising."""
    tx = await plane.begin()
    await tx.commit()
    await tx.commit()
    return tx.closed


async def _record_reap() -> dict[str, Any]:
    """Which rows the reaper drops, and which it keeps."""
    plane = MemoryControlPlane(ControlPlaneConfig())
    tx = await plane.begin()
    leases = plane.lease_store(tx)
    tokens = plane.token_store(tx)
    sessions = plane.session_store(tx)
    approvals = plane.approval_store(tx)

    # Cutoff is NOW - 100 = 900. Strictly older than that goes.
    await leases.put_lease(_lease("expired", "a", expires_at=899.0))
    await leases.put_lease(_lease("on-the-cutoff", "b", expires_at=900.0))
    await leases.put_lease(_lease("live", "c", expires_at=NOW + 60))
    await leases.put_lease(_lease("soft-deleted", "d", expires_at=NOW + 60, deleted_at=899.0))

    await tokens.create_resume_token(_resume("old", 899.0))
    await tokens.create_resume_token(_resume("fresh", NOW + 60))
    await tokens.create_resume_token(_resume("revoked", NOW + 60, revoked_at=899.0))
    await tokens.put_session_token(_session_token("never-expires", "t"))
    await sessions.upsert_session(_session("gone", state="deleted", deleted_at=899.0))
    await sessions.upsert_session(_session("here"))
    await approvals.put_approval(_approval("settled", state="approved", resolved_at=899.0))
    await approvals.put_approval(_approval("waiting"))
    await tx.commit()

    removed = await plane.reap(now=NOW, retention_s=100)

    after = await plane.begin()
    survivors = {
        "leases": sorted(after.state.leases),
        "resume_tokens": sorted(after.state.resume_tokens),
        "session_tokens": sorted(key[0] + ":" + key[1] for key in after.state.session_tokens),
        "sessions": sorted(after.state.sessions),
        "approvals": sorted(after.state.approvals),
    }
    return {"removed": removed, "survivors": survivors, "cutoff": NOW - 100}


async def _record_audit_head() -> dict[str, Any]:
    """The head moves forward only."""
    plane = MemoryControlPlane(ControlPlaneConfig())
    empty = await plane.get_audit_head()
    await plane.set_audit_head(5, "hash-5")
    after_first = await plane.get_audit_head()
    await plane.set_audit_head(4, "hash-4")
    after_lower = await plane.get_audit_head()
    await plane.set_audit_head(5, "hash-5-again")
    after_equal = await plane.get_audit_head()
    await plane.set_audit_head(6, "hash-6")
    after_higher = await plane.get_audit_head()
    return {
        "starts_empty": empty is None,
        "after_first": list(after_first) if after_first else None,
        "a_lower_sequence_is_ignored": list(after_lower) if after_lower else None,
        "an_equal_sequence_is_ignored": list(after_equal) if after_equal else None,
        "a_higher_sequence_moves_it": list(after_higher) if after_higher else None,
    }


async def _record_stores() -> dict[str, Any]:
    """What each store does with the records it is given."""
    plane = MemoryControlPlane(ControlPlaneConfig())
    tx = await plane.begin()
    approvals = plane.approval_store(tx)
    tokens = plane.token_store(tx)
    sessions = plane.session_store(tx)
    leases = plane.lease_store(tx)

    # Deliberately out of order, and with a tie on created_at.
    await approvals.put_approval(_approval("c", created_at=2.0))
    await approvals.put_approval(_approval("a", created_at=2.0))
    await approvals.put_approval(_approval("b", created_at=1.0))
    await approvals.put_approval(_approval("done", state="approved", created_at=0.5, resolved_at=1.0))

    await tokens.create_resume_token(_resume("live", NOW + 60, role="operator"))
    await tokens.create_resume_token(_resume("dead", NOW + 60, role="operator"))
    await tokens.revoke_resume_token("dead", NOW)
    await tokens.put_session_token(_session_token("join", "v1"))
    await tokens.put_session_token(_session_token("join", "v2", created_at=2.0))

    await sessions.upsert_session(_session("s1"))
    await sessions.mark_deleted("s1", NOW)
    await sessions.mark_deleted("never-existed", NOW)

    await leases.put_lease(_lease("s1", "alice"))
    await leases.clear_lease("s1")
    await leases.clear_lease("never-existed")

    joined = await tokens.get_session_token("s", "join")
    deleted = await sessions.get_session("s1")
    return {
        "pending_in_order": [record.approval_id for record in await approvals.list_pending()],
        "a_resolved_approval_is_not_pending": await approvals.get_approval("done") is not None,
        "a_revoked_resume_token_reads_as_absent": await tokens.get_resume_token("dead") is None,
        "a_live_one_does_not": (await tokens.get_resume_token("live")).token_value,
        "a_session_token_is_keyed_by_kind": joined.token_value if joined else None,
        "a_deleted_session_keeps_its_row": {
            "lifecycle_state": deleted.lifecycle_state if deleted else None,
            "deleted_at": deleted.deleted_at if deleted else None,
        },
        "clearing_a_lease_that_is_not_there_is_fine": await leases.get_lease("s1") is None,
    }


async def _record_bootstrap() -> dict[str, Any]:
    """What the factory builds, and what it refuses."""
    memory = await bootstrap_control_plane(ControlPlaneConfig(backend="memory"))
    try:
        await bootstrap_control_plane(ControlPlaneConfig(backend="postgres"))  # type: ignore[arg-type]
        refusal = None
    except ValueError as exc:
        refusal = str(exc)
    return {"memory_builds": type(memory).__name__, "unknown_backend": refusal}


async def _main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "conflict": await _record_conflict(),
        "disjoint": await _record_disjoint(),
        "isolation": await _record_isolation(),
        "reap": await _record_reap(),
        "audit_head": await _record_audit_head(),
        "stores": await _record_stores(),
        "bootstrap": await _record_bootstrap(),
        "capabilities": asdict(EngineCapabilities()),
        "default_config": {
            "backend": ControlPlaneConfig().backend,
            "database_url": ControlPlaneConfig().database_url,
        },
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({corpus['reap']['removed']} rows reaped in the reap case)")
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
