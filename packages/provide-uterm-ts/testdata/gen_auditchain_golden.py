#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the tamper-evident audit chain.

An audit log is the record of who did what, so the only thing that makes it
worth keeping is that it can be shown not to have been edited. Each record
hashes its own contents together with the hash of the one before it, so any
change anywhere breaks every link after it.

What the verifier has to catch, and what this records:

* **A record altered.** Its own hash no longer matches its contents.
* **A record removed, inserted or reordered.** The sequence stops being
  contiguous, or a link no longer points at the record before it.
* **The end cut off.** Nothing inside the log can show this — the shortened
  chain is perfectly valid — so the caller supplies the head it expects, and
  a log that ends anywhere else is a log that was rolled back.

The hash is over a canonical serialisation with the keys sorted and no
whitespace, so two runtimes hashing the same record agree byte for byte.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_auditchain_golden.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from provide.uterm.server import audit_chain

OUT = Path(__file__).resolve().parent / "auditchain_golden.json"


def _chain(count: int) -> list[dict[str, Any]]:
    """A valid chain of `count` records, built the way the reference builds one."""
    records: list[dict[str, Any]] = []
    prev = audit_chain.GENESIS_HASH
    for index in range(count):
        fields = {
            "seq": index + 1,
            # Fixed instants, so the corpus is the same on every run.
            "ts": 1_700_000_000.0 + index,
            "mono_ns": 1_000 * (index + 1),
            "action": ["session_create", "hijack_begin", "session_delete"][index % 3],
            # One principal outside ASCII, because the canonical form leaves
            # such characters as themselves and a serialiser that escaped them
            # would hash differently.
            "principal": ["ada", "bob", "ådæ ☃"][index % 3],
            "session_id": f"sess-{index + 1}",
            "source_ip": "203.0.113.7",
            "detail": {"note": f"record {index + 1} — ☃", "n": index},
            "prev_hash": prev,
        }
        payload = audit_chain._canonical_payload(**fields)
        record = {**fields, "record_hash": audit_chain.compute_record_hash(payload)}
        records.append(record)
        prev = record["record_hash"]
    return records


def _result(value: Any) -> dict[str, Any]:
    return {
        "ok": value.ok,
        "count": value.count,
        "head_seq": value.head_seq,
        "head_hash": value.head_hash,
        "first_bad_seq": value.first_bad_seq,
        "reason": value.reason,
    }


def _tampered() -> list[tuple[str, list[dict[str, Any]]]]:
    """Every way a chain can be broken, applied to a good one."""
    cases: list[tuple[str, list[dict[str, Any]]]] = []

    altered = copy.deepcopy(_chain(3))
    altered[1]["action"] = "something_else"
    cases.append(("a record altered", altered))

    detail = copy.deepcopy(_chain(3))
    detail[1]["detail"]["note"] = "edited"
    cases.append(("a detail altered", detail))

    removed = copy.deepcopy(_chain(3))
    del removed[1]
    cases.append(("a record removed", removed))

    reordered = copy.deepcopy(_chain(3))
    reordered[1], reordered[2] = reordered[2], reordered[1]
    cases.append(("two records swapped", reordered))

    duplicated = copy.deepcopy(_chain(3))
    duplicated.insert(2, copy.deepcopy(duplicated[1]))
    cases.append(("a record repeated", duplicated))

    relinked = copy.deepcopy(_chain(3))
    relinked[2]["prev_hash"] = audit_chain.GENESIS_HASH
    cases.append(("a link pointed elsewhere", relinked))

    rehashed = copy.deepcopy(_chain(3))
    rehashed[1]["record_hash"] = "0" * 64
    cases.append(("a hash replaced", rehashed))

    front = copy.deepcopy(_chain(3))
    del front[0]
    cases.append(("the beginning cut off", front))

    genesis = copy.deepcopy(_chain(3))
    genesis[0]["prev_hash"] = "f" * 64
    cases.append(("the first record unlinked", genesis))

    missing = copy.deepcopy(_chain(2))
    del missing[1]["principal"]
    cases.append(("a field missing", missing))

    seq_text = copy.deepcopy(_chain(2))
    seq_text[1]["seq"] = "2"
    cases.append(("a sequence given as text", seq_text))

    seq_bool = copy.deepcopy(_chain(2))
    seq_bool[1]["seq"] = True
    cases.append(("a sequence given as a boolean", seq_bool))

    gap = copy.deepcopy(_chain(3))
    gap[2]["seq"] = 99
    cases.append(("a gap in the sequence", gap))

    return cases


def main() -> None:
    good = _chain(3)
    head = (good[-1]["seq"], good[-1]["record_hash"])

    corpus = {
        "genesis": audit_chain.GENESIS_HASH,
        "chain": good,
        "canonical": audit_chain._canonical_payload(
            seq=1,
            ts=1_700_000_000.0,
            mono_ns=1000,
            action="session_create",
            principal="ada",
            session_id="sess-1",
            source_ip="203.0.113.7",
            detail={"z": 1, "a": {"nested": True}},
            prev_hash=audit_chain.GENESIS_HASH,
        ).decode("utf-8"),
        "verified": [
            {
                "name": "an empty log",
                "records": [],
                "expected_head": None,
                "result": _result(audit_chain.verify_records([])),
            },
            {
                "name": "a good chain",
                "records": good,
                "expected_head": None,
                "result": _result(audit_chain.verify_records(good)),
            },
            {
                "name": "a good chain with the head it should have",
                "records": good,
                "expected_head": list(head),
                "result": _result(audit_chain.verify_records(good, expected_head=head)),
            },
            {
                "name": "a good chain with the wrong head sequence",
                "records": good,
                "expected_head": [99, head[1]],
                "result": _result(audit_chain.verify_records(good, expected_head=(99, head[1]))),
            },
            {
                "name": "a good chain with the wrong head hash",
                "records": good,
                "expected_head": [head[0], "0" * 64],
                "result": _result(audit_chain.verify_records(good, expected_head=(head[0], "0" * 64))),
            },
            {
                "name": "the end cut off",
                "records": good[:2],
                "expected_head": list(head),
                "result": _result(audit_chain.verify_records(good[:2], expected_head=head)),
            },
            {
                "name": "an empty log with a head expected",
                "records": [],
                "expected_head": list(head),
                "result": _result(audit_chain.verify_records([], expected_head=head)),
            },
            *(
                {
                    "name": name,
                    "records": records,
                    "expected_head": None,
                    "result": _result(audit_chain.verify_records(records)),
                }
                for name, records in _tampered()
            ),
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['verified'])} verifications)")


if __name__ == "__main__":
    main()
