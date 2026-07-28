//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  type SocketState,
  TransportConnectionError,
  WebSocketClosedError,
  type WebSocketConnectOptions,
  type WebSocketLike,
  WebSocketTransport,
} from "./index.ts";

interface WsGolden {
  default_text_frame_encoding: string;
  connects: Array<{ name: string; url: string; forwarded: Record<string, unknown>; connected: boolean }>;
  connect_failure: { message: string; causes: string[]; connected: boolean };
  sends: Array<{ name: string; bytes: number[]; text: string; is_text: boolean }>;
  receives: Array<{
    name: string;
    is_text: boolean;
    message: string | number[];
    "latin-1": number[];
    "utf-8": number[];
  }>;
  failures: {
    send_not_connected: string;
    receive_not_connected: string;
    send_closed: { message: string; connected_after: boolean };
    receive_closed: { message: string; connected_after: boolean };
    receive_error: { message: string; connected_after: boolean };
    receive_timeout: { data: number[]; connected_after: boolean };
    disconnect_idempotent: { connected_after: boolean };
    failed_reconnect: { connected_after: boolean; send_message: string; receive_message: string };
  };
  liveness: Array<{ state: string; connected: boolean }>;
}

const golden = loadGolden<WsGolden>("ws_transport_golden.json");

/**
 * The reference names its options as the Python library does; the port names
 * them as TypeScript does. The corpus is about which options survive — unset
 * dropped, zero kept, unknown dropped — not about the spelling, so the two
 * spellings are mapped here rather than leaking into the API.
 */
const OPTION_NAMES: Record<string, keyof WebSocketConnectOptions> = {
  max_size: "maxSize",
  ping_interval: "pingInterval",
  ping_timeout: "pingTimeout",
  close_timeout: "closeTimeout",
  origin: "origin",
  additional_headers: "additionalHeaders",
};

/** The connect options for a recorded case, in the port's spelling. */
function optionsFor(name: string): WebSocketConnectOptions {
  const cases: Record<string, WebSocketConnectOptions> = {
    "host and port": {},
    "explicit url wins": { url: "ws://localhost:8080/ws" },
    "empty url falls back": { url: "" },
    "forwards the tuning options": { maxSize: 65536, pingInterval: 20.0 },
    "forwards origin and headers": { origin: "https://app.example.org", additionalHeaders: { UA: "bot" } },
    "drops an option left unset": { maxSize: undefined, pingTimeout: 5.0 },
    "drops an option it does not know": { compression: "deflate" } as WebSocketConnectOptions,
    "keeps a zero": { pingInterval: 0, closeTimeout: 0.0 },
  };
  return cases[name] as WebSocketConnectOptions;
}

/** The host and port a recorded case connected with. */
function targetFor(name: string): [string, number] {
  if (name === "host and port" || name === "empty url falls back") {
    return ["bbs.example.org", 2323];
  }
  return name === "explicit url wins" ? ["ignored", 1] : ["h", 1];
}

/** Expected forwarded options in the port's spelling. */
function expectedForwarded(record: WsGolden["connects"][number]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [pythonName, value] of Object.entries(record.forwarded)) {
    out[OPTION_NAMES[pythonName] as string] = value;
  }
  return out;
}

/** Stands in for a socket, recording what reached the wire. */
class FakeSocket implements WebSocketLike {
  state: SocketState = "open";
  readonly sent: Array<string | Uint8Array> = [];
  readonly incoming: Array<string | Uint8Array> = [];
  closed = 0;
  sendError: unknown;
  recvError: unknown;
  closeError: unknown;

  async send(message: string | Uint8Array): Promise<void> {
    if (this.sendError !== undefined) {
      throw this.sendError;
    }
    this.sent.push(message);
  }

  async recv(): Promise<string | Uint8Array> {
    if (this.recvError !== undefined) {
      throw this.recvError;
    }
    const next = this.incoming.shift();
    if (next === undefined) {
      // A quiet terminal: nothing arrives until the caller's timeout fires.
      return await new Promise<never>(() => {});
    }
    return next;
  }

