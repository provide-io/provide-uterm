//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type ByteSink,
  type ByteSource,
  filterRfbClientInput,
  MAX_CUT_TEXT,
  RfbProtocolError,
  RfbShortReadError,
} from "./index.ts";

interface Outcome {
  out: number[];
  ready: number;
  error: string | null;
}

interface RfbGolden {
  handshake: number[];
  max_cut_text: number;
  streams: Array<{ name: string; body: number[]; allowed: Outcome; refused: Outcome; no_checker: Outcome }>;
  bad_security_type: Outcome;
  unknown_message: Outcome;
  short_message: Outcome;
  short_handshake: Outcome;
  oversized_clipboard: Outcome;
  oversized_then_more: Outcome;
  oversized_fully_sent: Outcome;
  clipboard_at_the_cap: Outcome;
}

const golden = loadGolden<RfbGolden>("rfbfilter_golden.json");

const KEY_EVENT = 4;
const UPDATE_REQUEST = 3;

/** A source over a fixed buffer, handing out at most what is left. */
function source(bytes: Uint8Array): ByteSource {
  let offset = 0;
  return {
    read(size: number): Uint8Array {
      const chunk = bytes.subarray(offset, offset + size);
      offset += chunk.length;
      return chunk;
    },
  };
}

/**
 * A sink that records what it was written.
 *
 * Appended chunk by chunk rather than spread into `push`: a clipboard write at
 * the cap is a megabyte, and spreading that many arguments overflows the call
 * stack.
 */
function sink(): ByteSink & { readonly written: number[] } {
  const chunks: Uint8Array[] = [];
  return {
    get written(): number[] {
      const out: number[] = [];
      for (const chunk of chunks) {
        for (const byte of chunk) {
          out.push(byte);
        }
      }
      return out;
    },
    write: (data) => {
      chunks.push(Uint8Array.from(data));
    },
  };
}

/** A ClientCutText message declaring `length` and carrying `sent` bytes. */
function cutText(length: number, sent: number): number[] {
  return [
    6,
    0,
    0,
    0,
    (length >>> 24) & 0xff,
    (length >>> 16) & 0xff,
    (length >>> 8) & 0xff,
    length & 0xff,
    ...new Array(sent).fill(0),
  ];
}

/** Run one stream through and record it in the shape the corpus does. */
function run(body: number[], allow: boolean | null, handshake = golden.handshake): Outcome {
  const out = sink();
  let ready = 0;
  const options = {
    ...(allow === null ? {} : { canInject: () => allow }),
    sessionId: "s",
    leaseId: "l",
    principalId: "p",
    principalRole: "operator",
    onClientReady: () => {
      ready += 1;
    },
  };
  try {
    filterRfbClientInput(out, source(Uint8Array.from([...handshake, ...body])), options);
    return { out: out.written, ready, error: null };
  } catch (error) {
    return {
      out: out.written,
      ready,
      error: error instanceof RfbShortReadError ? "EOFError" : error instanceof RfbProtocolError ? "ValueError" : "?",
    };
  }
}

describe("what a viewer may send", () => {
  it.each(golden.streams)("$name", (record) => {
    expect(run(record.body, true)).toEqual(record.allowed);
    expect(run(record.body, false)).toEqual(record.refused);
    expect(run(record.body, null)).toEqual(record.no_checker);
  });

  it("passes the handshake through untouched", () => {
    expect(run([], true).out).toEqual(golden.handshake);
  });

  it("forwards everything that only reads the screen", () => {
    // A viewer with no permission to act still has to see the session.
    const readOnly = [UPDATE_REQUEST, ...new Array(9).fill(0)];
    expect(run(readOnly, false).out).toEqual(run(readOnly, true).out);
  });

  it("drops a keystroke a viewer may not send", () => {
    // Dropped rather than refused: the stream is a byte protocol with no room
    // for an error, so the session stays up and the viewer sees nothing
    // happen.
    const keystroke = [KEY_EVENT, ...new Array(7).fill(0)];
    expect(run(keystroke, false).out).toEqual(golden.handshake);
    expect(run(keystroke, true).out.length).toBeGreaterThan(golden.handshake.length);
  });

  it("fails closed with no permission check at all", () => {
    // A relay wired up without one would otherwise forward keystrokes from
    // anybody.
    for (const message of [
      [KEY_EVENT, ...new Array(7).fill(0)],
      [5, ...new Array(5).fill(0)],
      [6, 0, 0, 0, 0, 0, 0, 1, 120],
    ]) {
      expect(run(message, null).out).toEqual(golden.handshake);
    }
  });

  it("announces readiness once, after the first update request", () => {
    // The client's pixel format and encodings precede it; a driver injecting
    // requests earlier would have the server answer in its own format and the
    // client render those frames with swapped colours.
    const one = [UPDATE_REQUEST, ...new Array(9).fill(0)];
    expect(run(one, true).ready).toBe(1);
    expect(run([...one, ...one], true).ready).toBe(1);
    expect(run([], true).ready).toBe(0);
  });
});

