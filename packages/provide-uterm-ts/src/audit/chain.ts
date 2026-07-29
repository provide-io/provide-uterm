//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * A tamper-evident audit chain.
 *
 * Port of `provide.uterm.server.audit_chain`'s verifying half. An audit log is
 * the record of who did what, so the only thing that makes it worth keeping is
 * that it can be shown not to have been edited. Each record hashes its own
 * contents together with the hash of the one before it, so a change anywhere
 * breaks every link after it.
 *
 * What that catches:
 *
 * * **A record altered** — its own hash no longer matches its contents.
 * * **A record removed, inserted or reordered** — the sequence stops being
 *   contiguous, or a link no longer points at the record before it.
 * * **The end cut off** — which nothing *inside* the log can show, since a
 *   shortened chain is perfectly valid. The caller supplies the head it
 *   expects, and a log ending anywhere else was rolled back.
 */

import { createHash } from "node:crypto";
import { asPyFloat, pyJsonDumps } from "../pycompat/json.ts";

/** What the first record links to: a hash of nothing. */
export const GENESIS_HASH = "0".repeat(64);

/** The fields every record must carry. */
const RECORD_KEYS = [
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
] as const;

/** One record, as it was written. */
export type AuditRecord = Record<string, unknown>;

/** Why a chain was refused. */
export type VerifyReason =
  | "malformed record"
  | "non-contiguous sequence"
  | "broken hash link"
  | "record hash mismatch — content altered"
  | "head mismatch — log truncated or rolled back";

/** What verifying a chain found. */
export interface VerifyResult {
  ok: boolean;
  /** How many records were read, including the one that failed. */
  count: number;
  head_seq: number | undefined;
  head_hash: string | undefined;
  /** The sequence number of the first record that failed, where one is readable. */
  first_bad_seq: number | undefined;
  reason: VerifyReason | undefined;
}

/**
 * The canonical bytes a record's hash is taken over.
 *
 * Everything except the record's own hash, with the keys sorted and no
 * whitespace, so two runtimes hashing the same record agree byte for byte.
 * `prev_hash` is part of it, which is what chains each record onto the last.
 */
export function canonicalPayload(record: AuditRecord): string {
  return pyJsonDumps(
    {
      seq: record.seq,
      // A `float` in the reference, and integral more often than not — so
      // without saying so it would render as an int here and the digests
      // would disagree across runtimes.
      ts: typeof record.ts === "number" ? asPyFloat(record.ts) : record.ts,
      mono_ns: record.mono_ns,
      action: record.action,
      principal: record.principal,
      session_id: record.session_id,
      source_ip: record.source_ip,
      detail: record.detail,
      prev_hash: record.prev_hash,
    },
    // Sorted and compact, and non-ASCII left as itself — the reference's
    // `json.dumps(..., sort_keys=True, separators=(",", ":"),
    // ensure_ascii=False)`.
    { sortKeys: true, ensureAscii: false, separators: [",", ":"] },
  );
}

/** The digest of a canonical payload. Linking, not secrecy. */
export function computeRecordHash(payload: string): string {
  return createHash("sha256").update(payload, "utf8").digest("hex");
}

/** A whole number, which a sequence has to be — and not a boolean. */
function isSequence(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

/**
 * A record's sequence number, for saying where a failure was.
 *
 * Only ever asked of something already known to be an object — a record that
 * is not one is refused before there is anywhere to look.
 */
function seqOf(record: AuditRecord): number | undefined {
  return isSequence(record.seq) ? record.seq : undefined;
}

/** A refusal, naming where and why. */
function refuse(count: number, seq: number | undefined, reason: VerifyReason): VerifyResult {
  return { ok: false, count, head_seq: undefined, head_hash: undefined, first_bad_seq: seq, reason };
}

/**
 * Verify that a run of records forms an unbroken chain.
 *
 * The first record sets the starting sequence number; after that it must
 * increment by one each time. Stops at the first failure and reports it rather
 * than raising — a tampered log is an answer, not an error.
 */
export function verifyRecords(
  records: Iterable<AuditRecord>,
  options: { genesis?: string; expectedHead?: readonly [number, string] } = {},
): VerifyResult {
  let previous = options.genesis ?? GENESIS_HASH;
  let expectedSeq: number | undefined;
  let count = 0;
  let lastSeq: number | undefined;
  let lastHash: string | undefined;

  for (const record of records) {
    count += 1;
    if (typeof record !== "object" || record === null) {
      return refuse(count, undefined, "malformed record");
    }
    for (const key of RECORD_KEYS) {
      if (!Object.hasOwn(record, key)) {
        return refuse(count, seqOf(record), "malformed record");
      }
    }
    const seq = record.seq;
    if (!isSequence(seq)) {
      // Reported without a sequence number, because the one it carries is not
      // one — saying "at seq 2" of a record whose seq is the string "2" would
      // be repeating the tampering back.
      return refuse(count, seqOf(record), "malformed record");
    }

    expectedSeq ??= seq;
    if (seq !== expectedSeq) {
      return refuse(count, seq, "non-contiguous sequence");
    }
    if (record.prev_hash !== previous) {
      return refuse(count, seq, "broken hash link");
    }
    if (computeRecordHash(canonicalPayload(record)) !== record.record_hash) {
      return refuse(count, seq, "record hash mismatch — content altered");
    }

    previous = record.record_hash as string;
    lastSeq = seq;
    lastHash = record.record_hash as string;
    expectedSeq += 1;
  }

  if (options.expectedHead !== undefined) {
    const [wantSeq, wantHash] = options.expectedHead;
    if (lastSeq !== wantSeq || lastHash !== wantHash) {
      // The only tampering the log itself cannot show: a chain with its end
      // cut off is a perfectly valid chain.
      return {
        ok: false,
        count,
        head_seq: lastSeq,
        head_hash: lastHash,
        first_bad_seq: lastSeq,
        reason: "head mismatch — log truncated or rolled back",
      };
    }
  }

  return { ok: true, count, head_seq: lastSeq, head_hash: lastHash, first_bad_seq: undefined, reason: undefined };
}

/** What verifying a written log found, including one that will not parse. */
export interface VerifyLogResult extends Omit<VerifyResult, "reason"> {
  reason: VerifyReason | string | undefined;
}

/**
 * Verify a log as it was written: one JSON record per line.
 *
 * A blank line is skipped; a line that will not parse is named by its number,
 * because "somewhere in this file" is not something an operator can act on.
 * A missing file is reported rather than raised.
 */
export function verifyAuditLog(
  contents: string | undefined,
  options: { expectedHead?: readonly [number, string] } = {},
): VerifyLogResult {
  if (contents === undefined) {
    return {
      ok: false,
      count: 0,
      head_seq: undefined,
      head_hash: undefined,
      first_bad_seq: undefined,
      reason: "audit log not found",
    };
  }

  const records: AuditRecord[] = [];
  const lines = contents.split(/\r\n|\r|\n/);
  for (const [index, raw] of lines.entries()) {
    const line = raw.trim();
    if (line === "") {
      continue;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      // Numbered from one, as an operator would count them.
      return {
        ok: false,
        count: records.length,
        head_seq: undefined,
        head_hash: undefined,
        first_bad_seq: index + 1,
        reason: `unparseable line ${index + 1}`,
      };
    }
    records.push(parsed as AuditRecord);
  }

  return verifyRecords(records, options.expectedHead === undefined ? {} : { expectedHead: options.expectedHead });
}
