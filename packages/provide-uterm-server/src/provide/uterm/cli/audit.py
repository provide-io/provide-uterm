#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``uterm audit`` — verify a tamper-evident WORM audit log.

Example::

    uterm audit verify /var/log/uterm/audit.log
    uterm audit verify /var/log/uterm/audit.log --expected-seq 42 --expected-hash <hex>
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, cast

from provide.uterm.server.audit_chain import verify_audit_log

if TYPE_CHECKING:
    import argparse


def _cmd_audit_verify(args: argparse.Namespace) -> None:
    """Execute ``uterm audit verify`` — verify the chain and exit 0/1."""
    expected_seq = getattr(args, "expected_seq", None)
    expected_hash = getattr(args, "expected_hash", None)

    # --expected-seq and --expected-hash must be supplied together (a head is a
    # (seq, hash) pair); one without the other is a usage error.
    if (expected_seq is None) != (expected_hash is None):
        args._parser.error("--expected-seq and --expected-hash must be given together")

    # The XOR guard above ensures both are set together; getattr loses that to Any.
    expected_head = cast("tuple[int, str] | None", (expected_seq, expected_hash) if expected_seq is not None else None)
    result = verify_audit_log(args.path, expected_head=expected_head)

    if result.ok:
        print(f"OK: {result.count} records, head seq={result.head_seq} hash={result.head_hash}")
        sys.exit(0)

    print(f"TAMPERED: {result.reason} at seq={result.first_bad_seq}")
    sys.exit(1)


def add_audit_subcommand(sub: Any) -> None:
    """Register the ``audit`` subcommand (with a nested ``verify`` action)."""
    audit_p = sub.add_parser(
        "audit",
        help="verify a tamper-evident WORM audit log",
        description="Verify the integrity of a hash-chained append-only audit log.",
    )
    audit_sub = audit_p.add_subparsers(dest="audit_command", metavar="ACTION")
    audit_sub.required = True

    verify_p = audit_sub.add_parser(
        "verify",
        help="verify the hash chain of an audit log file",
        description="Walk the audit log and confirm no record was inserted, deleted, reordered, or altered.",
    )
    verify_p.add_argument(
        "path",
        metavar="PATH",
        help="path to the JSONL audit log to verify",
    )
    verify_p.add_argument(
        "--expected-seq",
        type=int,
        metavar="N",
        default=None,
        help="expected head sequence number (requires --expected-hash)",
    )
    verify_p.add_argument(
        "--expected-hash",
        metavar="HEX",
        default=None,
        help="expected head record hash (requires --expected-seq)",
    )
    # Stash the parser so the handler can emit a proper argparse usage error
    # (exit code 2) when exactly one of the --expected-* pair is supplied.
    verify_p.set_defaults(func=_cmd_audit_verify, _parser=verify_p)
