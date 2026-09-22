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
 */

import { describe, expect, it } from "vitest";
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

describe("a WebSocket close", () => {
  it.each([
    {
      id: "our ping timeout",
      options: { sent: frame(1011, "keepalive ping timeout") },
      expected: ["local", 1011, "keepalive ping timeout"],
      detail: "sent 1011 (internal error) keepalive ping timeout; no close frame received",
    },
    {
      id: "the peer closed",
      options: { received: frame(1001, "going away") },
      expected: ["remote", 1001, "going away"],
      detail: "received 1001 (going away) going away; no close frame sent",
    },
    {
      id: "the peer's frame came first",
      options: { received: frame(1000, ""), sent: frame(1000, ""), receivedThenSent: true },
      expected: ["remote", 1000, ""],
      detail: "received 1000 (OK); then sent 1000 (OK)",
    },
    {
      id: "our frame came first",
      options: { received: frame(1000, ""), sent: frame(1000, "bye"), receivedThenSent: false },
      expected: ["local", 1000, "bye"],
      detail: "sent 1000 (OK) bye; then received 1000 (OK)",
    },
    {
      id: "no close frames",
      options: {},
      expected: ["unknown", undefined, ""],
      detail: "no close frame received or sent",
    },
  ])("on receive: $id", async ({ options, expected, detail }) => {
    const transport = await connectedWs(failingSocket({ recv: new WebSocketClosedError("closed", options) }));

    const error = await rejection(transport.receive(4096, 1000));

    expect(error).toBeInstanceOf(TransportClosedError);
    const { close } = error as TransportClosedError;
    expect([close.initiator, close.code, close.reason]).toStrictEqual(expected);
    expect(close.detail).toBe(detail);
    expect((error as Error).message).toBe(`Connection closed (${close.summary()})`);
    expect(transport.isConnected()).toBe(false);
  });

  it("on send", async () => {
    const closed = new WebSocketClosedError("closed", { sent: frame(1011, "keepalive ping timeout") });
    const transport = await connectedWs(failingSocket({ send: closed }));

    const error = await rejection(transport.send(Uint8Array.from([104])));

    expect(error).toBeInstanceOf(TransportClosedError);
    expect((error as TransportClosedError).close).toMatchObject({ initiator: "local", code: 1011 });
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

describe("a telnet close", () => {
  it("EOF is a remote close", async () => {
    const transport = await connectedTelnet(telnetSocket({}));

    const error = await rejection(transport.receive(64, 1000));

    expect((error as Error).message).toMatch(/^Connection closed by remote/);
    expect((error as TransportClosedError).close.initiator).toBe("remote");
  });

  it.each([
    [reset(), "remote"],
    [brokenPipe(), "unknown"],
  ])("a receive loss says how: %s", async (cause, initiator) => {
    const transport = await connectedTelnet(telnetSocket({ reads: [cause] }));

    const error = await rejection(transport.receive(64, 1000));

    expect((error as Error).message).toMatch(/^Connection lost/);
    expect((error as TransportClosedError).close).toMatchObject({ initiator, detail: `Error: ${cause.message}` });
    expect((error as Error).cause).toBe(cause);
  });

  it.each([
    [reset(), "remote"],
    [brokenPipe(), "unknown"],
  ])("a send loss says how: %s", async (cause, initiator) => {
    // The opening negotiation swallows a failed write, so only the data write
    // reports it.
    const transport = await connectedTelnet(telnetSocket({ writeError: cause }));

    const error = await rejection(transport.send(Uint8Array.from([104])));

    expect(error).toBeInstanceOf(TransportClosedError);
    expect((error as Error).message).toMatch(/^Connection lost/);
    expect((error as TransportClosedError).close.initiator).toBe(initiator);
    expect(transport.isConnected()).toBe(false);
  });

  it("a receive-buffer overflow is a local close", async () => {
    const flood = Uint8Array.from([TELNET.IAC, TELNET.SB, ...new Array(TELNET_MAX_RX_BUFFER + 1).fill(97)]);
    const transport = await connectedTelnet(telnetSocket({ reads: [flood] }));

    const error = await rejection(transport.receive(TELNET_MAX_RX_BUFFER * 2, 1000));

    expect((error as Error).message).toMatch(/^telnet receive buffer exceeded/);
    expect((error as TransportClosedError).close).toMatchObject({
      initiator: "local",
      reason: "receive buffer exceeded",
    });
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
  it("is a typed close nobody initiated", async () => {
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
    expect((error as TransportClosedError).close).toMatchObject({
      initiator: "unknown",
      reason: "injected disconnect",
    });
  });
});
