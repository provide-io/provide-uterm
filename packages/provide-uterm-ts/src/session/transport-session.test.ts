//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { encodeControlFrame, encodeTerminalData } from "../control-channel/index.ts";
import { type SessionTransport, TransportSession } from "./index.ts";

/** A transport the test feeds chunk by chunk. */
class ScriptedTransport implements SessionTransport {
  connected = false;
  closed = false;
  readonly sent: string[] = [];
  #queue: string[] = [];
  #waiters: Array<() => void> = [];

  async connect(): Promise<void> {
    this.connected = true;
  }

  async close(): Promise<void> {
    this.closed = true;
    this.connected = false;
    this.#release();
  }

  async send(data: string): Promise<void> {
    this.sent.push(data);
  }

  async receive(): Promise<string | undefined> {
    if (this.#queue.length > 0) {
      return this.#queue.shift();
    }
    await new Promise<void>((resolve) => {
      this.#waiters.push(resolve);
    });
    return this.#queue.shift();
  }

  /** Hand the reader loop one chunk and let it settle. */
  async feed(chunk: string): Promise<void> {
    this.#queue.push(chunk);
    this.#release();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  #release(): void {
    const waiters = this.#waiters;
    this.#waiters = [];
    for (const resolve of waiters) {
      resolve();
    }
  }
}

/** A connected session over a scripted transport. */
async function build(options: { controlChannel?: boolean } = {}) {
  const transport = new ScriptedTransport();
  const session = new TransportSession({
    transport,
    ...(options.controlChannel === true ? { controlChannel: true } : {}),
  });
  await session.connect();
  return { transport, session };
}

describe("TransportSession lifecycle", () => {
  it("connects its transport", async () => {
    const { transport, session } = await build();
    expect(transport.connected).toBe(true);
    expect(session.isConnected()).toBe(true);
    await session.close();
  });

  it("closes its transport and stops reading", async () => {
    const { transport, session } = await build();
    await session.close();
    expect(transport.closed).toBe(true);
    expect(session.isConnected()).toBe(false);
  });

  it("is idempotent about closing", async () => {
    const { session } = await build();
    await session.close();
    await expect(session.close()).resolves.toBeUndefined();
  });

  it("ends the session when the transport fails rather than crashing", async () => {
    // A transport that throws mid-read is a disconnection, not a bug to
    // propagate into whatever happened to be awaiting.
    const transport = new ScriptedTransport();
    const session = new TransportSession({ transport });
    transport.receive = async () => {
      throw new Error("connection reset");
    };
    await session.connect();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(session.isConnected()).toBe(false);
  });

  it("takes the terminal size it is given", async () => {
    const transport = new ScriptedTransport();
    const session = new TransportSession({ transport, cols: 100, rows: 40 });
    await session.connect();
    expect(session.snapshot().cols).toBe(100);
    expect(session.snapshot().rows).toBe(40);
    await session.close();
  });

  it("waits five seconds for a screen change by default", async () => {
    // The default matters because callers that omit it are relying on it;
    // the wait is started and abandoned rather than sat through.
    const { transport, session } = await build();
    await transport.feed("x");
    const waiting = session.waitForScreenChange({ since: session.screenChangeSeq() });
    await transport.feed("y");
    expect(await waiting).toBe(true);
    await session.close();
  });

  it("ignores an empty chunk", async () => {
    // Some transports surface a keepalive as a zero-length read; drawing it
    // would advance the change counter for nothing.
    const { transport, session } = await build();
    await transport.feed("");
    expect(session.screenChangeSeq()).toBe(0);
    await session.close();
  });

  it("forwards sends to the transport", async () => {
    const { transport, session } = await build();
    await session.send("ls\r");
    expect(transport.sent).toStrictEqual(["ls\r"]);
    await session.close();
  });
});

describe("TransportSession screen", () => {
  it("feeds received bytes into the emulator", async () => {
    const { transport, session } = await build();
    await transport.feed("hello");
    expect(session.snapshot().screen).toContain("hello");
    await session.close();
  });

  it("advances a sequence on every update", async () => {
    // Callers capture this before sending so they can tell a fresh screen
    // from the one that was already there.
    const { transport, session } = await build();
    expect(session.screenChangeSeq()).toBe(0);
    await transport.feed("a");
    expect(session.screenChangeSeq()).toBe(1);
    await transport.feed("b");
    expect(session.screenChangeSeq()).toBe(2);
    await session.close();
  });

  it("exposes the rendered screen with its escapes intact", async () => {
    const { transport, session } = await build();
    await transport.feed("[31mred[0m");
    expect(session.ansiScreen()).toContain("red");
    await session.close();
  });
});

