// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The RFB client's handshake, framebuffer tracking, and every refusal.
 *
 * Driven through a scripted duplex rather than a socket: an RFB conversation is
 * a byte sequence, and the interesting cases are the malformed ones a real
 * server will not produce on demand — a truncated stream, a security list
 * without `None`, a ServerInit claiming a 60000-pixel desktop.
 *
 * Held to the same cases as the Python reference's test_rfb_session.py, because
 * the point of a port is that it refuses the same things.
 */

import { describe, expect, it } from "vitest";

import {
  ByteReader,
  ENCODING_COPY_RECT,
  ENCODING_RAW,
  encodeKeyEvent,
  encodePointerEvent,
  encodeSetEncodings,
  encodeSetPixelFormat,
  encodeUpdateRequest,
  negotiateSecurity,
  negotiateVersion,
  RfbGraphicalSession,
  type RfbStream,
  readServerInit,
} from "./rfb.ts";

/** A duplex that replays a script and records what was written to it. */
class ScriptedStream implements RfbStream {
  readonly written: Buffer[] = [];
  destroyed = false;
  #onData: ((chunk: Buffer) => void) | null = null;
  #onEnd: (() => void) | null = null;
  #pending: Buffer;

  constructor(script: Buffer = Buffer.alloc(0)) {
    this.#pending = script;
  }

  on(event: string, listener: (...args: never[]) => void): this {
    if (event === "data") {
      this.#onData = listener as unknown as (chunk: Buffer) => void;
      // Deliver the script once somebody is listening.
      // Synchronously: a queued delivery loses the race with end() below, and
      // ByteReader buffers whatever arrives, so early is harmless.
      const script = this.#pending;
      this.#pending = Buffer.alloc(0);
      if (script.length > 0) {
        this.#onData?.(script);
      }
    } else if (event === "end") {
      this.#onEnd = listener as unknown as () => void;
    }
    return this;
  }

  /** Push more bytes after construction, for stepwise scripts. */
  push(chunk: Buffer): void {
    this.#onData?.(chunk);
  }

  end(): void {
    this.#onEnd?.();
  }

  write(chunk: Uint8Array): boolean {
    this.written.push(Buffer.from(chunk));
    return true;
  }

  destroy(): void {
    this.destroyed = true;
    // A real socket stops delivering once destroyed, which is what lets a
    // closed session's read loop finish instead of hanging.
    this.#onEnd?.();
  }

  /** Everything written, concatenated. */
  sent(): Buffer {
    return Buffer.concat(this.written);
  }
}

function serverInit(width: number, height: number, name = ""): Buffer {
  const header = Buffer.alloc(24);
  header.writeUInt16BE(width, 0);
  header.writeUInt16BE(height, 2);
  header.writeUInt32BE(name.length, 20);
  return Buffer.concat([header, Buffer.from(name, "ascii")]);
}

function rawUpdate(x: number, y: number, w: number, h: number, pixels: Buffer): Buffer {
  const head = Buffer.alloc(4);
  head.writeUInt8(0, 0);
  head.writeUInt16BE(1, 2);
  const rect = Buffer.alloc(12);
  rect.writeUInt16BE(x, 0);
  rect.writeUInt16BE(y, 2);
  rect.writeUInt16BE(w, 4);
  rect.writeUInt16BE(h, 6);
  rect.writeInt32BE(ENCODING_RAW, 8);
  return Buffer.concat([head, rect, pixels]);
}

/** A full handshake script ending in ServerInit. */
function handshake(width: number, height: number): Buffer {
  return Buffer.concat([
    Buffer.from("RFB 003.008\n", "ascii"),
    Buffer.from([1, 1]),
    Buffer.from([0, 0, 0, 0]),
    serverInit(width, height),
  ]);
}

