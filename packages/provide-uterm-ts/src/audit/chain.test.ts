//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type AuditRecord,
  canonicalPayload,
  computeRecordHash,
  GENESIS_HASH,
  verifyAuditLog,
  verifyRecords,
} from "./index.ts";

interface ChainGolden {
  genesis: string;
  chain: AuditRecord[];
  canonical: string;
  verified: Array<{
    name: string;
    records: AuditRecord[];
    expected_head: [number, string] | null;
    result: {
      ok: boolean;
      count: number;
      head_seq: number | null;
      head_hash: string | null;
      first_bad_seq: number | null;
      reason: string | null;
    };
  }>;
}

const golden = loadGolden<ChainGolden>("auditchain_golden.json");

/** The result in the shape the corpus records. */
function recorded(result: ReturnType<typeof verifyRecords>) {
  return {
    ok: result.ok,
    count: result.count,
    head_seq: result.head_seq ?? null,
    head_hash: result.head_hash ?? null,
    first_bad_seq: result.first_bad_seq ?? null,
    reason: result.reason ?? null,
  };
}

/** A copy, so a test that edits a record does not edit the corpus. */
function copyOf<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

describe("the bytes a record is hashed over", () => {
  it("serialises exactly as the reference does", () => {
    // Two runtimes hashing the same record have to agree byte for byte, or
    // every log written by one is tampered to the other.
    expect(
      canonicalPayload({
        seq: 1,
        ts: 1_700_000_000.0,
        mono_ns: 1000,
        action: "session_create",
        principal: "ada",
        session_id: "sess-1",
        source_ip: "203.0.113.7",
        detail: { z: 1, a: { nested: true } },
        prev_hash: GENESIS_HASH,
      }),
    ).toBe(golden.canonical);
  });

  it("serialises a timestamp that is not a number as it stands", () => {
    // A record written wrong still has to hash to something stable, or it
    // could never be shown to have been altered afterwards.
    const record = { ...(golden.chain[0] as AuditRecord), ts: "not a number" };
    expect(canonicalPayload(record)).toContain('"ts":"not a number"');
    expect(canonicalPayload(record)).toBe(canonicalPayload(record));
  });

  it("sorts the keys, however they were given", () => {
    // A record is a mapping; the order it was built in is not part of it.
    const record = golden.chain[0] as AuditRecord;
    const shuffled = Object.fromEntries([...Object.entries(record)].reverse());
    expect(canonicalPayload(shuffled)).toBe(canonicalPayload(record));
  });

  it("leaves the record's own hash out of it", () => {
    // It cannot be inside what it is a hash of.
    expect(canonicalPayload(golden.chain[0] as AuditRecord)).not.toContain("record_hash");
  });

  it("takes the link in, which is what makes it a chain", () => {
    expect(canonicalPayload(golden.chain[1] as AuditRecord)).toContain("prev_hash");
  });

  it("leaves anything outside ASCII as itself", () => {
    // Escaping it would change the bytes, and the digest with them — a log
    // written by the reference would read as tampered.
    const record = golden.chain.find((entry) => String(entry.principal).includes("☃")) as AuditRecord;
    expect(canonicalPayload(record)).toContain("☃");
    expect(canonicalPayload(record)).not.toContain("\\u");
    expect(computeRecordHash(canonicalPayload(record))).toBe(record.record_hash);
  });

  it("writes it compactly, with no spaces to differ over", () => {
    expect(golden.canonical).not.toContain(", ");
    expect(golden.canonical).not.toContain(": ");
  });

  it("hashes to what the chain says it does", () => {
    for (const record of golden.chain) {
      expect(computeRecordHash(canonicalPayload(record))).toBe(record.record_hash);
    }
  });

  it("starts from a hash of nothing", () => {
    expect(GENESIS_HASH).toBe(golden.genesis);
    expect(GENESIS_HASH).toBe("0".repeat(64));
    expect(golden.chain[0]?.prev_hash).toBe(GENESIS_HASH);
  });

  it("links each record to the one before it", () => {
    for (let index = 1; index < golden.chain.length; index += 1) {
      expect(golden.chain[index]?.prev_hash).toBe(golden.chain[index - 1]?.record_hash);
    }
  });
});

