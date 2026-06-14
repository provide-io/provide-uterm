#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tamper-evident WORM audit chain primitive + verifier.

Each appended record carries a monotonic sequence number, a wall-clock and a
monotonic timestamp, and a ``prev_hash`` linking it to the previous record's
``record_hash`` (sha256 over the record's canonical payload). Because every
record's hash covers its predecessor's hash, *any* insertion, deletion,
reordering, or content modification breaks the chain and is detectable by the
verifier.

This is tamper-*evident*, not tamper-*proof*: a writer with the file can still
rewrite the whole log. True immutability requires an append-only sink + an
externally anchored head; that wiring (and cross-instance anchoring) is the
subject of a later sub-task. This module provides only the chain primitive,
the 0600 append-only file sink, and the verification routines.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# The prev_hash of the very first record in a chain (no predecessor). 64 hex
# zeros so it is the same width as a real sha256 digest.
GENESIS_HASH = "0" * 64

# Keys required on a record dict for verification. Mirrors AuditRecord fields.
_RECORD_KEYS = (
    "seq",
    "ts",
    "mono_ns",
    "action",
    "principal",
    "session_id",
    "source_ip",
    "detail",
    "prev_hash",
    "record_hash",
)


def _canonical_payload(
    *,
    seq: int,
    ts: float,
    mono_ns: int,
    action: str,
    principal: str,
    session_id: str,
    source_ip: str,
    detail: dict[str, Any],
    prev_hash: str,
) -> bytes:
    """Deterministic serialization of every field EXCEPT the record's own hash.

    This is THE canonical form: ``compute_record_hash`` digests it, and the
    verifier recomputes it identically from a record's fields. ``prev_hash`` is
    included, so each record's hash chains onto its predecessor.

    ``default=str`` guarantees an exotic value buried in ``detail`` (a set, a
    custom object, ...) can never raise during an audit append — auditing must
    never crash the action it records.
    """
    return json.dumps(
        {
            "seq": seq,
            "ts": ts,
            "mono_ns": mono_ns,
            "action": action,
            "principal": principal,
            "session_id": session_id,
            "source_ip": source_ip,
            "detail": detail,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def compute_record_hash(payload: bytes) -> str:
    """Return the sha256 hex digest of a canonical payload.

    sha256 is used as an integrity/linking hash, not for password storage.
    """
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One immutable record in the audit chain."""

    seq: int
    ts: float
    mono_ns: int
    action: str
    principal: str
    session_id: str
    source_ip: str
    detail: dict[str, Any]
    prev_hash: str
    record_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Return the plain dict written as one JSON line (includes record_hash)."""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "mono_ns": self.mono_ns,
            "action": self.action,
            "principal": self.principal,
            "session_id": self.session_id,
            "source_ip": self.source_ip,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "record_hash": self.record_hash,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuditRecord:
        """Rebuild an AuditRecord from a parsed JSON line."""
        return cls(
            seq=d["seq"],
            ts=d["ts"],
            mono_ns=d["mono_ns"],
            action=d["action"],
            principal=d["principal"],
            session_id=d["session_id"],
            source_ip=d["source_ip"],
            detail=d["detail"],
            prev_hash=d["prev_hash"],
            record_hash=d["record_hash"],
        )


class AuditChain:
    """Append-only, hash-chained audit log backed by a 0600 file."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        seq: int = 0,
        last_hash: str = GENESIS_HASH,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], int] = time.monotonic_ns,
        on_head: Callable[[int, str], None] | None = None,
    ) -> None:
        """Create a chain writer.

        Parameters
        ----------
        path:
            Destination JSONL file (created 0600 if absent, tightened to 0600
            if it pre-existed with looser perms).
        seq / last_hash:
            Resume state — the head sequence number and hash to continue from.
        clock / mono:
            Injectable wall-clock and monotonic clocks (for deterministic tests).
        on_head:
            Optional callback invoked ``(seq, record_hash)`` after each append;
            a later sub-task passes the control-plane head-persist writer here.
        """
        self._path = os.fspath(path)
        self._seq = seq
        self._last_hash = last_hash
        self._clock = clock
        self._mono = mono
        self._on_head = on_head
        self._lock = threading.Lock()

    @property
    def seq(self) -> int:
        """Current head sequence number."""
        return self._seq

    @property
    def last_hash(self) -> str:
        """Current head record hash."""
        return self._last_hash

    def _write_line(self, line: str) -> None:
        """Append one line to the 0600 file, durably.

        Open-per-append keeps the implementation simple and safe: O_APPEND makes
        each write land atomically at end-of-file even with concurrent writers,
        and the in-process lock keeps seq/last_hash consistent. fsync ensures the
        record reaches disk before the audited action is acknowledged.
        """
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            # Enforce 0600 even if the file pre-existed with looser perms — an
            # audit log must never be world-readable. fchmod targets the open fd
            # (no TOCTOU on the path).
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def append(
        self,
        action: str,
        *,
        principal: str = "",
        session_id: str = "",
        source_ip: str = "",
        detail: dict[str, Any] | None = None,
    ) -> AuditRecord:
        """Append a new audit record, returning it.

        Field names match :func:`provide.uterm.server.audit.audit_event` so a
        later sub-task can wire the two 1:1.
        """
        with self._lock:
            seq = self._seq + 1
            ts = self._clock()
            mono_ns = self._mono()
            detail_d = detail or {}
            prev_hash = self._last_hash
            payload = _canonical_payload(
                seq=seq,
                ts=ts,
                mono_ns=mono_ns,
                action=action,
                principal=principal,
                session_id=session_id,
                source_ip=source_ip,
                detail=detail_d,
                prev_hash=prev_hash,
            )
            record_hash = compute_record_hash(payload)
            record = AuditRecord(
                seq=seq,
                ts=ts,
                mono_ns=mono_ns,
                action=action,
                principal=principal,
                session_id=session_id,
                source_ip=source_ip,
                detail=detail_d,
                prev_hash=prev_hash,
                record_hash=record_hash,
            )
            line = json.dumps(record.to_dict(), separators=(",", ":"), ensure_ascii=False, default=str) + "\n"
            self._write_line(line)
            self._seq = seq
            self._last_hash = record_hash
            if self._on_head is not None:
                self._on_head(seq, record_hash)
            return record

    def anchor(self) -> AuditRecord:
        """Append a checkpoint record snapshotting the head before this record.

        A periodic anchor lets an external notary countersign the head;
        cross-instance/external anchoring is deferred to the HA design.
        """
        anchored_seq = self._seq
        anchored_hash = self._last_hash
        return self.append(
            "audit.anchor",
            detail={"anchored_seq": anchored_seq, "anchored_hash": anchored_hash},
        )

    def close(self) -> None:
        """No-op close (open-per-append keeps no long-lived fd); present for API symmetry."""


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of verifying an audit chain."""

    ok: bool
    count: int
    head_seq: int | None
    head_hash: str | None
    first_bad_seq: int | None
    reason: str | None


def verify_records(
    records: Iterable[dict[str, Any]],
    *,
    genesis: str = GENESIS_HASH,
    expected_head: tuple[int, str] | None = None,
) -> VerifyResult:
    """Verify a sequence of record dicts forms an unbroken hash chain.

    The first record establishes the starting sequence number; thereafter the
    sequence must strictly increment by 1. Each record's ``prev_hash`` must
    equal the running hash, and its ``record_hash`` must equal the recomputed
    canonical hash of its own fields. On the FIRST failure a non-ok result is
    returned (never raised). If ``expected_head`` is supplied, the final
    ``(seq, hash)`` must equal it (catches end-truncation / rollback).
    """
    prev = genesis
    expected_seq: int | None = None
    count = 0
    last_seq: int | None = None
    last_hash: str | None = None

    for record in records:
        count += 1
        try:
            for key in _RECORD_KEYS:
                if key not in record:
                    return VerifyResult(False, count, None, None, _seq_of(record), "malformed record")
            seq = record["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool):
                return VerifyResult(False, count, None, None, _seq_of(record), "malformed record")
        except TypeError:
            return VerifyResult(False, count, None, None, None, "malformed record")

        if expected_seq is None:
            expected_seq = seq
        if seq != expected_seq:
            return VerifyResult(False, count, None, None, seq, "non-contiguous sequence")
        if record["prev_hash"] != prev:
            return VerifyResult(False, count, None, None, seq, "broken hash link")

        payload = _canonical_payload(
            seq=seq,
            ts=record["ts"],
            mono_ns=record["mono_ns"],
            action=record["action"],
            principal=record["principal"],
            session_id=record["session_id"],
            source_ip=record["source_ip"],
            detail=record["detail"],
            prev_hash=record["prev_hash"],
        )
        if compute_record_hash(payload) != record["record_hash"]:
            return VerifyResult(False, count, None, None, seq, "record hash mismatch — content altered")

        prev = record["record_hash"]
        last_seq = seq
        last_hash = record["record_hash"]
        expected_seq += 1

    if expected_head is not None and (last_seq, last_hash) != expected_head:
        return VerifyResult(False, count, last_seq, last_hash, last_seq, "head mismatch — log truncated or rolled back")

    return VerifyResult(True, count, last_seq, last_hash, None, None)


def _seq_of(record: Any) -> int | None:
    """Best-effort extraction of a record's seq for error reporting."""
    try:
        value = record.get("seq")
    except AttributeError:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def verify_audit_log(
    path: str | os.PathLike[str],
    *,
    expected_head: tuple[int, str] | None = None,
) -> VerifyResult:
    """Verify an on-disk JSONL audit log.

    Reads the file line by line, skipping blank lines; a JSON-decode failure on
    a non-blank line is reported as ``unparseable line N`` (1-based). A missing
    file is reported rather than raised.
    """
    try:
        with Path(path).open(encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except FileNotFoundError:
        return VerifyResult(False, 0, None, None, None, "audit log not found")

    records: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            return VerifyResult(False, len(records), None, None, index, f"unparseable line {index}")

    return verify_records(records, expected_head=expected_head)