  async close(): Promise<void> {
    this.closed += 1;
    if (this.closeError !== undefined) {
      throw this.closeError;
    }
    this.state = "closed";
  }
}

/** A transport plus the socket and connect calls behind it. */
function harness(options: { textFrameEncoding?: "latin-1" | "utf-8"; failWith?: unknown } = {}) {
  const calls: Array<{ url: string; options: Record<string, unknown> }> = [];
  let socket = new FakeSocket();
  let failure = options.failWith;
  const transport = new WebSocketTransport({
    connect: async (url, connectOptions) => {
      calls.push({ url, options: connectOptions });
      if (failure !== undefined) {
        throw failure;
      }
      socket = new FakeSocket();
      return socket;
    },
    ...(options.textFrameEncoding === undefined ? {} : { textFrameEncoding: options.textFrameEncoding }),
  });
  return {
    transport,
    calls,
    socket: () => socket,
    /** Make the next connect fail. */
    failFrom: (error: unknown) => {
      failure = error;
    },
  };
}

describe("connect", () => {
  it.each(golden.connects)("$name", async (record) => {
    const [host, port] = targetFor(record.name);
    const { transport, calls } = harness();
    await transport.connect(host, port, optionsFor(record.name));
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(record.url);
    expect(calls[0]?.options).toStrictEqual(expectedForwarded(record));
    expect(transport.isConnected()).toBe(record.connected);
  });

  it("builds a secure URL from the host and port", async () => {
    // Falling back to ws:// would silently downgrade every session that did
    // not pass an explicit URL.
    const record = golden.connects.find((entry) => entry.name === "host and port");
    expect(record?.url.startsWith("wss://")).toBe(true);
  });

  it("prefers an explicit URL over the host and port", async () => {
    // A gateway is rarely at wss://host:port/ — the path matters.
    const record = golden.connects.find((entry) => entry.name === "explicit url wins");
    expect(record?.url).toBe("ws://localhost:8080/ws");
  });

  it("falls back when the URL is empty rather than connecting to nothing", async () => {
    const record = golden.connects.find((entry) => entry.name === "empty url falls back");
    expect(record?.url).toBe("wss://bbs.example.org:2323");
  });

  it("forwards a zero rather than treating it as unset", async () => {
    // Zero is a meaningful ping interval — it turns keepalives off.
    const record = golden.connects.find((entry) => entry.name === "keeps a zero");
    expect(expectedForwarded(record as WsGolden["connects"][number])).toStrictEqual({
      pingInterval: 0,
      closeTimeout: 0,
    });
  });

  it("reports a failure against the URL it tried", async () => {
    const cause = new Error("no route to host");
    const { transport } = harness({ failWith: cause });
    await expect(transport.connect("down.example.org", 2323)).rejects.toThrow(golden.connect_failure.message);
    expect(transport.isConnected()).toBe(golden.connect_failure.connected);
  });

  it("keeps the cause of a failed connect", async () => {
    // "Failed to connect" alone leaves an operator with no idea whether it is
    // DNS, a refused port or TLS.
    const cause = new Error("no route to host");
    const { transport } = harness({ failWith: cause });
    await expect(transport.connect("down.example.org", 2323)).rejects.toMatchObject({ cause });
  });
});

describe("a reconnect that fails", () => {
  /** A transport that connected once, then failed to reconnect. */
  async function stale() {
    const harnessed = harness();
    await harnessed.transport.connect("h", 1);
    harnessed.failFrom(new Error("no route to host"));
    await expect(harnessed.transport.connect("h", 1)).rejects.toThrow(TransportConnectionError);
    return harnessed;
  }

  it("is not reported as connected", async () => {
    // Nothing clears the old socket, so liveness has to be reading the
    // connected flag and not merely the presence of one.
    const { transport } = await stale();
    expect(transport.isConnected()).toBe(golden.failures.failed_reconnect.connected_after);
  });

  it("refuses to send into the old socket", async () => {
    // The far end is gone; writing there is a write into nothing, and the
    // caller would never learn the session had ended.
    const { transport, socket } = await stale();
    await expect(transport.send(Uint8Array.from([1]))).rejects.toThrow(golden.failures.failed_reconnect.send_message);
    expect(socket().sent).toStrictEqual([]);
  });

  it("refuses to read from the old socket", async () => {
    const { transport } = await stale();
    await expect(transport.receive(4096, 10)).rejects.toThrow(golden.failures.failed_reconnect.receive_message);
  });
});

