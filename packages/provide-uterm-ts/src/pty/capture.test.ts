//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  CAPTURE_BIND_UMASK,
  CAPTURE_HEADER_SIZE,
  CAPTURE_QUEUE_MAXSIZE,
  CAPTURE_SOCKET_MODE,
  type CaptureFrame,
  CaptureQueue,
  type CaptureReader,
  CaptureShortRead,
  CHANNEL_CONNECT,
  CHANNEL_STDIN,
  CHANNEL_STDOUT,
  MAX_CAPTURE_FRAME_BYTES,
  readCaptureFrames,
} from "./index.ts";

interface StreamCase {
  name: string;
  stream: string;
  frames: Array<{ channel: number; data: string }>;
  reads: number[];
  closes: number;
}

interface CaptureGolden {
  channels: { stdout: number; stdin: number; connect: number };
  max_frame_bytes: number;
  queue_maxsize: number;
  socket_mode: number;
  bind_umask: number;
  header_size: number;
  streams: StreamCase[];
  backpressure: { maxsize: number; pushed: number; kept: number; first_kept: string; last_kept: string };
}

const golden = loadGolden<CaptureGolden>("ptycapture_golden.json");

/** The corpus writes bytes as latin-1 text, which is byte-for-character. */
function bytes(text: string): Uint8Array {
  return Uint8Array.from([...text].map((character) => character.charCodeAt(0)));
}

function latin1(data: Uint8Array): string {
  return [...data].map((byte) => String.fromCharCode(byte)).join("");
}

/**
 * A reader over a fixed buffer that records every read it was asked for.
 *
 * The record is the point: an over-large frame has to be shown never to have
 * been read, not merely never to have been delivered.
 */
class RecordingReader implements CaptureReader {
  readonly reads: number[] = [];
  #offset = 0;
  readonly #data: Uint8Array;

  constructor(data: Uint8Array) {
    this.#data = data;
  }

  async readExactly(size: number): Promise<Uint8Array> {
    const chunk = this.#data.subarray(this.#offset, this.#offset + size);
    if (chunk.length < size) {
      this.#offset = this.#data.length;
      throw new CaptureShortRead(`want ${size}, got ${chunk.length}`);
    }
    this.#offset += size;
    this.reads.push(size);
    return chunk;
  }
}

/** Read one recorded stream and collect what came out. */
async function drive(record: StreamCase): Promise<{ frames: CaptureFrame[]; reads: number[]; reason: string }> {
  const reader = new RecordingReader(bytes(record.stream));
  const frames: CaptureFrame[] = [];
  const reason = await readCaptureFrames(reader, (frame) => frames.push(frame));
  return { frames, reads: reader.reads, reason };
}

/** A frame as it goes on the wire. */
function frame(channel: number, payload: string): string {
  return header(channel, payload.length) + payload;
}

/** A header claiming `length` bytes. */
function header(channel: number, length: number): string {
  return [channel, (length >>> 24) & 0xff, (length >>> 16) & 0xff, (length >>> 8) & 0xff, length & 0xff]
    .map((byte) => String.fromCharCode(byte))
    .join("");
}