describe("framing primitives", () => {
  it("reassembles a dribbled stream", async () => {
    const stream = new ScriptedStream();
    const reader = new ByteReader(stream);
    const pending = reader.read(4);
    stream.push(Buffer.from([1, 2]));
    stream.push(Buffer.from([3, 4]));
    expect([...(await pending)]).toEqual([1, 2, 3, 4]);
  });

  it("serves a read that is already buffered without waiting", async () => {
    const stream = new ScriptedStream();
    const reader = new ByteReader(stream);
    stream.push(Buffer.from([9, 8, 7]));
    expect([...(await reader.read(2))]).toEqual([9, 8]);
  });

  it("rejects a read outstanding when the stream ends", async () => {
    const stream = new ScriptedStream();
    const reader = new ByteReader(stream);
    const pending = reader.read(4);
    stream.end();
    await expect(pending).rejects.toThrow("RFB stream ended");
  });

  it("rejects a read attempted after the stream has already ended", async () => {
    const stream = new ScriptedStream();
    const reader = new ByteReader(stream);
    stream.end();
    await expect(reader.read(1)).rejects.toThrow("RFB stream ended");
  });

  it("encodes the wire messages at the sizes RFB specifies", () => {
    expect(encodeSetPixelFormat()).toHaveLength(20);
    expect(encodeSetEncodings()).toHaveLength(12);
    expect(encodeUpdateRequest(800, 600, true)).toHaveLength(10);
    expect(encodeUpdateRequest(800, 600, true)[1]).toBe(1);
    expect(encodeUpdateRequest(800, 600, false)[1]).toBe(0);
  });

  it("encodes input events to the documented layout", () => {
    expect([...encodePointerEvent(10, 20, 1)]).toEqual([5, 1, 0, 10, 0, 20]);
    // Negative coordinates clamp rather than wrapping into a huge u16.
    expect([...encodePointerEvent(-5, -5, 0xffff)]).toEqual([5, 0xff, 0, 0, 0, 0]);
    expect([...encodeKeyEvent(0xff0d, true)]).toEqual([4, 1, 0, 0, 0, 0, 0xff, 0x0d]);
    expect(encodeKeyEvent(0xff0d, false)[1]).toBe(0);
  });
});

describe("version and security", () => {
  it("prefers 3.8 when the server offers it", async () => {
    const stream = new ScriptedStream(Buffer.from("RFB 003.008\n", "ascii"));
    const reader = new ByteReader(stream);
    expect(await negotiateVersion(reader, stream)).toBe("RFB 003.008\n");
    expect(stream.sent().toString("ascii")).toBe("RFB 003.008\n");
  });

  it("echoes what an older server offered", async () => {
    const stream = new ScriptedStream(Buffer.from("RFB 003.003\n", "ascii"));
    const reader = new ByteReader(stream);
    expect(await negotiateVersion(reader, stream)).toBe("RFB 003.003\n");
  });

  it("refuses a peer that is not speaking RFB at all", async () => {
    const stream = new ScriptedStream(Buffer.from("HTTP/1.1 200", "ascii"));
    const reader = new ByteReader(stream);
    await expect(negotiateVersion(reader, stream)).rejects.toThrow("not an RFB server");
  });

  it("selects None and reads the 3.8 security result", async () => {
    const stream = new ScriptedStream(Buffer.concat([Buffer.from([2, 1, 2]), Buffer.from([0, 0, 0, 0])]));
    const reader = new ByteReader(stream);
    await negotiateSecurity(reader, stream, "RFB 003.008\n");
    expect([...stream.sent()]).toEqual([1]);
  });

  it("does not consume a security result on 3.7", async () => {
    const stream = new ScriptedStream(Buffer.from([1, 1]));
    const reader = new ByteReader(stream);
    await negotiateSecurity(reader, stream, "RFB 003.007\n");
    expect([...stream.sent()]).toEqual([1]);
  });

  it("treats an empty security list as the server refusing", async () => {
    const stream = new ScriptedStream(Buffer.from([0]));
    const reader = new ByteReader(stream);
    await expect(negotiateSecurity(reader, stream, "RFB 003.008\n")).rejects.toThrow("offered no types");
  });

  it("refuses a server without type None and says what it offered", async () => {
    const stream = new ScriptedStream(Buffer.from([2, 2, 16]));
    const reader = new ByteReader(stream);
    await expect(negotiateSecurity(reader, stream, "RFB 003.008\n")).rejects.toThrow("2, 16");
  });

  it("treats a non-zero security result as a rejection", async () => {
    const stream = new ScriptedStream(Buffer.concat([Buffer.from([1, 1]), Buffer.from([0, 0, 0, 1])]));
    const reader = new ByteReader(stream);
    await expect(negotiateSecurity(reader, stream, "RFB 003.008\n")).rejects.toThrow("security rejected");
  });

  it("accepts None on 3.3 and refuses anything else", async () => {
    const ok = new ScriptedStream(Buffer.from([0, 0, 0, 1]));
    await negotiateSecurity(new ByteReader(ok), ok, "RFB 003.003\n");

    const bad = new ScriptedStream(Buffer.from([0, 0, 0, 2]));
    await expect(negotiateSecurity(new ByteReader(bad), bad, "RFB 003.003\n")).rejects.toThrow(
      "unsupported RFB security type 2",
    );
  });
});

