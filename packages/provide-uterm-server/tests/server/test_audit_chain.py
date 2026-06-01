#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the tamper-evident WORM audit chain primitive + verifier."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from provide.uterm.server.audit_chain import (
    GENESIS_HASH,
    AuditChain,
    AuditRecord,
    VerifyResult,
    _canonical_payload,
    compute_record_hash,
    verify_audit_log,
    verify_records,
)


def _read_lines(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL audit file into a list of record dicts."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _fixed_chain(path: Path) -> AuditChain:
    """Build an AuditChain with deterministic injected clocks."""
    counter = {"mono": 0}

    def _mono() -> int:
        counter["mono"] += 1
        return counter["mono"]

    return AuditChain(path, clock=lambda: 1700000000.0, mono=_mono)


# ---------------------------------------------------------------------------
# Append + chain construction
# ---------------------------------------------------------------------------


class TestAppend:
    def test_append_builds_valid_chain(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("session.create", principal="alice")
        chain.append("session.join", principal="bob")
        chain.append("session.close", principal="alice")

        records = _read_lines(path)
        assert [r["seq"] for r in records] == [1, 2, 3]
        assert records[0]["prev_hash"] == GENESIS_HASH
        assert records[1]["prev_hash"] == records[0]["record_hash"]
        assert records[2]["prev_hash"] == records[1]["record_hash"]

    def test_file_mode_is_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("session.create")
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_chmod_tightens_preexisting_loose_perms(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        path.touch(mode=0o644)
        path.chmod(0o644)
        chain = _fixed_chain(path)
        chain.append("session.create")
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_append_returns_record(self, tmp_path: Path) -> None:
        chain = _fixed_chain(tmp_path / "audit.log")
        rec = chain.append("x", principal="p", session_id="s", source_ip="1.2.3.4", detail={"k": 1})
        assert isinstance(rec, AuditRecord)
        assert rec.seq == 1
        assert rec.action == "x"
        assert rec.principal == "p"
        assert rec.session_id == "s"
        assert rec.source_ip == "1.2.3.4"
        assert rec.detail == {"k": 1}
        assert rec.prev_hash == GENESIS_HASH

    def test_seq_and_last_hash_properties(self, tmp_path: Path) -> None:
        chain = _fixed_chain(tmp_path / "audit.log")
        assert chain.seq == 0
        assert chain.last_hash == GENESIS_HASH
        rec = chain.append("a")
        assert chain.seq == 1
        assert chain.last_hash == rec.record_hash

    def test_seq_and_last_hash_seed(self, tmp_path: Path) -> None:
        chain = AuditChain(tmp_path / "audit.log", seq=10, last_hash="a" * 64)
        assert chain.seq == 10
        assert chain.last_hash == "a" * 64

    def test_close(self, tmp_path: Path) -> None:
        chain = _fixed_chain(tmp_path / "audit.log")
        chain.append("a")
        chain.close()  # must be safe even if no fd kept


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestHashing:
    def test_deterministic_same_inputs(self, tmp_path: Path) -> None:
        a = _fixed_chain(tmp_path / "a.log").append("act", principal="p", detail={"x": 1})
        b = _fixed_chain(tmp_path / "b.log").append("act", principal="p", detail={"x": 1})
        assert a.record_hash == b.record_hash

    def test_changing_a_field_changes_hash(self, tmp_path: Path) -> None:
        a = _fixed_chain(tmp_path / "a.log").append("act", principal="p")
        b = _fixed_chain(tmp_path / "b.log").append("act", principal="DIFFERENT")
        assert a.record_hash != b.record_hash

    def test_compute_record_hash_is_sha256_hex(self) -> None:
        h = compute_record_hash(b"hello")
        assert len(h) == 64
        assert h == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    def test_canonical_payload_is_sorted_compact(self) -> None:
        payload = _canonical_payload(
            seq=1,
            ts=1.0,
            mono_ns=2,
            action="a",
            principal="p",
            session_id="s",
            source_ip="i",
            detail={"b": 1, "a": 2},
            prev_hash=GENESIS_HASH,
        )
        # sorted keys, compact separators, detail keys sorted too
        assert b'"a":2,"b":1' in payload
        assert b", " not in payload  # compact separators


# ---------------------------------------------------------------------------
# AuditRecord round-trip
# ---------------------------------------------------------------------------


class TestAuditRecord:
    def test_to_dict_from_dict_roundtrip(self, tmp_path: Path) -> None:
        rec = _fixed_chain(tmp_path / "a.log").append("a", principal="p", detail={"x": 1})
        d = rec.to_dict()
        assert d["record_hash"] == rec.record_hash
        rebuilt = AuditRecord.from_dict(d)
        assert rebuilt == rec


# ---------------------------------------------------------------------------
# verify_audit_log — happy path
# ---------------------------------------------------------------------------


class TestVerifyHappy:
    def test_untampered_log_ok(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("a")
        chain.append("b")
        chain.append("c")
        result = verify_audit_log(path)
        assert isinstance(result, VerifyResult)
        assert result.ok is True
        assert result.count == 3
        assert result.head_seq == 3
        assert result.head_hash == chain.last_hash
        assert result.first_bad_seq is None
        assert result.reason is None

    def test_expected_head_match(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("a")
        chain.append("b")
        result = verify_audit_log(path, expected_head=(2, chain.last_hash))
        assert result.ok is True

    def test_empty_records_ok(self) -> None:
        result = verify_records([])
        assert result.ok is True
        assert result.count == 0
        assert result.head_seq is None
        assert result.head_hash is None

    def test_empty_records_with_expected_head_fails(self) -> None:
        result = verify_records([], expected_head=(1, "x" * 64))
        assert result.ok is False
        assert "head" in (result.reason or "")


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


class TestTamper:
    def _three_record_file(self, tmp_path: Path) -> tuple[Path, AuditChain]:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("a", principal="alice")
        chain.append("b", principal="bob")
        chain.append("c", principal="carol")
        return path, chain

    def test_content_modification_caught(self, tmp_path: Path) -> None:
        path, _ = self._three_record_file(tmp_path)
        records = _read_lines(path)
        records[1]["action"] = "TAMPERED"  # alter content, leave hash
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        result = verify_audit_log(path)
        assert result.ok is False
        assert result.first_bad_seq == 2
        assert "record hash mismatch" in (result.reason or "")

    def test_detail_modification_caught(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("a", detail={"amount": 1})
        chain.append("b", detail={"amount": 2})
        records = _read_lines(path)
        records[0]["detail"] = {"amount": 9999}
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        result = verify_audit_log(path)
        assert result.ok is False
        assert result.first_bad_seq == 1
        assert "record hash mismatch" in (result.reason or "")

    def test_deleted_middle_line_caught(self, tmp_path: Path) -> None:
        path, _ = self._three_record_file(tmp_path)
        records = _read_lines(path)
        del records[1]  # remove seq 2
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        result = verify_audit_log(path)
        assert result.ok is False
        assert result.first_bad_seq == 3
        assert result.reason in ("non-contiguous sequence", "broken hash link")

    def test_reordered_lines_caught(self, tmp_path: Path) -> None:
        path, _ = self._three_record_file(tmp_path)
        records = _read_lines(path)
        records[0], records[1] = records[1], records[0]  # swap seq 1 and 2
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        result = verify_audit_log(path)
        assert result.ok is False

    def test_flipped_prev_hash_caught(self, tmp_path: Path) -> None:
        path, _ = self._three_record_file(tmp_path)
        records = _read_lines(path)
        ph = records[2]["prev_hash"]
        flipped = ("f" if ph[0] != "f" else "0") + ph[1:]
        records[2]["prev_hash"] = flipped
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        result = verify_audit_log(path)
        assert result.ok is False
        assert result.first_bad_seq == 3
        assert result.reason == "broken hash link"

    def test_truncated_tail_with_expected_head_caught(self, tmp_path: Path) -> None:
        path, chain = self._three_record_file(tmp_path)
        original_head = (chain.seq, chain.last_hash)
        records = _read_lines(path)
        del records[-1]  # truncate last record
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        result = verify_audit_log(path, expected_head=original_head)
        assert result.ok is False
        assert "head" in (result.reason or "")

    def test_first_seq_not_one_is_allowed_then_increments(self, tmp_path: Path) -> None:
        # A log that begins mid-stream (seq=5) is fine as long as it's contiguous from there.
        chain = AuditChain(tmp_path / "audit.log", seq=4, clock=lambda: 1.0, mono=lambda: 1)
        chain.append("a")
        chain.append("b")
        result = verify_audit_log(tmp_path / "audit.log")
        assert result.ok is True
        assert result.head_seq == 6

    def test_non_contiguous_within_records(self) -> None:
        # craft records where seq jumps; prev links fine via recompute
        recs = []
        prev = GENESIS_HASH
        for seq in (1, 3):
            payload = _canonical_payload(
                seq=seq, ts=1.0, mono_ns=1, action="a", principal="", session_id="",
                source_ip="", detail={}, prev_hash=prev,
            )
            rh = compute_record_hash(payload)
            recs.append(
                {"seq": seq, "ts": 1.0, "mono_ns": 1, "action": "a", "principal": "",
                 "session_id": "", "source_ip": "", "detail": {}, "prev_hash": prev, "record_hash": rh}
            )
            prev = rh
        result = verify_records(recs)
        assert result.ok is False
        assert result.first_bad_seq == 3
        assert result.reason == "non-contiguous sequence"

    def test_malformed_record_missing_key(self) -> None:
        result = verify_records([{"seq": 1}])
        assert result.ok is False
        assert result.first_bad_seq == 1
        assert result.reason == "malformed record"

    def test_malformed_record_wrong_type(self) -> None:
        rec = {
            "seq": "not-an-int", "ts": 1.0, "mono_ns": 1, "action": "a", "principal": "",
            "session_id": "", "source_ip": "", "detail": {}, "prev_hash": GENESIS_HASH,
            "record_hash": "x",
        }
        result = verify_records([rec])
        assert result.ok is False
        assert result.reason == "malformed record"

    def test_malformed_record_bool_seq(self) -> None:
        rec = {
            "seq": True, "ts": 1.0, "mono_ns": 1, "action": "a", "principal": "",
            "session_id": "", "source_ip": "", "detail": {}, "prev_hash": GENESIS_HASH,
            "record_hash": "x",
        }
        result = verify_records([rec])
        assert result.ok is False
        assert result.reason == "malformed record"

    def test_non_mapping_record_int(self) -> None:
        # A JSON line that decodes to a scalar (not a dict) — `key in 42` raises
        # TypeError and `42.get(...)` raises AttributeError; both defensive
        # paths must report "malformed record" rather than crash.
        result = verify_records([42])  # type: ignore[list-item]
        assert result.ok is False
        assert result.first_bad_seq is None
        assert result.reason == "malformed record"

    def test_non_mapping_record_list(self) -> None:
        # A list supports `in` (so no TypeError on key check) but lacks `.get`;
        # the key-presence check fails first → malformed, and _seq_of returns None.
        result = verify_records([["seq", 1]])  # type: ignore[list-item]
        assert result.ok is False
        assert result.first_bad_seq is None
        assert result.reason == "malformed record"


# ---------------------------------------------------------------------------
# anchor()
# ---------------------------------------------------------------------------


class TestAnchor:
    def test_anchor_snapshots_prior_head(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("a")
        rec2 = chain.append("b")
        anchor = chain.anchor()
        assert anchor.action == "audit.anchor"
        assert anchor.detail == {"anchored_seq": 2, "anchored_hash": rec2.record_hash}
        assert anchor.seq == 3

    def test_chain_valid_through_and_after_anchor(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("a")
        chain.anchor()
        chain.append("c")
        result = verify_audit_log(path)
        assert result.ok is True
        assert result.head_seq == 3


# ---------------------------------------------------------------------------
# default=str resilience
# ---------------------------------------------------------------------------


class TestExoticDetail:
    def test_set_in_detail_does_not_crash(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("a", detail={"roles": {"admin", "viewer"}})
        result = verify_audit_log(path)
        assert result.ok is True

    def test_object_in_detail_does_not_crash(self, tmp_path: Path) -> None:
        class Weird:
            def __str__(self) -> str:
                return "weird-obj"

        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("a", detail={"obj": Weird()})
        result = verify_audit_log(path)
        assert result.ok is True


# ---------------------------------------------------------------------------
# verify_audit_log — file-level errors
# ---------------------------------------------------------------------------


class TestFileErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        result = verify_audit_log(tmp_path / "does-not-exist.log")
        assert result.ok is False
        assert result.reason == "audit log not found"

    def test_unparseable_line(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        path.write_text("{not json}\n", encoding="utf-8")
        result = verify_audit_log(path)
        assert result.ok is False
        assert result.first_bad_seq == 1
        assert "unparseable line 1" in (result.reason or "")

    def test_blank_trailing_lines_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.log"
        chain = _fixed_chain(path)
        chain.append("a")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n\n")
        result = verify_audit_log(path)
        assert result.ok is True
        assert result.count == 1


# ---------------------------------------------------------------------------
# on_head callback
# ---------------------------------------------------------------------------


class TestOnHead:
    def test_on_head_fires_after_each_append(self, tmp_path: Path) -> None:
        seen: list[tuple[int, str]] = []
        chain = AuditChain(
            tmp_path / "audit.log",
            clock=lambda: 1.0,
            mono=lambda: 1,
            on_head=lambda seq, h: seen.append((seq, h)),
        )
        r1 = chain.append("a")
        r2 = chain.append("b")
        assert seen == [(1, r1.record_hash), (2, r2.record_hash)]

    def test_on_head_none_is_fine(self, tmp_path: Path) -> None:
        chain = AuditChain(tmp_path / "audit.log", clock=lambda: 1.0, mono=lambda: 1)
        chain.append("a")  # no callback, no crash


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-vv"])
