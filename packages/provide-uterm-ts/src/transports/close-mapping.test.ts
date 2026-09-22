//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Every transport reports the end of a connection as a typed close.
 *
 * Port of `provide-uterm-client/tests/transports/test_transport_close_mapping.py`.
 * A WebSocket knows which side sent the close frame, its code and its reason;
 * a telnet socket knows whether the peer sent EOF, reset the connection, or
 * whether this end gave up on it. These pin that each transport passes that on
 * as a `TransportClosedError` instead of flattening it into a message.
 *
 * The cases are `close_cases` from `spec/behavior_vectors.json`, shared with
 * the Python, Go and C# suites; the tests after them are this port's own.
 */

import { describe, expect, it } from "vitest";
import {
  type CloseFrameVector,
  type EventCase,
  type ExpectedClose,
  loadCloseCases,
  vectorCode,
  type WebSocketCase,
} from "../testing/close-vectors.ts";
import {
  ChaosTransport,
  type ConnectionTransport,
  closeFromSocketError,
  closeFromWebSocket,
  describeWebSocketClose,
  TELNET,
  TELNET_MAX_RX_BUFFER,
  type TelnetSocket,
  TelnetTransport,
  type TransportClose,
  TransportClosedError,
  TransportConnectionError,
  WebSocketClosedError,
  type WebSocketCloseFrame,
  type WebSocketLike,
  WebSocketTransport,
} from "./index.ts";

/** A socket whose next send or receive fails with `error`. */
function failingSocket(error: { send?: unknown; recv?: unknown }): WebSocketLike {
  return {
    state: "open",
    send: async () => {
      if (error.send !== undefined) {
        throw error.send;
      }
    },
    recv: async () => {
      throw error.recv;
    },
    close: async () => undefined,
  };
}

/** A connected WebSocket transport over `socket`. */
async function connectedWs(socket: WebSocketLike): Promise<WebSocketTransport> {
  const transport = new WebSocketTransport({ connect: async () => socket });
  await transport.connect("h", 1);
  return transport;
}

/** Whatever `operation` rejected with. */
async function rejection(operation: Promise<unknown>): Promise<unknown> {
  try {
    await operation;
  } catch (error) {
    return error;
  }
  throw new Error("expected the operation to fail");
}

const frame = (code: number, reason: string): WebSocketCloseFrame => ({ code, reason });

const CLOSE_CASES = loadCloseCases();

/** The close frames a vector exchanged, as the socket layer reports them. */
function closedBy(vector: WebSocketCase): WebSocketClosedError {
  const toFrame = (value: CloseFrameVector | null) => (value === null ? undefined : frame(value.code, value.reason));
  const received = toFrame(vector.received);
  const sent = toFrame(vector.sent);
  return new WebSocketClosedError("closed", {
    ...(received === undefined ? {} : { received }),
    ...(sent === undefined ? {} : { sent }),
    ...(vector.received_then_sent === null ? {} : { receivedThenSent: vector.received_then_sent }),
  });
}

/** Assert `close` is the one `vector` expects. */
function expectClose(close: TransportClose, vector: ExpectedClose): void {
  expect([close.initiator, close.code, close.reason]).toStrictEqual([
    vector.initiator,
    vectorCode(vector.code),
    vector.reason,
  ]);
}

describe("a WebSocket close", () => {
  it.each(CLOSE_CASES.websocket)("on receive: $name", async (vector) => {
    const transport = await connectedWs(failingSocket({ recv: closedBy(vector) }));

    const error = await rejection(transport.receive(4096, 1000));

    expect(error).toBeInstanceOf(TransportClosedError);
    const { close } = error as TransportClosedError;
    expectClose(close, vector);
    expect(close.detail).toBe(vector.detail);
    expect((error as Error).message).toBe(`Connection closed (${close.summary()})`);
    expect(transport.isConnected()).toBe(false);
  });

  it.each(CLOSE_CASES.websocket)("on send: $name", async (vector) => {
    const closed = closedBy(vector);
    const transport = await connectedWs(failingSocket({ send: closed }));

    const error = await rejection(transport.send(Uint8Array.from([104])));

    expect(error).toBeInstanceOf(TransportClosedError);
    expectClose((error as TransportClosedError).close, vector);
    expect((error as TransportClosedError).close.detail).toBe(vector.detail);
    expect((error as Error).cause).toBe(closed);
  });

  it("any other receive failure is an unknown close", async () => {
    const transport = await connectedWs(failingSocket({ recv: new Error("kaboom") }));

    const error = await rejection(transport.receive(4096, 1000));

    expect((error as Error).message).toMatch(/^WebSocket receive error/);
    expect((error as TransportClosedError).close).toMatchObject({ initiator: "unknown", detail: "Error: kaboom" });
  });

  it("carries its frames and cause", () => {
    const cause = new Error("socket");
    const closed = new WebSocketClosedError("closed", { cause, received: frame(1000, "") });
    expect(closed.name).toBe("WebSocketClosedError");
    expect(closed.cause).toBe(cause);
    expect(closed.received).toStrictEqual(frame(1000, ""));
    expect(closed.sent).toBeUndefined();
    expect(closed.receivedThenSent).toBe(false);
    expect("cause" in new WebSocketClosedError("closed")).toBe(false);
  });

  it.each([
    [1000, "OK"],
    [1015, "TLS handshake failure [internal]"],
    [1004, "unknown"],
    [2999, "unknown"],
    [3000, "registered"],
    [3999, "registered"],
    [4000, "private use"],
    [4999, "private use"],
    [5000, "unknown"],
  ])("explains code %i as the reference does", (code, explanation) => {
    const closed = new WebSocketClosedError("closed", { received: frame(code, "") });
    expect(describeWebSocketClose(closed)).toBe(`received ${code} (${explanation}); no close frame sent`);
  });

  it("attributes a lone received frame even when told it came second", () => {
    // `receivedThenSent` only orders two frames; with one, that one decides.
    const closed = new WebSocketClosedError("closed", { received: frame(1001, ""), receivedThenSent: false });
    expect(closeFromWebSocket(closed).initiator).toBe("remote");
  });
});

