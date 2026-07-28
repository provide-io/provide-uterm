//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Rebuild a raw terminal byte stream from a session log.
 *
 * Port of the Python module `provide.uterm.replay.raw`.
 *
 * The rebuilt stream is fed back through an emulator, so a dropped or
 * reordered chunk is a different session. That is why this refuses a line it
 * cannot read rather than skipping it, where the viewer skips: a silently
 * short stream is a silently different session.
 */

import { readFileSync, writeFileSync } from "node:fs";

/** One record, as much of one as this reads. */
interface LogRecord {
  event?: unknown;
  data?: unknown;
}

/**
 * Parse one line, insisting it is an object.
 *
 * @throws {TypeError} When the line parses to something that is not a record —
 *   the reference reaches for `.get` on it and raises there.
 */
function parseRecord(line: string): LogRecord {
  const parsed: unknown = JSON.parse(line);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new TypeError("session log line is not an object");
  }
  return parsed as LogRecord;
}

/**
 * The `data` map of a record.
 *
 * Absent is an empty map; present but not a map is a failure, because the
 * reference reaches into it and raises there. Reading a list as an empty map
 * would quietly drop whatever that record carried.
 *
 * @throws {TypeError} When `data` is present and is not an object.
 */
function dataOf(record: LogRecord): Record<string, unknown> {
  const data = record.data;
  if (data === undefined || data === null) {
    return {};
  }
  if (typeof data !== "object" || Array.isArray(data)) {
    throw new TypeError("session log record data is not an object");
  }
  return data as Record<string, unknown>;
}

/**
 * Every `read` event's bytes, concatenated.
 *
 * A read that carries no screen still carries bytes; the viewer skips that
 * record and this must not, or the stream loses everything the operator did
 * not see rendered.
 */
export function rawBytesFromLog(text: string): Uint8Array {
  const chunks: Buffer[] = [];
  for (const line of text.split("\n")) {
    if (line.trim() === "") {
      continue;
    }
    const record = parseRecord(line);
    if (record.event !== "read") {
      continue;
    }
    const encoded = dataOf(record).raw_bytes_b64;
    // The emptiness check is not observable — decoding "" yields no bytes
    // either — but it says that a read with no payload is a read that
    // contributed nothing, rather than one that contributed an empty chunk.
    if (typeof encoded === "string" && encoded !== "") {
      chunks.push(Buffer.from(encoded, "base64"));
    }
  }
  return Uint8Array.from(Buffer.concat(chunks));
}

/** Rebuild the stream from `logPath` and write it to `outPath`. */
export function rebuildRawStream(logPath: string, outPath: string): void {
  writeFileSync(outPath, rawBytesFromLog(readFileSync(logPath, "utf8")));
}