describe("send", () => {
  it.each(golden.sends)("$name", async (record) => {
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    await transport.send(Uint8Array.from(record.bytes));
    expect(socket().sent).toStrictEqual([record.text]);
  });

  it("sends a text frame, not a binary one", async () => {
    // The library maps a string to TEXT and bytes to BINARY. A BINARY frame
    // reaches the Cloudflare Worker as a JsProxy and is dropped without an
    // error, so the session simply goes quiet.
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    await transport.send(Uint8Array.from([104, 105]));
    expect(typeof socket().sent[0]).toBe("string");
    expect(golden.sends.every((record) => record.is_text)).toBe(true);
  });

  it("replaces a byte that is not valid UTF-8 rather than failing", async () => {
    // One bad byte from a noisy line should not tear down the session.
    const record = golden.sends.find((entry) => entry.name === "a byte that is not valid utf-8");
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    await transport.send(Uint8Array.from(record?.bytes ?? []));
    expect(socket().sent[0]).toBe(record?.text);
    expect(record?.text).toContain("�");
  });

  it("refuses to send before a connect", async () => {
    const { transport } = harness();
    await expect(transport.send(Uint8Array.from([1]))).rejects.toThrow(golden.failures.send_not_connected);
  });

  it("refuses to send after a disconnect", async () => {
    const { transport } = harness();
    await transport.connect("h", 1);
    await transport.disconnect();
    await expect(transport.send(Uint8Array.from([1]))).rejects.toThrow(golden.failures.send_not_connected);
  });

  it("lets any other send fault through untouched", async () => {
    // A protocol or programming fault is not a lost connection: wrapping it
    // as one would send the caller reconnecting instead of fixing the bug.
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    socket().sendError = new RangeError("frame too large");
    await expect(transport.send(Uint8Array.from([1]))).rejects.toThrow(RangeError);
    expect(transport.isConnected()).toBe(true);
  });

  it("tears the connection down when the far end closed mid-send", async () => {
    // Leaving it up would let the caller keep writing into a socket that is
    // already gone.
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    socket().sendError = new WebSocketClosedError("closed");
    await expect(transport.send(Uint8Array.from([1]))).rejects.toThrow(golden.failures.send_closed.message);
    expect(transport.isConnected()).toBe(golden.failures.send_closed.connected_after);
  });
});