describe("TransportSession waiting", () => {
  it("reports an update that arrives", async () => {
    const { transport, session } = await build();
    const waiting = session.waitForUpdate({ timeoutMs: 1000 });
    await transport.feed("x");
    expect(await waiting).toBe(true);
    await session.close();
  });

  it("reports a timeout when nothing arrives", async () => {
    const { session } = await build();
    expect(await session.waitForUpdate({ timeoutMs: 5 })).toBe(false);
    await session.close();
  });

  it("returns at once when the screen has already moved past the mark", async () => {
    // The point of the sequence: output landing between the send and the
    // wait must not be slept through.
    const { transport, session } = await build();
    const before = session.screenChangeSeq();
    await transport.feed("x");
    expect(await session.waitForScreenChange({ timeoutMs: 5, since: before })).toBe(true);
    await session.close();
  });

  it("waits when the screen has not moved", async () => {
    const { transport, session } = await build();
    await transport.feed("x");
    const since = session.screenChangeSeq();
    const waiting = session.waitForScreenChange({ timeoutMs: 1000, since });
    await transport.feed("y");
    expect(await waiting).toBe(true);
    await session.close();
  });

  it("gives up on the deadline", async () => {
    const { transport, session } = await build();
    await transport.feed("x");
    const since = session.screenChangeSeq();
    expect(await session.waitForScreenChange({ timeoutMs: 5, since })).toBe(false);
    await session.close();
  });

  it("reports a stall when the mark is already the deadline", async () => {
    // A zero-length wait is a question, not a pause: has the screen moved
    // already? It must answer without scheduling anything.
    const { session } = await build();
    expect(await session.waitForScreenChange({ timeoutMs: 0, since: 0 })).toBe(false);
    await session.close();
  });

  it("waits for any change when given no mark", async () => {
    const { transport, session } = await build();
    const waiting = session.waitForScreenChange({ timeoutMs: 1000 });
    await transport.feed("x");
    expect(await waiting).toBe(true);
    await session.close();
  });

  it("reports a change that landed during a timed-out wait", async () => {
    // A wait that expires having still seen progress should say so rather
    // than reporting a stall that did not happen.
    const { transport, session } = await build();
    await transport.feed("x");
    expect(await session.waitForScreenChange({ timeoutMs: 5, since: 0 })).toBe(true);
    await session.close();
  });
});

describe("TransportSession watchers", () => {
  it("hands raw bytes to a watcher", async () => {
    // Before the emulator consumes them, so a watcher sees the wire content
    // rather than the decoded display.
    const seen: string[] = [];
    const { transport, session } = await build();
    session.addWatch((_snapshot, raw) => seen.push(raw));
    await transport.feed("[31mred");
    expect(seen).toStrictEqual(["[31mred"]);
    await session.close();
  });

  it("calls a watcher before the emulator has drawn the chunk", async () => {
    // The claim the raw tap rests on: a watcher sees the wire content and a
    // screen that does not yet include it, so it can diff before and after.
    const screensAtWatchTime: string[] = [];
    const { transport, session } = await build();
    session.addWatch(() => {
      screensAtWatchTime.push(String(session.snapshot().screen));
    });
    await transport.feed("first");
    await transport.feed("second");
    expect(screensAtWatchTime[0]).not.toContain("first");
    expect(screensAtWatchTime[1]).toContain("first");
    expect(screensAtWatchTime[1]).not.toContain("second");
    await session.close();
  });

  it("keeps going when a watcher throws", async () => {
    // A watcher is an observer; a broken one must not stop the session
    // reading its own transport.
    const seen: string[] = [];
    const { transport, session } = await build();
    session.addWatch(() => {
      throw new Error("watcher exploded");
    });
    session.addWatch((_snapshot, raw) => seen.push(raw));
    await transport.feed("data");
    expect(seen).toStrictEqual(["data"]);
    expect(session.snapshot().screen).toContain("data");
    await session.close();
  });
});

