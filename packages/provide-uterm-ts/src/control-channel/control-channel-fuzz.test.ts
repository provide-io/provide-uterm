//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Replay the cross-language differential fuzz corpus for the control-frame
 * codec.
 *
 * The corpus lives at `conformance/fuzz/control_channel_fuzz.json`, outside
 * this package, and is the same file the Python, Go and C# ports replay.
 * `conformance/fuzz/README.md` is the normative contract for its format; this
 * suite is written against that document, not against the generator.
 *
 * It is *additional* to `control_channel_golden.json`, not a replacement: the
 * golden is the hand-written surface, this is the same surface driven by
 * generated hostile input.
 *
 * ## Bytes, not strings
 *
 * Every codec input and output travels as base64 of UTF-8 precisely so that
 * no port has to agree about string representation. This replayer decodes to
 * bytes and compares in bytes — a comparison that round-tripped through a
 * JavaScript string would agree on the ASCII cases and could still disagree
 * on the astral ones, which is exactly what the corpus exists to catch.
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { loadConformance } from "../testing/golden.ts";
import type { ControlFrameChunk } from "./index.ts";
import {
  ControlFrameDecoder,
  ControlFrameProtocolError,
  encodeControlFrame,
  encodeTerminalData,
  isControlFrame,
} from "./index.ts";

/** The only corpus format this replayer understands. */
const SCHEMA = "provide-uterm/control-channel-fuzz/1";

/** Recorded id prefix per family, from README.md's "Case identifiers". */
const ID_PREFIXES = {
  encode_data: "CCF-ED-",
  encode_control: "CCF-EC-",
  is_control_frame: "CCF-PR-",
  decode: "CCF-DC-",
  regressions: "CCF-REG-",
  serializer_divergences: "CCF-SD-",
} as const;

/**
 * The case count this replayer expects to assert, per family.
 *
 * Pinned here as well as read from the file so that a corpus which lost cases
 * makes this suite fail rather than silently assert fewer things. A
 * regeneration that legitimately grows a family updates these numbers.
 */
const EXPECTED_COUNTS = {
  encode_data: 96,
  encode_control: 96,
  is_control_frame: 128,
  decode: 192,
  regressions: 5,
  serializer_divergences: 6,
} as const;

type FamilyName = keyof typeof ID_PREFIXES;

/** One recorded decoder event, in the corpus's own shape. */
type RecordedEvent = { kind: "data"; data_b64: string } | { kind: "control"; control: unknown };

/** One recorded drive of the decoder: what it emitted, and how it stopped. */
interface DriveRecord {
  events: RecordedEvent[];
  error: string | null;
  on_error: string[];
}

interface DecodeCase {
  id: string;
  chunks_b64: string[];
  finish: boolean;
  chunked: DriveRecord;
  single: DriveRecord;
}

interface FuzzCorpus {
  schema: string;
  generator: string;
  seed: number;
  limits: { header_bytes: number; max_control_payload_bytes: number; max_frame_depth: number };
  counts: Record<FamilyName, number>;
  encode_data: Array<{ id: string; in_b64: string; out_b64: string }>;
  encode_control: Array<{ id: string; payload: Record<string, unknown>; out_b64: string }>;
  is_control_frame: Array<{ id: string; in_b64: string; out: boolean }>;
  decode: DecodeCase[];
  regressions: Array<DecodeCase & { note: string }>;
  serializer_divergences: Array<{
    id: string;
    note: string;
    payload: Record<string, unknown>;
    cpython_out_b64: string;
  }>;
}

/**
 * Where the corpus is read from.
 *
 * Normally resolved from this module's own URL, so `vitest run` works from
 * any working directory. `UTERM_FUZZ_CORPUS` points the replay at a candidate
 * file instead — used to rehearse a regeneration, and to prove this suite
 * really fails (naming the case id) when a recorded value moves.
 */
const override = process.env.UTERM_FUZZ_CORPUS;
const corpus: FuzzCorpus = override
  ? (JSON.parse(readFileSync(override, "utf-8")) as FuzzCorpus)
  : loadConformance<FuzzCorpus>("fuzz/control_channel_fuzz.json");

// Refuse to run on a format this replayer does not understand, rather than
// silently skipping every case in it. A module-scope throw fails the file.
if (corpus.schema !== SCHEMA) {
  throw new Error(`unsupported fuzz corpus schema ${corpus.schema}, expected ${SCHEMA}`);
}

/** Strict UTF-8 decoder: invalid bytes must throw, never become U+FFFD. */
const utf8 = new TextDecoder("utf-8", { fatal: true });