describe("verifying a chain", () => {
  it.each(golden.verified)("$name", (record) => {
    const result = verifyRecords(
      copyOf(record.records),
      record.expected_head === null ? {} : { expectedHead: record.expected_head },
    );
    expect(recorded(result)).toEqual(record.result);
  });

  it("accepts a log nobody has touched", () => {
    expect(verifyRecords(copyOf(golden.chain)).ok).toBe(true);
  });

  it("catches a single character changed anywhere in a record", () => {
    // Which is the whole point: there is no edit too small to break the hash.
    for (const field of ["action", "principal", "session_id", "source_ip"]) {
      const records = copyOf(golden.chain);
      const target = records[1] as AuditRecord;
      target[field] = `${String(target[field])}x`;
      const result = verifyRecords(records);
      expect(result.ok).toBe(false);
      expect(result.reason).toBe("record hash mismatch — content altered");
    }
  });

  it("catches a change buried in the detail", () => {
    const records = copyOf(golden.chain);
    ((records[1] as AuditRecord).detail as Record<string, unknown>).note = "edited";
    expect(verifyRecords(records).reason).toBe("record hash mismatch — content altered");
  });

  it("catches a record taken out", () => {
    const records = copyOf(golden.chain);
    records.splice(1, 1);
    expect(verifyRecords(records).reason).toBe("non-contiguous sequence");
  });

  it("catches two records swapped", () => {
    const records = copyOf(golden.chain);
    [records[1], records[2]] = [records[2] as AuditRecord, records[1] as AuditRecord];
    expect(verifyRecords(records).ok).toBe(false);
  });

  it("catches a link pointed somewhere else", () => {
    const records = copyOf(golden.chain);
    (records[2] as AuditRecord).prev_hash = GENESIS_HASH;
    expect(verifyRecords(records).reason).toBe("broken hash link");
  });

  it("catches the first record being unlinked from the start", () => {
    // A log that does not begin at the beginning is a log with its front
    // removed.
    const records = copyOf(golden.chain);
    (records[0] as AuditRecord).prev_hash = "f".repeat(64);
    const result = verifyRecords(records);
    expect(result.reason).toBe("broken hash link");
    expect(result.first_bad_seq).toBe(1);
  });

  it("stops at the first failure rather than the worst", () => {
    // An operator needs to know where the log stopped being trustworthy.
    const records = copyOf(golden.chain);
    (records[1] as AuditRecord).action = "edited";
    (records[2] as AuditRecord).action = "edited too";
    expect(verifyRecords(records).first_bad_seq).toBe(2);
  });

  it("refuses a record missing any field it needs", () => {
    for (const field of ["seq", "ts", "action", "prev_hash", "record_hash", "detail"]) {
      const records = copyOf(golden.chain);
      // biome-ignore lint/performance/noDelete: the absence is the test
      delete (records[1] as AuditRecord)[field];
      expect(verifyRecords(records).reason).toBe("malformed record");
    }
  });

  it("refuses a sequence number that is not one", () => {
    // Including a boolean, which is a number in more languages than it should
    // be — `true` must not pass for one.
    for (const seq of ["2", true, 2.5, null, [2]]) {
      const records = copyOf(golden.chain);
      (records[1] as AuditRecord).seq = seq;
      expect(verifyRecords(records).reason).toBe("malformed record");
    }
  });

  it("refuses something that is not a record at all", () => {
    for (const value of [null, 42, "a record", [1, 2]]) {
      expect(verifyRecords([value as unknown as AuditRecord]).reason).toBe("malformed record");
    }
  });

  it("accepts an empty log, and says it has no head", () => {
    // Nothing has been recorded, which is not the same as something having
    // been removed.
    expect(recorded(verifyRecords([]))).toEqual({
      ok: true,
      count: 0,
      head_seq: null,
      head_hash: null,
      first_bad_seq: null,
      reason: null,
    });
  });

  it("catches the end being cut off, which the log itself cannot show", () => {
    // A shortened chain is a perfectly valid chain; only the head somebody
    // remembers proves otherwise.
    const whole = copyOf(golden.chain);
    const head = [whole.at(-1)?.seq as number, whole.at(-1)?.record_hash as string] as const;
    expect(verifyRecords(whole.slice(0, 2)).ok).toBe(true);
    const result = verifyRecords(whole.slice(0, 2), { expectedHead: head });
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("head mismatch — log truncated or rolled back");
  });

  it("reports the head it actually found when it disagrees", () => {
    // So an operator can see how far back the log was rolled.
    const whole = copyOf(golden.chain);
    const head = [whole.at(-1)?.seq as number, whole.at(-1)?.record_hash as string] as const;
    const result = verifyRecords(whole.slice(0, 2), { expectedHead: head });
    expect(result.head_seq).toBe(2);
    expect(result.head_hash).toBe(whole[1]?.record_hash);
  });

  it("takes a head that matches", () => {
    const whole = copyOf(golden.chain);
    const head = [whole.at(-1)?.seq as number, whole.at(-1)?.record_hash as string] as const;
    expect(verifyRecords(whole, { expectedHead: head }).ok).toBe(true);
  });

  it("takes a chain starting anywhere, so long as it is contiguous", () => {
    // A log that has been rotated does not start at one.
    const records = copyOf(golden.chain).slice(1);
    (records[0] as AuditRecord).prev_hash = golden.chain[0]?.record_hash as string;
    (records[0] as AuditRecord).record_hash = computeRecordHash(canonicalPayload(records[0] as AuditRecord));
    (records[1] as AuditRecord).prev_hash = (records[0] as AuditRecord).record_hash as string;
    (records[1] as AuditRecord).record_hash = computeRecordHash(canonicalPayload(records[1] as AuditRecord));
    expect(verifyRecords(records, { genesis: golden.chain[0]?.record_hash as string }).ok).toBe(true);
  });
});