/** A telnet socket that reads `reads` in turn, failing on `writeError`. */
function telnetSocket(options: { reads?: Array<Uint8Array | Error>; writeError?: Error }): TelnetSocket {
  const reads = [...(options.reads ?? [])];
  return {
    closing: false,
    read: async () => {
      const next = reads.shift() ?? new Uint8Array(0);
      if (next instanceof Error) {
        throw next;
      }
      return next;
    },
    write: async () => {
      if (options.writeError !== undefined) {
        throw options.writeError;
      }
    },
    close: async () => undefined,
    peerAddress: () => undefined,
  };
}

/** A connected telnet transport over `socket`. */
async function connectedTelnet(socket: TelnetSocket): Promise<TelnetTransport> {
  const transport = new TelnetTransport({ connect: async () => socket });
  await transport.connect("h", 1);
  return transport;
}

const reset = () => Object.assign(new Error("read ECONNRESET"), { code: "ECONNRESET" });
const brokenPipe = () => Object.assign(new Error("write EPIPE"), { code: "EPIPE" });

/** The socket error a vector's event stands for. */
const socketError = (event: string) => (event === "reset" ? reset() : brokenPipe());

/** A telnet transport that meets `vector`'s event on its operation. */
async function telnetMeeting(vector: EventCase): Promise<unknown> {
  if (vector.event === "rx_buffer_cap") {
    const flood = Uint8Array.from([TELNET.IAC, TELNET.SB, ...new Array(TELNET_MAX_RX_BUFFER + 1).fill(97)]);
    const transport = await connectedTelnet(telnetSocket({ reads: [flood] }));
    return rejection(transport.receive(TELNET_MAX_RX_BUFFER * 2, 1000));
  }
  if (vector.event === "eof") {
    return rejection((await connectedTelnet(telnetSocket({}))).receive(64, 1000));
  }
  if (vector.operation === "receive") {
    const transport = await connectedTelnet(telnetSocket({ reads: [socketError(vector.event)] }));
    return rejection(transport.receive(64, 1000));
  }
  // The opening negotiation swallows a failed write, so only the data write
  // reports it.
  const transport = await connectedTelnet(telnetSocket({ writeError: socketError(vector.event) }));
  return rejection(transport.send(Uint8Array.from([104])));
}

// The message prefix is this transport's own wording; the close is the shared contract.
const TELNET_PREFIXES: Record<string, RegExp> = {
  eof: /^Connection closed by remote/,
  reset: /^Connection lost/,
  broken_pipe: /^Connection lost/,
  rx_buffer_cap: /^telnet receive buffer exceeded/,
};

describe("a telnet close", () => {
  it.each(CLOSE_CASES.telnet)("$name", async (vector) => {
    const error = await telnetMeeting(vector);

    expect(error).toBeInstanceOf(TransportClosedError);
    expect((error as Error).message).toMatch(TELNET_PREFIXES[vector.event] as RegExp);
    expectClose((error as TransportClosedError).close, vector);
  });

  it.each([
    [reset(), "remote"],
    [brokenPipe(), "unknown"],
  ])("a receive loss names the socket error: %s", async (cause, initiator) => {
    const transport = await connectedTelnet(telnetSocket({ reads: [cause] }));

    const error = await rejection(transport.receive(64, 1000));

    expect((error as TransportClosedError).close).toMatchObject({ initiator, detail: `Error: ${cause.message}` });
    expect((error as Error).cause).toBe(cause);
  });

  it("a send loss disconnects", async () => {
    const transport = await connectedTelnet(telnetSocket({ writeError: reset() }));

    await rejection(transport.send(Uint8Array.from([104])));

    expect(transport.isConnected()).toBe(false);
  });

  it("not being connected is not a close", async () => {
    const error = await rejection(new TelnetTransport({ connect: async () => telnetSocket({}) }).receive(64, 1000));

    expect(error).toBeInstanceOf(TransportConnectionError);
    expect(error).not.toBeInstanceOf(TransportClosedError);
  });

  it("classifies a socket error by its code", () => {
    expect(closeFromSocketError(reset()).initiator).toBe("remote");
    expect(closeFromSocketError(brokenPipe()).initiator).toBe("unknown");
    expect(closeFromSocketError(new Error("no code")).initiator).toBe("unknown");
    expect(closeFromSocketError("not an error")).toMatchObject({ initiator: "unknown", detail: "not an error" });
  });
});

describe("an injected chaos disconnect", () => {
  it.each(CLOSE_CASES.chaos)("$name", async (vector) => {
    const inner: ConnectionTransport = {
      connect: async () => undefined,
      disconnect: async () => undefined,
      send: async () => undefined,
      receive: async () => new Uint8Array(0),
      isConnected: () => false,
    };
    const chaos = new ChaosTransport(inner, { disconnectEveryNReceives: 1, label: "chaos" });

    const error = await rejection(chaos.receive(64, 100));

    expect(error).toBeInstanceOf(TransportClosedError);
    expect((error as Error).message).toMatch(/^chaos: injected disconnect on receive #1/);
    expectClose((error as TransportClosedError).close, vector);
  });
});