describe("TransportSession control frames", () => {
  it("passes plain terminal data straight through", async () => {
    const { transport, session } = await build({ controlChannel: true });
    await transport.feed(encodeTerminalData("hello"));
    expect(session.snapshot().screen).toContain("hello");
    await session.close();
  });

  it("routes a control frame to its watchers and not to the screen", async () => {
    // A control frame rendered onto the terminal would show the operator raw
    // JSON where their output should be.
    const seen: Array<Record<string, unknown>> = [];
    const { transport, session } = await build({ controlChannel: true });
    session.addControlFrameWatch((frame) => seen.push(frame));
    await transport.feed(encodeControlFrame({ type: "hijack_state", hijacked: true }));
    expect(seen).toStrictEqual([{ type: "hijack_state", hijacked: true }]);
    expect(session.snapshot().screen).not.toContain("hijack_state");
    await session.close();
  });

  it("does not count a control-only chunk as a screen change", async () => {
    // Nothing was drawn, so a caller waiting for the screen to move must not
    // be woken by it.
    const { transport, session } = await build({ controlChannel: true });
    const before = session.screenChangeSeq();
    await transport.feed(encodeControlFrame({ type: "ping" }));
    expect(session.screenChangeSeq()).toBe(before);
    await session.close();
  });

  it("keeps the terminal half of a mixed chunk", async () => {
    const seen: Array<Record<string, unknown>> = [];
    const { transport, session } = await build({ controlChannel: true });
    session.addControlFrameWatch((frame) => seen.push(frame));
    await transport.feed(encodeControlFrame({ type: "ping" }) + encodeTerminalData("after"));
    expect(seen).toHaveLength(1);
    expect(session.snapshot().screen).toContain("after");
    await session.close();
  });

  it("keeps going when a control watcher throws", async () => {
    const seen: Array<Record<string, unknown>> = [];
    const { transport, session } = await build({ controlChannel: true });
    session.addControlFrameWatch(() => {
      throw new Error("watcher exploded");
    });
    session.addControlFrameWatch((frame) => seen.push(frame));
    await transport.feed(encodeControlFrame({ type: "ping" }));
    expect(seen).toHaveLength(1);
    await session.close();
  });

  it("leaves frames alone when the control channel is off", async () => {
    // Without it the bytes are just terminal output, escape codes and all.
    const { transport, session } = await build();
    await transport.feed(encodeControlFrame({ type: "ping" }));
    expect(session.screenChangeSeq()).toBe(1);
    await session.close();
  });
});

describe("TransportSession capture", () => {
  it("records terminal text while the scope is open", async () => {
    const { transport, session } = await build();
    const capture = session.beginCapture();
    await transport.feed("hello ");
    await transport.feed("world");
    expect(capture.text).toBe("hello world");
    session.endCapture(capture);
    await session.close();
  });

  it("stops recording once the scope closes", async () => {
    // Otherwise a capture becomes session-wide history and grows with the
    // session rather than with the operation someone asked about.
    const { transport, session } = await build();
    const capture = session.beginCapture();
    await transport.feed("during");
    session.endCapture(capture);
    await transport.feed("after");
    expect(capture.text).toBe("during");
    await session.close();
  });

  it("keeps control frames out of the capture", async () => {
    const { transport, session } = await build({ controlChannel: true });
    const capture = session.beginCapture();
    await transport.feed(encodeControlFrame({ type: "ping" }));
    await transport.feed(encodeTerminalData("visible"));
    expect(capture.text).toBe("visible");
    session.endCapture(capture);
    await session.close();
  });

  it("feeds several captures at once", async () => {
    const { transport, session } = await build();
    const first = session.beginCapture();
    const second = session.beginCapture();
    await transport.feed("shared");
    expect(first.text).toBe("shared");
    expect(second.text).toBe("shared");
    session.endCapture(first);
    session.endCapture(second);
    await session.close();
  });

  it("ignores an unknown capture on close", async () => {
    const { session } = await build();
    const capture = session.beginCapture();
    session.endCapture(capture);
    expect(() => session.endCapture(capture)).not.toThrow();
    await session.close();
  });

  it("honours the requested bound", async () => {
    const { transport, session } = await build();
    const capture = session.beginCapture(4);
    await transport.feed("abcdefgh");
    expect(capture.text).toBe("efgh");
    session.endCapture(capture);
    await session.close();
  });
});