describe("verifying a log as it was written", () => {
  /** The corpus's chain, one JSON record per line. */
  const written = `${golden.chain.map((record) => JSON.stringify(record)).join("\n")}\n`;

  it("reads a log written one record per line", () => {
    expect(verifyAuditLog(written).ok).toBe(true);
  });

  it("skips blank lines", () => {
    // A file that has been appended to unevenly is not a tampered file.
    expect(verifyAuditLog(`\n${written}\n\n`).ok).toBe(true);
  });

  it("names the line it could not read", () => {
    // "Somewhere in this file" is not something an operator can act on.
    const lines = written.trimEnd().split("\n");
    lines.splice(1, 0, "{not json");
    const result = verifyAuditLog(lines.join("\n"));
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("unparseable line 2");
    expect(result.first_bad_seq).toBe(2);
  });

  it("counts lines from one, as somebody reading the file would", () => {
    const result = verifyAuditLog("\n\nnot json\n");
    expect(result.reason).toBe("unparseable line 3");
  });

  it("says so when there is no log at all", () => {
    // Reported rather than raised: a missing log is an answer.
    const result = verifyAuditLog(undefined);
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("audit log not found");
    expect(result.count).toBe(0);
  });

  it("accepts a log that is there and empty", () => {
    expect(verifyAuditLog("").ok).toBe(true);
    expect(verifyAuditLog("\n\n").ok).toBe(true);
  });

  it("catches tampering in a written log too", () => {
    const tampered = written.replace('"principal":"ada"', '"principal":"eve"');
    const result = verifyAuditLog(tampered);
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("record hash mismatch — content altered");
  });

  it("checks the head of a written log", () => {
    const head = [golden.chain.at(-1)?.seq as number, golden.chain.at(-1)?.record_hash as string] as const;
    expect(verifyAuditLog(written, { expectedHead: head }).ok).toBe(true);
    const truncated = `${golden.chain
      .slice(0, 2)
      .map((record) => JSON.stringify(record))
      .join("\n")}\n`;
    expect(verifyAuditLog(truncated, { expectedHead: head }).ok).toBe(false);
  });

  it("reads a log written with carriage returns", () => {
    // Both endings: a lone carriage return is a line break too, and splitting
    // only on newlines would read a whole file as one unparseable line.
    expect(verifyAuditLog(written.replaceAll("\n", "\r\n")).ok).toBe(true);
    expect(verifyAuditLog(written.replaceAll("\n", "\r")).ok).toBe(true);
  });
});
