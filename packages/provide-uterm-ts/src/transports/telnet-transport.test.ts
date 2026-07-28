//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  TELNET,
  TELNET_CONNECT_TIMEOUT_S,
  TELNET_MAX_RX_BUFFER,
  TELNET_TRANSPORT_DEFAULTS,
  type TelnetSocket,
  TelnetTransport,
  TransportConnectionError,
} from "./index.ts";

interface TelnetTransportGolden {
  cols: number;
  rows: number;
  term: string;
  handshake: { opening_offer: number[]; connected: boolean };
  answers: Array<{ name: string; incoming: number[]; payload: number[]; reply: number[] }>;
  sends: Array<{ name: string; input: number[]; wire: number[] }>;
  receives: Array<{ name: string; chunks: number[][]; payloads: number[][] }>;
  failures: {
    send_before_connect: string;
    receive_before_connect: string;
    set_size_before_connect: string;
    closed_with_nothing_buffered: string;
    closed_leaves_it_disconnected: boolean;
    partial_then_close: { first: number[]; second: number[] };
    unbounded_subnegotiation: string;
  };
  set_size: { cols: number; rows: number; wire: number[] };
  sequences: {
    incoming_will_suppresses_our_will: number[];
    naws_after_resize: number[];
    naws_with_a_command_byte: number[];
  };
  max_rx_buffer: number;
  default_connect_timeout_s: number;
  peer_ip: string;
}

const golden = loadGolden<TelnetTransportGolden>("telnet_transport_golden.json");

/** A socket that hands out queued chunks and records what was written. */
class FakeSocket implements TelnetSocket {
  closing = false;
  readonly written: number[] = [];
  readonly #chunks: Uint8Array[];
  writeError: unknown;

  constructor(chunks: Uint8Array[] = []) {
    this.#chunks = [...chunks];
  }

  async read(): Promise<Uint8Array> {
    // Empty when drained: end of stream, which is what the far end going
    // looks like.
    return this.#chunks.shift() ?? new Uint8Array(0);
  }

  async write(data: Uint8Array): Promise<void> {
    if (this.writeError !== undefined) {
      throw this.writeError;
    }
    this.written.push(...data);
  }

  async close(): Promise<void> {
    this.closing = true;
  }

  peerAddress(): string | undefined {
    return golden.peer_ip;
  }
}

/** A transport wired to a fake socket, already connected. */
async function connected(chunks: number[][] = []) {
  const socket = new FakeSocket(chunks.map((chunk) => Uint8Array.from(chunk)));
  const transport = new TelnetTransport({ connect: async () => socket });
  await transport.connect("bbs.example.org", 2323, {
    cols: golden.cols,
    rows: golden.rows,
    term: golden.term,
  });
  socket.written.length = 0;
  return { transport, socket };
}

describe("the opening offer", () => {
  it("is what the reference sends", async () => {
    // A client that opens without offering anything gets a line-mode,
    // seven-bit session, and a BBS then draws for the wrong terminal.
    const socket = new FakeSocket();
    const transport = new TelnetTransport({ connect: async () => socket });
    await transport.connect("bbs.example.org", 2323, { cols: golden.cols, rows: golden.rows, term: golden.term });
    expect(socket.written).toStrictEqual(golden.handshake.opening_offer);
    expect(transport.isConnected()).toBe(golden.handshake.connected);
  });

  it("offers binary and suppress-go-ahead", () => {
    expect(golden.handshake.opening_offer).toStrictEqual([
      TELNET.IAC,
      TELNET.WILL,
      TELNET.OPT_BINARY,
      TELNET.IAC,
      TELNET.WILL,
      TELNET.OPT_SGA,
    ]);
  });

  it("reports a failure against the host it tried", async () => {
    const transport = new TelnetTransport({
      connect: async () => {
        throw new Error("ECONNREFUSED");
      },
    });
    await expect(transport.connect("down.example.org", 2323)).rejects.toThrow(
      "Failed to connect to down.example.org:2323",
    );
    expect(transport.isConnected()).toBe(false);
  });

  it("keeps the cause of a failed connect", async () => {
    const cause = new Error("ECONNREFUSED");
    const transport = new TelnetTransport({
      connect: async () => {
        throw cause;
      },
    });
    await expect(transport.connect("down.example.org", 2323)).rejects.toMatchObject({ cause });
  });

  it("closes an earlier connection before opening another", async () => {
    // Otherwise a reconnect leaks the socket it replaced.
    const first = new FakeSocket();
    const second = new FakeSocket();
    let next = 0;
    const transport = new TelnetTransport({ connect: async () => (next++ === 0 ? first : second) });
    await transport.connect("a", 1);
    await transport.connect("b", 2);
    expect(first.closing).toBe(true);
    expect(second.closing).toBe(false);
  });

  it("uses the recorded defaults", () => {
    expect(TELNET_TRANSPORT_DEFAULTS.cols).toBe(golden.cols);
    expect(TELNET_TRANSPORT_DEFAULTS.rows).toBe(golden.rows);
    expect(TELNET_TRANSPORT_DEFAULTS.term).toBe(golden.term);
    expect(TELNET_CONNECT_TIMEOUT_S).toBe(golden.default_connect_timeout_s);
  });
});