describe("ServerInit", () => {
  it("returns the dimensions and consumes the desktop name", async () => {
    const stream = new ScriptedStream(serverInit(640, 480, "desktop"));
    expect(await readServerInit(new ByteReader(stream))).toEqual([640, 480]);
  });

  it.each([
    [0, 480],
    [640, 0],
    [60000, 480],
    [640, 60000],
  ])("refuses a hostile ServerInit of %ix%i", async (width, height) => {
    const stream = new ScriptedStream(serverInit(width, height));
    await expect(readServerInit(new ByteReader(stream))).rejects.toThrow("dimensions out of range");
  });

  it("refuses an overlong desktop name", async () => {
    const header = Buffer.alloc(24);
    header.writeUInt16BE(64, 0);
    header.writeUInt16BE(64, 2);
    header.writeUInt32BE(99999, 20);
    const stream = new ScriptedStream(header);
    await expect(readServerInit(new ByteReader(stream))).rejects.toThrow("desktop name too long");
  });
});

describe("the session", () => {
  async function settled(script: Buffer, width = 4, height = 2): Promise<RfbGraphicalSession> {
    const stream = new ScriptedStream(script);
    const session = new RfbGraphicalSession(stream, new ByteReader(stream), width, height);
    stream.end();
    await session.settled;
    return session;
  }

  it("lands a raw rectangle in the framebuffer as RGBA", async () => {
    // One blue pixel, sent BGRA as the negotiated pixel format specifies.
    const session = await settled(rawUpdate(1, 0, 1, 1, Buffer.from([255, 0, 0, 0])));
    expect([...session.screenshot().pixels.slice(4, 8)]).toEqual([0, 0, 255, 255]);
  });

  it("returns a copy from screenshot, not the live buffer", async () => {
    const session = await settled(rawUpdate(0, 0, 1, 1, Buffer.from([1, 2, 3, 4])));
    const first = session.screenshot();
    first.pixels[0] = 99;
    expect(session.screenshot().pixels[0]).not.toBe(99);
  });

  it("skips a zero-area rectangle rather than reading a payload", async () => {
    const head = Buffer.alloc(4);
    head.writeUInt16BE(1, 2);
    const rect = Buffer.alloc(12);
    rect.writeInt32BE(ENCODING_RAW, 8);
    const session = await settled(Buffer.concat([head, rect]));
    expect(session.screenshot().pixels[0]).toBe(0);
  });

  it("consumes a copyrect's source coordinates exactly", async () => {
    const head = Buffer.alloc(4);
    head.writeUInt16BE(2, 2);
    const copy = Buffer.alloc(12);
    copy.writeUInt16BE(1, 4);
    copy.writeUInt16BE(1, 6);
    copy.writeInt32BE(ENCODING_COPY_RECT, 8);
    const raw = Buffer.alloc(12);
    raw.writeUInt16BE(1, 4);
    raw.writeUInt16BE(1, 6);
    raw.writeInt32BE(ENCODING_RAW, 8);
    // The raw rect only decodes if the copyrect payload was consumed exactly.
    const session = await settled(
      Buffer.concat([head, copy, Buffer.from([0, 2, 0, 2]), raw, Buffer.from([9, 9, 9, 9])]),
    );
    expect(session.screenshot().pixels[0]).toBe(9);
  });

  it("refuses a rectangle outside the framebuffer", async () => {
    const head = Buffer.alloc(4);
    head.writeUInt16BE(1, 2);
    const rect = Buffer.alloc(12);
    rect.writeUInt16BE(3, 0);
    rect.writeUInt16BE(4, 4);
    rect.writeUInt16BE(1, 6);
    rect.writeInt32BE(ENCODING_RAW, 8);
    const session = await settled(Buffer.concat([head, rect]));
    expect(session.screenshot().pixels[0]).toBe(0);
  });

  it("stops on an encoding it did not negotiate", async () => {
    const head = Buffer.alloc(4);
    head.writeUInt16BE(1, 2);
    const rect = Buffer.alloc(12);
    rect.writeUInt16BE(1, 4);
    rect.writeUInt16BE(1, 6);
    rect.writeInt32BE(7, 8);
    await expect(settled(Buffer.concat([head, rect]))).resolves.toBeDefined();
  });

  it("refuses an absurd rectangle count before looping", async () => {
    const head = Buffer.alloc(4);
    head.writeUInt16BE(9999, 2);
    await expect(settled(head)).resolves.toBeDefined();
  });

  it("ends the loop on a server message it cannot skip blindly", async () => {
    // Bell carries no length; guessing one would desynchronise the stream.
    const session = await settled(Buffer.from([2]));
    expect(session.screenshot().pixels[0]).toBe(0);
  });

  it("writes input to the wire and refuses it after close", async () => {
    const stream = new ScriptedStream();
    const session = new RfbGraphicalSession(stream, new ByteReader(stream), 4, 2);
    session.injectPointer(3, 1, 1);
    session.injectKey(0xff0d, true);
    expect(stream.sent()).toEqual(Buffer.concat([encodePointerEvent(3, 1, 1), encodeKeyEvent(0xff0d, true)]));

    session.close();
    expect(stream.destroyed).toBe(true);
    expect(() => session.injectPointer(0, 0, 0)).toThrow("session is closed");
    await session.settled;
  });

  it("asks for an incremental update after each batch of rectangles", async () => {
    const stream = new ScriptedStream(rawUpdate(0, 0, 1, 1, Buffer.from([1, 1, 1, 0])));
    const session = new RfbGraphicalSession(stream, new ByteReader(stream), 4, 2);
    stream.end();
    await session.settled;
    expect(stream.sent().includes(encodeUpdateRequest(4, 2, true))).toBe(true);
  });
});