describe("reading captured terminal traffic", () => {
  it.each(golden.streams)("$name", async (record) => {
    const { frames, reads } = await drive(record);
    expect(frames.map((entry) => ({ channel: entry.channel, data: latin1(entry.data) }))).toEqual(record.frames);
    // Which reads were attempted, not only which frames arrived.
    expect(reads).toEqual(record.reads);
  });

  it("never reads the body of an over-large frame", async () => {
    // The whole guard: asking for the body first is what would let a claimed
    // four gigabytes actually be allocated.
    const record = golden.streams.find((entry) => entry.name === "a frame claiming four gigabytes") as StreamCase;
    const { frames, reads, reason } = await drive(record);
    expect(frames).toEqual([]);
    expect(reads).toEqual([CAPTURE_HEADER_SIZE]);
    expect(reason).toBe("frame-too-large");
  });

  it("reads a frame exactly at the cap and refuses one byte more", async () => {
    // Inclusive, so the largest legitimate frame is not the first one dropped.
    const atCap = new RecordingReader(bytes(header(CHANNEL_STDOUT, MAX_CAPTURE_FRAME_BYTES)));
    expect(await readCaptureFrames(atCap, () => undefined)).toBe("ended");
    expect(atCap.reads).toEqual([CAPTURE_HEADER_SIZE]);

    const overCap = new RecordingReader(bytes(header(CHANNEL_STDOUT, MAX_CAPTURE_FRAME_BYTES + 1)));
    expect(await readCaptureFrames(overCap, () => undefined)).toBe("frame-too-large");
  });

  it("keeps the frames it already read before an over-large one", async () => {
    const record = golden.streams.find((entry) => entry.name === "a good frame before an over-large one") as StreamCase;
    const { frames } = await drive(record);
    expect(frames).toHaveLength(1);
    expect(latin1(frames[0]?.data as Uint8Array)).toBe("first");
  });

  it("reads nothing after an over-large one", async () => {
    // The stream is no longer trustworthy: once one length is wrong there is
    // no way to find where the next frame starts.
    const record = golden.streams.find((entry) => entry.name === "a frame after an over-large one") as StreamCase;
    expect((await drive(record)).frames).toEqual([]);
  });

  it("ends quietly when the stream stops mid-frame", async () => {
    // Which is how a capture session ordinarily ends.
    for (const name of ["a header that stops short", "a payload that stops short"]) {
      const record = golden.streams.find((entry) => entry.name === name) as StreamCase;
      const { frames, reason } = await drive(record);
      expect(frames).toEqual([]);
      expect(reason).toBe("ended");
    }
  });

  it("loses its place when a frame lies about its length", async () => {
    // A short frame does not stop the read: the declared length is taken from
    // whatever follows, so the next frame's header is swallowed into this
    // one's payload. The reference's behaviour, pinned because it is the kind
    // of thing a port would 'fix' into a difference.
    const record = golden.streams.find((entry) => entry.name === "a frame after one that stops short") as StreamCase;
    const { frames } = await drive(record);
    expect(frames).toHaveLength(1);
    expect(latin1(frames[0]?.data as Uint8Array)).toHaveLength(10);
    expect(latin1(frames[0]?.data as Uint8Array).startsWith("abc")).toBe(true);
  });

  it("takes an empty payload", async () => {
    const reader = new RecordingReader(bytes(frame(CHANNEL_STDOUT, "")));
    const frames: CaptureFrame[] = [];
    await readCaptureFrames(reader, (entry) => frames.push(entry));
    expect(frames).toEqual([{ channel: CHANNEL_STDOUT, data: new Uint8Array(0) }]);
  });

  it("passes a channel nobody defined through rather than refusing it", async () => {
    // A capture library that learns a new channel must not break a reader
    // that has not.
    const reader = new RecordingReader(bytes(frame(0x7f, "x")));
    const frames: CaptureFrame[] = [];
    await readCaptureFrames(reader, (entry) => frames.push(entry));
    expect(frames[0]?.channel).toBe(0x7f);
  });

  it("names the channels the reference names", () => {
    expect([CHANNEL_STDOUT, CHANNEL_STDIN, CHANNEL_CONNECT]).toEqual([
      golden.channels.stdout,
      golden.channels.stdin,
      golden.channels.connect,
    ]);
    expect(CAPTURE_HEADER_SIZE).toBe(golden.header_size);
    expect(MAX_CAPTURE_FRAME_BYTES).toBe(golden.max_frame_bytes);
  });

  it("passes on a failure that is not the stream ending", async () => {
    // Only a short read is quiet; a real fault is the caller's to see.
    const broken: CaptureReader = {
      async readExactly(): Promise<Uint8Array> {
        throw new Error("socket error");
      },
    };
    await expect(readCaptureFrames(broken, () => undefined)).rejects.toThrow("socket error");
  });

  it("passes on a failure reading a body, too", async () => {
    let call = 0;
    const broken: CaptureReader = {
      async readExactly(size: number): Promise<Uint8Array> {
        call += 1;
        if (call === 1) {
          return bytes(header(CHANNEL_STDOUT, 4));
        }
        throw new Error(`socket error after ${size}`);
      },
    };
    await expect(readCaptureFrames(broken, () => undefined)).rejects.toThrow("socket error after 4");
  });
});

describe("a reader that has fallen behind", () => {
  it("keeps the newest frames and drops the oldest", async () => {
    // What a viewer wants when something has to go is the most recent screen
    // rather than the stalest.
    const queue = new CaptureQueue();
    for (let index = 0; index < golden.backpressure.pushed; index += 1) {
      queue.push({ channel: CHANNEL_STDOUT, data: bytes(String(index)) });
    }
    expect(queue.size).toBe(golden.backpressure.kept);
    expect(latin1(queue.pop()?.data as Uint8Array)).toBe(golden.backpressure.first_kept);
    let last: CaptureFrame | undefined;
    for (let frame = queue.pop(); frame !== undefined; frame = queue.pop()) {
      last = frame;
    }
    expect(latin1(last?.data as Uint8Array)).toBe(golden.backpressure.last_kept);
  });

  it("holds what the reference holds", () => {
    expect(CAPTURE_QUEUE_MAXSIZE).toBe(golden.queue_maxsize);
    expect(golden.backpressure.maxsize).toBe(CAPTURE_QUEUE_MAXSIZE);
  });

  it("counts what it dropped", () => {
    const queue = new CaptureQueue(2);
    for (const data of ["a", "b", "c", "d"]) {
      queue.push({ channel: CHANNEL_STDOUT, data: bytes(data) });
    }
    expect(queue.dropped).toBe(2);
    expect(queue.size).toBe(2);
    expect(latin1(queue.pop()?.data as Uint8Array)).toBe("c");
  });

  it("says nothing when there is nothing waiting", () => {
    expect(new CaptureQueue().pop()).toBeUndefined();
  });

  it("drops nothing while there is room", () => {
    const queue = new CaptureQueue(2);
    queue.push({ channel: CHANNEL_STDOUT, data: bytes("a") });
    expect(queue.dropped).toBe(0);
    expect(queue.size).toBe(1);
  });
});

describe("the socket it listens on", () => {
  it("is created at the mode the reference creates it at", () => {
    // This socket carries everything typed and shown, so the window a
    // post-bind chmod would leave is not one to have.
    expect(CAPTURE_SOCKET_MODE).toBe(golden.socket_mode);
    expect(CAPTURE_BIND_UMASK).toBe(golden.bind_umask);
    expect(CAPTURE_SOCKET_MODE).toBe(0o600);
    expect(CAPTURE_BIND_UMASK & CAPTURE_SOCKET_MODE).toBe(0);
  });
});