describe("answering a negotiation", () => {
  it.each(golden.answers)("$name", async (record) => {
    const { transport, socket } = await connected([record.incoming]);
    const payload = await transport.receive(4096, 100);
    expect([...payload]).toStrictEqual(record.payload);
    expect(socket.written).toStrictEqual(record.reply);
  });

  it("does not repeat an offer it already made", async () => {
    // Two polite implementations that answered every message would negotiate
    // at each other forever.
    const binary = golden.answers.find((entry) => entry.name === "do binary");
    const sga = golden.answers.find((entry) => entry.name === "do sga");
    expect(binary?.reply).toStrictEqual([]);
    expect(sga?.reply).toStrictEqual([]);
  });

  it("follows a window-size agreement with the size", async () => {
    // Agreeing and then never saying how big leaves the server guessing.
    const record = golden.answers.find((entry) => entry.name === "do naws");
    expect(record?.reply.slice(0, 3)).toStrictEqual([TELNET.IAC, TELNET.WILL, TELNET.OPT_NAWS]);
    expect(record?.reply.slice(3)).toStrictEqual([
      TELNET.IAC,
      TELNET.SB,
      TELNET.OPT_NAWS,
      0,
      golden.cols,
      0,
      golden.rows,
      TELNET.IAC,
      TELNET.SE,
    ]);
  });

  it("follows a terminal-type agreement with the type", async () => {
    const record = golden.answers.find((entry) => entry.name === "do ttype");
    expect(record?.reply.slice(0, 3)).toStrictEqual([TELNET.IAC, TELNET.WILL, TELNET.OPT_TTYPE]);
    expect(record?.reply.slice(3)).toStrictEqual([
      TELNET.IAC,
      TELNET.SB,
      TELNET.OPT_TTYPE,
      0,
      ...[...golden.term].map((character) => character.charCodeAt(0)),
      TELNET.IAC,
      TELNET.SE,
    ]);
  });

  it("refuses an option it does not implement", async () => {
    // Silence would leave the far end waiting; WONT is an answer.
    for (const name of ["do echo", "do something unknown"]) {
      const record = golden.answers.find((entry) => entry.name === name);
      expect(record?.reply[1]).toBe(TELNET.WONT);
    }
  });

  it("accepts the options worth having from the far end", async () => {
    for (const name of ["will echo", "will sga"]) {
      const record = golden.answers.find((entry) => entry.name === name);
      expect(record?.reply[1]).toBe(TELNET.DO);
    }
  });

  it("refuses one it does not want", async () => {
    const record = golden.answers.find((entry) => entry.name === "will something unknown");
    expect(record?.reply[1]).toBe(TELNET.DONT);
  });

  it("answers a refusal with the matching refusal", async () => {
    expect(golden.answers.find((entry) => entry.name === "dont binary")?.reply[1]).toBe(TELNET.WONT);
    expect(golden.answers.find((entry) => entry.name === "wont echo")?.reply[1]).toBe(TELNET.DONT);
  });

  it("answers a terminal-type request", async () => {
    // The server asks for it in a subnegotiation rather than a DO.
    const record = golden.answers.find((entry) => entry.name === "a ttype request");
    expect(record?.reply).toStrictEqual([
      TELNET.IAC,
      TELNET.SB,
      TELNET.OPT_TTYPE,
      0,
      ...[...golden.term].map((character) => character.charCodeAt(0)),
      TELNET.IAC,
      TELNET.SE,
    ]);
  });

  it("does not answer an option the far end already settled", async () => {
    // An incoming WILL records the option, so this end does not then send
    // its own WILL for it. Two implementations that both answered every
    // message would negotiate at each other forever.
    const { transport, socket } = await connected([
      [TELNET.IAC, TELNET.WILL, TELNET.OPT_NAWS],
      [TELNET.IAC, TELNET.DO, TELNET.OPT_NAWS],
    ]);
    await transport.receive(4096, 100);
    socket.written.length = 0;
    await transport.receive(4096, 100);
    expect(socket.written).toStrictEqual(golden.sequences.incoming_will_suppresses_our_will);
  });

  it("keeps a negotiation off the screen", async () => {
    expect(golden.answers.every((record) => record.payload.length === 0)).toBe(true);
  });
});