/**
 * Recover the codec string carried by a `*_b64` field.
 *
 * Standard base64 of UTF-8, per README.md rule 1. Node's base64 reader is
 * lenient — it drops characters outside the alphabet — so the decoded bytes
 * are re-encoded and compared to reject a field that is not canonical base64.
 */
function fromB64(value: string): string {
  const bytes = Buffer.from(value, "base64");
  if (bytes.toString("base64") !== value) {
    throw new Error(`not canonical base64: ${value}`);
  }
  return utf8.decode(bytes);
}

/** Render a codec string the way the corpus records one. */
function toB64(value: string): string {
  return Buffer.from(value, "utf-8").toString("base64");
}

/** Decode every chunk of a case, in feed order. */
function chunksOf(testCase: DecodeCase): string[] {
  return testCase.chunks_b64.map(fromB64);
}

/** Record one decoder event in the corpus's shape. */
function recordEvent(event: ControlFrameChunk): RecordedEvent {
  return event.kind === "data"
    ? { kind: "data", data_b64: toB64(event.data) }
    : { kind: "control", control: event.control };
}

/**
 * Drive a fresh decoder over `chunks` and record what it did.
 *
 * Per README.md: stop at the first protocol error, keeping the events emitted
 * before it, and call `finish()` only when the case says to.
 */
function drive(chunks: readonly string[], finish: boolean): DriveRecord {
  const onError: string[] = [];
  const decoder = new ControlFrameDecoder({ onError: (code) => onError.push(code) });
  const events: RecordedEvent[] = [];
  try {
    for (const chunk of chunks) {
      for (const event of decoder.feed(chunk)) {
        events.push(recordEvent(event));
      }
    }
    if (finish) {
      for (const event of decoder.finish()) {
        events.push(recordEvent(event));
      }
    }
  } catch (error) {
    if (!(error instanceof ControlFrameProtocolError)) {
      throw error;
    }
    return { events, error: error.message, on_error: onError };
  }
  return { events, error: null, on_error: onError };
}

/** The decode family and the regression family have identical shape. */
const decodeCases: DecodeCase[] = [...corpus.decode, ...corpus.regressions];

describe("control-channel fuzz corpus: the file itself", () => {
  it("declares the schema, generator and seed this replayer was written for", () => {
    expect({ schema: corpus.schema, generator: corpus.generator, seed: corpus.seed }).toStrictEqual({
      schema: SCHEMA,
      generator: "conformance/fuzz/gen_control_channel_fuzz.py",
      seed: 20260729,
    });
  });

  it("agrees with this port's own protocol limits", () => {
    expect(corpus.limits).toStrictEqual({
      header_bytes: 11,
      max_control_payload_bytes: 1_048_576,
      max_frame_depth: 32,
    });
  });

  it("carries the case count each family is expected to assert", () => {
    const actual = Object.fromEntries(
      (Object.keys(EXPECTED_COUNTS) as FamilyName[]).map((family) => [family, corpus[family].length]),
    );
    expect(actual).toStrictEqual({ ...EXPECTED_COUNTS });
    expect(corpus.counts).toStrictEqual({ ...EXPECTED_COUNTS });
  });

  it("gives every case a unique, family-prefixed id", () => {
    const seen = new Set<string>();
    for (const family of Object.keys(ID_PREFIXES) as FamilyName[]) {
      for (const testCase of corpus[family]) {
        expect(testCase.id.startsWith(ID_PREFIXES[family]), `${testCase.id} is not in ${family}`).toBe(true);
        expect(seen.has(testCase.id), `duplicate case id ${testCase.id}`).toBe(false);
        seen.add(testCase.id);
      }
    }
    expect(seen.size).toBe(523);
  });

  it("exercises both drives and both finish states", () => {
    // A corpus that only ever fed whole streams, or only ever finished, would
    // replay green here and prove nothing about buffering.
    const divergent = decodeCases.filter(
      (testCase) => JSON.stringify(testCase.chunked) !== JSON.stringify(testCase.single),
    );
    expect(divergent.length).toBeGreaterThan(10);
    expect(corpus.decode.filter((testCase) => !testCase.finish).length).toBeGreaterThan(10);
  });
});

describe("control-channel fuzz corpus: encode_data", () => {
  it("matches every recorded terminal-data encoding, in bytes", () => {
    let asserted = 0;
    for (const testCase of corpus.encode_data) {
      const actual = toB64(encodeTerminalData(fromB64(testCase.in_b64)));
      expect({ id: testCase.id, out_b64: actual }).toStrictEqual({ id: testCase.id, out_b64: testCase.out_b64 });
      asserted += 1;
    }
    expect(asserted).toBe(EXPECTED_COUNTS.encode_data);
  });
});

