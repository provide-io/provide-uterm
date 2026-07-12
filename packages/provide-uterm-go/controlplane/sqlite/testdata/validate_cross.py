#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Validate a Go-created control-plane DB by reading it via the Python engine.

Usage (from the repo root):
    uv run python validate_cross.py <go_created.db> <expected.json>

Prints ``CROSS_COMPAT_OK`` on success; exits non-zero with a diff on mismatch.
This proves the Go engine writes a database the Python engine reads identically.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from provide.uterm.control.plane import ControlPlaneConfig
from provide.uterm.control.plane.graphical_target import GraphicalTargetRecord
from provide.uterm.control.plane.sqlite import SqliteControlPlane


async def read_all(db_path: str, expected: dict) -> dict:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
    await plane.migrate()
    tx = await plane.begin()
    sessions = plane.session_store(tx)
    tokens = plane.token_store(tx)
    approvals = plane.approval_store(tx)
    leases = plane.lease_store(tx)
    graphical_targets = plane.graphical_target_store(tx)

    out: dict = {"sessions": [], "session_tokens": [], "resume_tokens": [], "approvals": [], "leases": []}
    for s in expected["sessions"]:
        rec = await sessions.get_session(s["session_id"])
        out["sessions"].append(asdict(rec))
    for st in expected["session_tokens"]:
        rec = await tokens.get_session_token(st["session_id"], st["token_kind"])
        out["session_tokens"].append(asdict(rec))
    for rt in expected["resume_tokens"]:
        rec = await tokens.get_resume_token(rt["token_value"])
        out["resume_tokens"].append(asdict(rec))
    for a in expected["approvals"]:
        rec = await approvals.get_approval(a["approval_id"])
        out["approvals"].append(asdict(rec))
    for lease in expected["leases"]:
        rec = await leases.get_lease(lease["session_id"])
        out["leases"].append(asdict(rec))
    if "graphical_targets" in expected:
        out["graphical_targets"] = []
        for target in expected["graphical_targets"]:
            rec = await graphical_targets.get_graphical_target(target["target_id"])
            out["graphical_targets"].append(json.loads(json.dumps(asdict(rec))))
    await tx.rollback()

    head = await plane.get_audit_head()
    out["audit_head"] = {"seq": head[0], "record_hash": head[1]}
    await plane.close()
    return out


async def create_target_db(db_path: str, expected: dict) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
    await plane.migrate()
    tx = await plane.begin()
    store = plane.graphical_target_store(tx)
    for target in expected["graphical_targets"]:
        fields = target.copy()
        fields["allowed_vm_patterns"] = tuple(target["allowed_vm_patterns"])
        fields["allowed_cidrs"] = tuple(target["allowed_cidrs"])
        fields["audit_labels"] = tuple(tuple(pair) for pair in target["audit_labels"])
        await store.put_graphical_target(GraphicalTargetRecord(**fields))
    await tx.commit()
    await plane.close()


def main() -> None:
    if sys.argv[1] == "--create":
        db_path = sys.argv[2]
        expected = json.loads(Path(sys.argv[3]).read_text())
        asyncio.run(create_target_db(db_path, expected))
        print("CROSS_COMPAT_CREATED")
        return
    db_path = sys.argv[1]
    expected = json.loads(Path(sys.argv[2]).read_text())
    got = asyncio.run(read_all(db_path, expected))
    if got != expected:
        print("CROSS_COMPAT_MISMATCH", file=sys.stderr)
        print("expected:", json.dumps(expected, sort_keys=True), file=sys.stderr)
        print("got:     ", json.dumps(got, sort_keys=True), file=sys.stderr)
        sys.exit(1)
    print("CROSS_COMPAT_OK")


if __name__ == "__main__":
    main()