describe("sending", () => {
  it.each(golden.sends)("$name", async (record) => {
    const { transport, socket } = await connected();
    await transport.send(Uint8Array.from(record.input));
    expect(socket.written).toStrictEqual(record.wire);
  });

  it("doubles a command byte", async () => {
    // Otherwise the far end reads what the user typed as the start of a
    // command and swallows the next byte.
    const record = golden.sends.find((entry) => entry.name === "a command byte");
    expect(record?.wire).toStrictEqual([97, TELNET.IAC, TELNET.IAC, 98]);
  });

  it("remaps delete to backspace", async () => {
    // A browser terminal sends DEL for the backspace key; a BBS deletes with
    // backspace and prints DEL as a stray character.
    const record = golden.sends.find((entry) => entry.name === "a delete");
    expect(record?.wire.at(-1)).toBe(0x08);
    expect(record?.wire).not.toContain(0x7f);
  });

  it("does both at once", async () => {
    const record = golden.sends.find((entry) => entry.name === "a delete and a command byte");
    expect(record?.wire).toStrictEqual([0x08, TELNET.IAC, TELNET.IAC]);
  });

  it("leaves other high bytes alone", async () => {
    const record = golden.sends.find((entry) => entry.name === "high bytes that are not commands");
    expect(record?.wire).toStrictEqual(record?.input);
  });

  it("tears the connection down when the far end went mid-send", async () => {
    const { transport, socket } = await connected();
    socket.writeError = new Error("EPIPE");
    await expect(transport.send(Uint8Array.from([1]))).rejects.toThrow("Connection lost");
    expect(transport.isConnected()).toBe(false);
  });
});

describe("receiving", () => {
  it.each(golden.receives)("$name", async (record) => {
    const { transport } = await connected(record.chunks);
    const payloads: number[][] = [];
    for (const _chunk of record.chunks) {
      payloads.push([...(await transport.receive(4096, 100))]);
    }
    expect(payloads).toStrictEqual(record.payloads);
  });

  it("joins a sequence split across reads", async () => {
    // A socket splits wherever it likes, including mid-command.
    const record = golden.receives.find((entry) => entry.name === "a sequence split across reads");
    expect(record?.payloads).toStrictEqual([[97], []]);
  });

  it("returns nothing on a read timeout and stays connected", async () => {
    // A quiet terminal is not a broken one.
    const socket = new FakeSocket();
    socket.read = async () => new Promise<Uint8Array>(() => {});
    const transport = new TelnetTransport({ connect: async () => socket });
    await transport.connect("h", 1);
    expect([...(await transport.receive(4096, 10))]).toStrictEqual([]);
    expect(transport.isConnected()).toBe(true);
  });

  it("hands over what it was holding when the far end goes", async () => {
    // A trailing command byte is data once nothing more is coming; dropping
    // it would lose bytes the server did send.
    const { transport } = await connected([[97, TELNET.IAC]]);
    expect([...(await transport.receive(4096, 100))]).toStrictEqual(golden.failures.partial_then_close.first);
    expect([...(await transport.receive(4096, 100))]).toStrictEqual(golden.failures.partial_then_close.second);
  });

  it("refuses once the far end has gone and there is nothing left", async () => {
    const { transport } = await connected();
    await expect(transport.receive(4096, 100)).rejects.toThrow(golden.failures.closed_with_nothing_buffered);
    expect(transport.isConnected()).toBe(golden.failures.closed_leaves_it_disconnected);
  });

  it("refuses a subnegotiation that never ends", async () => {
    // It has no length bound, so an upstream that never closes one would grow
    // the buffer until the process died.
    const flood = [TELNET.IAC, TELNET.SB, TELNET.OPT_TTYPE, ...new Array(TELNET_MAX_RX_BUFFER + 16).fill(120)];
    const { transport } = await connected([flood]);
    await expect(transport.receive(flood.length + 16, 100)).rejects.toThrow(golden.failures.unbounded_subnegotiation);
  });

  it("uses the recorded buffer cap", () => {
    expect(TELNET_MAX_RX_BUFFER).toBe(golden.max_rx_buffer);
  });

  it("tears the connection down when the read itself fails", async () => {
    const socket = new FakeSocket();
    socket.read = async () => {
      throw new Error("ECONNRESET");
    };
    const transport = new TelnetTransport({ connect: async () => socket });
    await transport.connect("h", 1);
    await expect(transport.receive(4096, 100)).rejects.toThrow("Connection lost");
    expect(transport.isConnected()).toBe(false);
  });
});