describe("control-channel fuzz corpus: encode_control", () => {
  it("matches every recorded control-frame encoding, in bytes", () => {
    let asserted = 0;
    for (const testCase of corpus.encode_control) {
      const actual = toB64(encodeControlFrame(testCase.payload));
      expect({ id: testCase.id, out_b64: actual }).toStrictEqual({ id: testCase.id, out_b64: testCase.out_b64 });
      asserted += 1;
    }
    expect(asserted).toBe(EXPECTED_COUNTS.encode_control);
  });

  it("draws payload keys in ascending order, so key ordering can never be the difference", () => {
    // Go marshals a map with sorted keys; JS, CPython and .NET preserve
    // insertion order. Ascending keys make both rules agree.
    for (const testCase of corpus.encode_control) {
      const keys = Object.keys(testCase.payload);
      expect({ id: testCase.id, keys }).toStrictEqual({ id: testCase.id, keys: [...keys].sort() });
    }
  });
});

describe("control-channel fuzz corpus: is_control_frame", () => {
  it("matches every recorded structural-predicate verdict", () => {
    let asserted = 0;
    for (const testCase of corpus.is_control_frame) {
      const actual = isControlFrame(fromB64(testCase.in_b64));
      expect({ id: testCase.id, out: actual }).toStrictEqual({ id: testCase.id, out: testCase.out });
      asserted += 1;
    }
    expect(asserted).toBe(EXPECTED_COUNTS.is_control_frame);
  });
});

describe("control-channel fuzz corpus: decode", () => {
  it("matches the chunked drive of every recorded stream", () => {
    let asserted = 0;
    for (const testCase of decodeCases) {
      const actual = drive(chunksOf(testCase), testCase.finish);
      expect({ id: testCase.id, drive: "chunked", ...actual }).toStrictEqual({
        id: testCase.id,
        drive: "chunked",
        ...testCase.chunked,
      });
      asserted += 1;
    }
    expect(asserted).toBe(EXPECTED_COUNTS.decode + EXPECTED_COUNTS.regressions);
  });

  it("matches the single drive of every recorded stream", () => {
    // Exactly one chunk is fed even when the concatenation is empty: one case
    // has no chunks at all, and feed("") must behave as feeding nothing.
    let asserted = 0;
    for (const testCase of decodeCases) {
      const actual = drive([chunksOf(testCase).join("")], testCase.finish);
      expect({ id: testCase.id, drive: "single", ...actual }).toStrictEqual({
        id: testCase.id,
        drive: "single",
        ...testCase.single,
      });
      asserted += 1;
    }
    expect(asserted).toBe(EXPECTED_COUNTS.decode + EXPECTED_COUNTS.regressions);
  });

  it("fires the error hook exactly once per rejection and never otherwise", () => {
    for (const testCase of decodeCases) {
      for (const [name, record] of [
        ["chunked", testCase.chunked],
        ["single", testCase.single],
      ] as const) {
        const expected = record.error === null ? [] : ["control_frame_protocol_error"];
        expect({ id: testCase.id, drive: name, on_error: record.on_error }).toStrictEqual({
          id: testCase.id,
          drive: name,
          on_error: expected,
        });
      }
    }
  });
});

describe("control-channel fuzz corpus: serializer_divergences", () => {
  it("pins this port's own output, which is NOT required to equal CPython's", () => {
    // README.md is explicit that these six are recorded, not asserted equal
    // across ports: the four runtimes' JSON serializers legitimately disagree.
    // What this port must not do is *move* — so its bytes are pinned here, and
    // whether each one happens to coincide with CPython is recorded alongside.
    const actual = corpus.serializer_divergences.map((testCase) => {
      const out = encodeControlFrame(testCase.payload);
      return { id: testCase.id, out: out.slice(2), matchesCPython: toB64(out) === testCase.cpython_out_b64 };
    });
    expect(actual).toStrictEqual([
      { id: "CCF-SD-0001", out: '00000008:{"k0":0}', matchesCPython: false },
      { id: "CCF-SD-0002", out: '00000010:{"k0":[1,1.5,2]}', matchesCPython: false },
      { id: "CCF-SD-0003", out: '0000000f:{"k0":"\u2028\u2029"}', matchesCPython: true },
      { id: "CCF-SD-0004", out: '0000000a:{"k0":"\u007f"}', matchesCPython: true },
      { id: "CCF-SD-0005", out: '0000000f:{"k0":"\\u001f"}', matchesCPython: true },
      { id: "CCF-SD-0006", out: '0000000d:{"k0":"\u{1d11e}"}', matchesCPython: true },
    ]);
    expect(actual.length).toBe(EXPECTED_COUNTS.serializer_divergences);
  });
});