describe("a clipboard write", () => {
  it("strips the extended-clipboard bit from the length", () => {
    // RFB's extended clipboard sets the top bit and the rest is the real
    // size; reading the field whole would make every extended write look like
    // two gigabytes and be dropped.
    const extended = golden.streams.find((entry) => entry.name === "an extended clipboard write");
    expect(run(extended?.body as number[], true).out).toEqual(extended?.allowed.out);
    expect(run(extended?.body as number[], true).out.length).toBeGreaterThan(golden.handshake.length);
  });

  it("drops one larger than the cap without tearing the relay down", () => {
    // Raising would black the framebuffer for everyone watching, which is a
    // worse answer to one hostile message.
    expect(run(cutText(MAX_CUT_TEXT + 1, 64), true)).toEqual(golden.oversized_clipboard);
    expect(golden.oversized_clipboard.error).toBeNull();
    expect(golden.oversized_clipboard.out).toEqual(golden.handshake);
    expect(MAX_CUT_TEXT).toBe(golden.max_cut_text);
  });

  it("carries on once an oversized write is fully drained", () => {
    // Which is what the cap is for: a client that sends every byte it
    // declared has its clipboard dropped and its session left running, and
    // the update request after it still gets through.
    const declared = golden.max_cut_text + 1;
    const body = [...cutText(declared, declared), 3, ...new Array(9).fill(0)];
    expect(run(body, true)).toEqual(golden.oversized_fully_sent);
    expect(golden.oversized_fully_sent.error).toBeNull();
    expect(golden.oversized_fully_sent.ready).toBe(1);
    expect(golden.oversized_fully_sent.out.length).toBeGreaterThan(golden.handshake.length);
  });

  it("forwards a write exactly at the cap", () => {
    // The boundary is inclusive, so the largest permitted paste is not the
    // first one dropped.
    const cap = golden.max_cut_text;
    expect(run(cutText(cap, cap), true)).toEqual(golden.clipboard_at_the_cap);
    expect(golden.clipboard_at_the_cap.error).toBeNull();
    expect(golden.clipboard_at_the_cap.out.length).toBeGreaterThan(golden.max_cut_text);
  });

  it("drains to the end when a client declares more than it sends", () => {
    // The drain trusts the declared length, so the rest of the stream goes
    // with it and the session's input ends there. The reference's behaviour,
    // pinned because it is the kind of thing a port would 'fix' into a
    // difference.
    const body = [...cutText(MAX_CUT_TEXT + 1, 64), 3, ...new Array(9).fill(0)];
    expect(run(body, true)).toEqual(golden.oversized_then_more);
    expect(golden.oversized_then_more.error).toBeNull();
    expect(golden.oversized_then_more.out).toEqual(golden.handshake);
  });
});

describe("a stream the filter refuses", () => {
  it("refuses a security type it does not implement", () => {
    const outcome = run([], true, [...golden.handshake.slice(0, 12), 2, 1]);
    expect(outcome).toEqual(golden.bad_security_type);
    expect(outcome.error).toBe("ValueError");
  });

  it("refuses a message type nobody sends", () => {
    // Rather than skipping it: the length is unknown, so there is no way to
    // find the next message and everything after would be misread.
    expect(run([99], true)).toEqual(golden.unknown_message);
  });

  it("refuses a stream that stops mid-message", () => {
    expect(run([KEY_EVENT, 0, 0, 0], true)).toEqual(golden.short_message);
  });

  it("refuses a handshake that stops short", () => {
    expect(run([], true, [...Buffer.from("RFB 003.")])).toEqual(golden.short_handshake);
  });

  it("ends quietly when the client hangs up between messages", () => {
    // Which is how a session ordinarily ends.
    expect(run([], true).error).toBeNull();
  });
});