describe("resizing", () => {
  it("sends the new size", async () => {
    // A terminal that resized and never said so is drawn for the old
    // geometry until something else forces a redraw.
    const { transport, socket } = await connected();
    await transport.setSize(golden.set_size.cols, golden.set_size.rows);
    expect(socket.written).toStrictEqual(golden.set_size.wire);
  });

  it("remembers it for the next negotiation", async () => {
    // A resize that is not remembered leaves the next NAWS reporting the
    // geometry from connect, and the server draws for a window that is gone.
    const { transport, socket } = await connected([[TELNET.IAC, TELNET.DO, TELNET.OPT_NAWS]]);
    await transport.setSize(132, 43);
    socket.written.length = 0;
    await transport.receive(4096, 100);
    expect(socket.written).toStrictEqual(golden.sequences.naws_after_resize);
  });

  it("escapes a size whose bytes include the command byte", async () => {
    // 255 columns puts an 0xFF in the payload; unescaped, the receiver reads
    // it as framing and the block ends in the wrong place.
    const { transport, socket } = await connected();
    await transport.setSize(255, 25);
    expect(socket.written).toStrictEqual(golden.sequences.naws_with_a_command_byte);
  });
});

describe("the connection state", () => {
  it("refuses everything before a connect", async () => {
    const transport = new TelnetTransport({ connect: async () => new FakeSocket() });
    await expect(transport.send(Uint8Array.from([1]))).rejects.toThrow(golden.failures.send_before_connect);
    await expect(transport.receive(1, 1)).rejects.toThrow(golden.failures.receive_before_connect);
    await expect(transport.setSize(80, 25)).rejects.toThrow(golden.failures.set_size_before_connect);
  });

  it("raises the shared error type", async () => {
    const transport = new TelnetTransport({ connect: async () => new FakeSocket() });
    await expect(transport.send(Uint8Array.from([1]))).rejects.toThrow(TransportConnectionError);
  });

  it("is safe to disconnect twice, and before connecting", async () => {
    const { transport, socket } = await connected();
    await transport.disconnect();
    await transport.disconnect();
    expect(socket.closing).toBe(true);
    expect(transport.isConnected()).toBe(false);
  });

  it("survives a socket that fails to close", async () => {
    const { transport, socket } = await connected();
    socket.close = async () => {
      throw new Error("already gone");
    };
    await expect(transport.disconnect()).resolves.toBeUndefined();
    expect(transport.isConnected()).toBe(false);
  });

  it("reports the address it actually reached", async () => {
    // The real peer, not the hostname asked for, so an egress check runs
    // against what the connection went to.
    const { transport } = await connected();
    expect(transport.peerIp()).toBe(golden.peer_ip);
  });

  it("says nothing about a peer it has no socket for", async () => {
    // Absent means "proceed": a caller must not fail closed on a socket that
    // simply cannot say.
    const transport = new TelnetTransport({ connect: async () => new FakeSocket() });
    expect(transport.peerIp()).toBeUndefined();
  });

  it("is not connected once the socket is closing", async () => {
    const { transport, socket } = await connected();
    socket.closing = true;
    expect(transport.isConnected()).toBe(false);
  });

  it("does not write a negotiation to a closing socket", async () => {
    const { transport, socket } = await connected([[TELNET.IAC, TELNET.DO, TELNET.OPT_NAWS]]);
    socket.closing = true;
    await transport.receive(4096, 100);
    expect(socket.written).toStrictEqual([]);
  });
});