describe("connect", () => {
  it("completes the handshake and asks for a full update first", async () => {
    const stream = new ScriptedStream(handshake(8, 4));
    const session = await RfbGraphicalSession.connect("host", 5900, { dial: () => stream });

    expect([session.width, session.height]).toEqual([8, 4]);
    const sent = stream.sent();
    expect(sent.subarray(0, 12).toString("ascii")).toBe("RFB 003.008\n");
    expect(sent.includes(encodeUpdateRequest(8, 4, false))).toBe(true);
    session.close();
  });

  it("destroys the stream when the handshake fails", async () => {
    const stream = new ScriptedStream(Buffer.from("HTTP/1.1 200", "ascii"));
    await expect(RfbGraphicalSession.connect("host", 5900, { dial: () => stream })).rejects.toThrow(
      "not an RFB server",
    );
    expect(stream.destroyed).toBe(true);
  });

  it("dials for real when no dial is injected", async () => {
    // Nothing listens on port 1, so this exercises the default netConnect path
    // and its failure, without a fixture standing in for the socket.
    await expect(RfbGraphicalSession.connect("127.0.0.1", 1)).rejects.toThrow();
  });

  it("does not ask for another update once closed mid-batch", async () => {
    const stream = new ScriptedStream();
    const session = new RfbGraphicalSession(stream, new ByteReader(stream), 4, 2);
    // A complete update, then close before the loop reaches its request.
    stream.push(rawUpdate(0, 0, 1, 1, Buffer.from([5, 5, 5, 0])));
    session.close();
    await session.settled;
    expect(stream.sent().includes(encodeUpdateRequest(4, 2, true))).toBe(false);
  });
});