describe("receive", () => {
  it.each(golden.receives)("$name as latin-1", async (record) => {
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    socket().incoming.push(typeof record.message === "string" ? record.message : Uint8Array.from(record.message));
    expect([...(await transport.receive(4096, 1000))]).toStrictEqual(record["latin-1"]);
  });

  it.each(golden.receives)("$name as utf-8", async (record) => {
    const { transport, socket } = harness({ textFrameEncoding: "utf-8" });
    await transport.connect("h", 1);
    socket().incoming.push(typeof record.message === "string" ? record.message : Uint8Array.from(record.message));
    expect([...(await transport.receive(4096, 1000))]).toStrictEqual(record["utf-8"]);
  });

  it("defaults to the byte-oriented dialect", async () => {
    // A byte-oriented gateway puts each terminal byte in the same-valued code
    // point. Decoding that as UTF-8 turns a CP437 box-drawing byte into a
    // replacement character, and the screen fills with them.
    expect(golden.default_text_frame_encoding).toBe("latin-1");
    const record = golden.receives.find((entry) => entry.name === "cp437 box drawing as latin-1 code points");
    expect(record?.["latin-1"]).toStrictEqual([201, 205, 187]);
    expect(record?.["utf-8"]).not.toStrictEqual(record?.["latin-1"]);
  });

  it("substitutes a code point that does not fit the dialect", async () => {
    // latin-1 has no room for U+2500, and the reference substitutes rather
    // than raising — a single undrawable glyph must not kill the session.
    const record = golden.receives.find((entry) => entry.name === "high code points in a text frame");
    expect(record?.["latin-1"]).toStrictEqual([63, 63, 201]);
  });

  it("passes a binary frame through untouched", async () => {
    const record = golden.receives.find((entry) => entry.name === "binary frame");
    expect(record?.["latin-1"]).toStrictEqual(record?.message);
    expect(record?.["utf-8"]).toStrictEqual(record?.message);
  });

  it("refuses to read before a connect", async () => {
    const { transport } = harness();
    await expect(transport.receive(4096, 1000)).rejects.toThrow(golden.failures.receive_not_connected);
  });

  it("returns nothing on a read timeout and stays connected", async () => {
    // A quiet terminal is not a broken one; tearing down here would reconnect
    // every idle session.
    const { transport } = harness();
    await transport.connect("h", 1);
    expect([...(await transport.receive(4096, 10))]).toStrictEqual(golden.failures.receive_timeout.data);
    expect(transport.isConnected()).toBe(golden.failures.receive_timeout.connected_after);
  });

  it("tears the connection down when the far end closed mid-read", async () => {
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    socket().recvError = new WebSocketClosedError("closed");
    await expect(transport.receive(4096, 1000)).rejects.toThrow(golden.failures.receive_closed.message);
    expect(transport.isConnected()).toBe(golden.failures.receive_closed.connected_after);
  });

  it("tears the connection down on any other read fault", async () => {
    // Distinct message, because a protocol fault and a clean close call for
    // different operator responses.
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    socket().recvError = new RangeError("frame too large");
    await expect(transport.receive(4096, 1000)).rejects.toThrow(golden.failures.receive_error.message);
    expect(transport.isConnected()).toBe(golden.failures.receive_error.connected_after);
  });

  it("ignores the byte cap, because a frame is not divisible", async () => {
    // WebSocket is message-framed: chunking a message to max_bytes would
    // corrupt the framing, so the whole message comes back.
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    socket().incoming.push("abcdef");
    expect((await transport.receive(2, 1000)).length).toBe(6);
  });
});

describe("disconnect", () => {
  it("closes the socket", async () => {
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    const live = socket();
    await transport.disconnect();
    expect(live.closed).toBe(1);
    expect(transport.isConnected()).toBe(false);
  });

  it("survives a socket that fails to close", async () => {
    // Cleanup that raises would leave the transport wedged as connected.
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    socket().closeError = new Error("already gone");
    await transport.disconnect();
    expect(transport.isConnected()).toBe(golden.failures.disconnect_idempotent.connected_after);
  });

  it("is safe to call twice", async () => {
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    const live = socket();
    await transport.disconnect();
    await transport.disconnect();
    expect(live.closed).toBe(1);
  });

  it("is safe to call before a connect", async () => {
    const { transport } = harness();
    await transport.disconnect();
    expect(transport.isConnected()).toBe(false);
  });
});

describe("isConnected", () => {
  it.each(golden.liveness.filter((entry) => entry.state !== "never connected"))("$state", async (record) => {
    // Liveness is read from the socket, not just from a flag the transport
    // set: the far end can go without the transport being told.
    const { transport, socket } = harness();
    await transport.connect("h", 1);
    socket().state = record.state.toLowerCase() as SocketState;
    expect(transport.isConnected()).toBe(record.connected);
  });

  it("is false before any connect", () => {
    const record = golden.liveness.find((entry) => entry.state === "never connected");
    const { transport } = harness();
    expect(transport.isConnected()).toBe(record?.connected);
  });
});

describe("the error type", () => {
  it("is the shared transport error, so callers can catch one kind", async () => {
    const { transport } = harness();
    await expect(transport.send(Uint8Array.from([1]))).rejects.toBeInstanceOf(TransportConnectionError);
  });
});
