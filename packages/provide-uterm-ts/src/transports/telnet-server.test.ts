//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { connect, type Socket } from "node:net";
import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  BIND_ALL,
  buildTelnetHandshake,
  DEFAULT_NEGOTIATION_DELAY_S,
  DEFAULT_TELNET_SERVER_PORT,
  portOf,
  type RunningTelnetServer,
  resolveBind,
  startTelnetServer,
} from "./index.ts";

interface ServerGolden {
  handshake: string;
  handshake_bytes: number[];
  codes: Record<string, number>;
  defaults: { bind_all: string; telnet_port: number; negotiation_delay_s: number };
}

const golden = loadGolden<ServerGolden>("telnetserver_golden.json");

/** A connected client, with everything the server sent kept. */
interface Client {
  socket: Socket;
  /** Everything received so far. */
  received(): Buffer;
}

/**
 * Connect to a running server.
 *
 * Reading starts immediately and into a buffer: a socket nobody reads stays
 * paused, and a paused socket is never told the far end has gone — but a
 * listener attached later would also miss whatever arrived first.
 */
function client(port: number): Promise<Client> {
  return new Promise((resolve, reject) => {
    let seen = Buffer.alloc(0);
    const socket = connect({ host: "127.0.0.1", port }, () => {
      // Once connected, the far end going away is the thing under test rather
      // than a failure of the test.
      socket.removeListener("error", reject);
      socket.on("error", () => {});
      resolve({ socket, received: () => seen });
    });
    socket.on("data", (chunk: Buffer) => {
      seen = Buffer.concat([seen, chunk]);
    });
    socket.once("error", reject);
  });
}

/**
 * Whether the far end has gone.
 *
 * Polled rather than awaited on an event: the far end can close before a
 * listener is attached, and a missed event is a test that hangs.
 */
function isEnded(socket: Socket): boolean {
  return socket.destroyed || socket.readableEnded || socket.readyState === "writeOnly";
}

/** Wait until `predicate` holds, or give up. */
async function until(predicate: () => boolean, attempts = 200): Promise<boolean> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (predicate()) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return predicate();
}

describe("what a client is told on arrival", () => {
  it("sends exactly the preamble the reference sends", () => {
    // The bytes are the whole contract: get them wrong and every keystroke
    // appears twice, or the session waits for a go-ahead nobody sends.
    expect([...buildTelnetHandshake()]).toEqual(golden.handshake_bytes);
  });

  it("says this end echoes and suppresses go-ahead", () => {
    const { IAC, WILL, DO, DONT, ECHO, SGA, LINEMODE, NAWS } = golden.codes as Record<string, number>;
    expect([...buildTelnetHandshake()]).toEqual([
      IAC,
      WILL,
      ECHO,
      IAC,
      WILL,
      SGA,
      IAC,
      DO,
      SGA,
      IAC,
      DONT,
      LINEMODE,
      IAC,
      DO,
      NAWS,
    ]);
  });

  it("turns the client's own line editing off", () => {
    // The session does the editing; leaving it on gives two editors fighting
    // over one line.
    const bytes = [...buildTelnetHandshake()];
    const at = bytes.indexOf(golden.codes.LINEMODE as number);
    expect(bytes[at - 1]).toBe(golden.codes.DONT);
  });

  it("asks to be told the window size", () => {
    const bytes = [...buildTelnetHandshake()];
    const at = bytes.indexOf(golden.codes.NAWS as number);
    expect(bytes[at - 1]).toBe(golden.codes.DO);
  });

  it("builds the same bytes every time", () => {
    expect([...buildTelnetHandshake()]).toEqual([...buildTelnetHandshake()]);
  });

  it("reads the port off a listening server, and refuses to guess", () => {
    // A caller that asked for port zero needs the one it actually got; a
    // server on a Unix socket has no port to give, and saying so beats
    // inventing one.
    expect(portOf({ address: "127.0.0.1", family: "IPv4", port: 4242 })).toBe(4242);
    expect(() => portOf(null)).toThrow("not listening on a network address");
    expect(() => portOf("/tmp/somewhere.sock")).toThrow("not listening on a network address");
  });

  it("waits for real when nobody says how", { timeout: 10_000 }, async () => {
    // The default pause is a real timer; a server that never paused would
    // hand a slow client over before it had read the options.
    const running = await startTelnetServer({
      host: "127.0.0.1",
      port: 0,
      negotiationDelayS: 0.05,
      handler: async () => {},
    });
    try {
      const started = Date.now();
      const { socket } = await client(running.port);
      expect(await until(() => isEnded(socket))).toBe(true);
      socket.destroy();
      expect(Date.now() - started).toBeGreaterThanOrEqual(40);
    } finally {
      await running.close();
    }
  });

  it("uses the defaults the reference uses", () => {
    expect(BIND_ALL).toBe(golden.defaults.bind_all);
    expect(DEFAULT_TELNET_SERVER_PORT).toBe(golden.defaults.telnet_port);
    expect(DEFAULT_NEGOTIATION_DELAY_S).toBe(golden.defaults.negotiation_delay_s);
  });

  it("falls back to those defaults, and takes what it is given", () => {
    // Checked without binding: the default is every interface on a well-known
    // port, which is not something a test should open to find out.
    expect(resolveBind({})).toEqual({ host: BIND_ALL, port: DEFAULT_TELNET_SERVER_PORT });
    expect(resolveBind({ host: "127.0.0.1" })).toEqual({ host: "127.0.0.1", port: DEFAULT_TELNET_SERVER_PORT });
    expect(resolveBind({ port: 0 })).toEqual({ host: BIND_ALL, port: 0 });
    expect(resolveBind({ host: "::1", port: 9999 })).toEqual({ host: "::1", port: 9999 });
  });
});

describe("a server somebody connects to", () => {
  /** Runs `body` against a started server, and always stops it. */
  async function withServer(
    options: Partial<Parameters<typeof startTelnetServer>[0]>,
    body: (running: RunningTelnetServer, seen: Socket[]) => Promise<void>,
  ): Promise<void> {
    const seen: Socket[] = [];
    const running = await startTelnetServer({
      host: "127.0.0.1",
      port: 0,
      sleep: async () => {},
      handler: async (socket) => {
        seen.push(socket);
        await new Promise<void>((resolve) => socket.on("close", () => resolve()));
      },
      ...options,
    });
    try {
      await body(running, seen);
    } finally {
      await running.close();
    }
  }

  it("greets a real client over a real socket", async () => {
    await withServer({}, async (running) => {
      const { socket, received } = await client(running.port);
      expect(await until(() => received().length >= golden.handshake_bytes.length)).toBe(true);
      socket.destroy();
      expect([...received()]).toEqual(golden.handshake_bytes);
    });
  });

  it("hands the connection over once it has said hello", async () => {
    await withServer({}, async (running, seen) => {
      const { socket } = await client(running.port);
      expect(await until(() => seen.length === 1)).toBe(true);
      socket.destroy();
    });
  });

  it("greets before it hands over", async () => {
    // A handler that wrote first would have its output arrive before the
    // options the client needs to read it correctly.
    const order: string[] = [];
    const running = await startTelnetServer({
      host: "127.0.0.1",
      port: 0,
      sleep: async () => {
        order.push("paused");
      },
      handler: async () => {
        order.push("handled");
      },
    });
    try {
      const { socket } = await client(running.port);
      expect(await until(() => order.includes("handled"))).toBe(true);
      socket.destroy();
      expect(order).toEqual(["paused", "handled"]);
    } finally {
      await running.close();
    }
  });

  it("pauses for the length it was told", async () => {
    const waits: number[] = [];
    const running = await startTelnetServer({
      host: "127.0.0.1",
      port: 0,
      negotiationDelayS: 2.5,
      sleep: async (seconds) => {
        waits.push(seconds);
      },
      handler: async () => {},
    });
    try {
      const { socket } = await client(running.port);
      expect(await until(() => waits.length === 1)).toBe(true);
      socket.destroy();
      expect(waits).toEqual([2.5]);
    } finally {
      await running.close();
    }
  });

  it("carries what the client types through to the handler", async () => {
    const received: string[] = [];
    await withServer(
      {
        handler: async (socket) => {
          socket.on("data", (chunk: Buffer) => received.push(chunk.toString()));
          await new Promise<void>((resolve) => socket.on("close", () => resolve()));
        },
      },
      async (running) => {
        const { socket } = await client(running.port);
        socket.write("hello");
        expect(await until(() => received.join("").includes("hello"))).toBe(true);
        socket.destroy();
      },
    );
  });

  it("lets the handler write back", async () => {
    await withServer(
      {
        handler: async (socket) => {
          socket.write("from the handler");
          await new Promise<void>((resolve) => socket.on("close", () => resolve()));
        },
      },
      async (running) => {
        const { socket, received } = await client(running.port);
        expect(await until(() => received().toString().includes("from the handler"))).toBe(true);
        socket.destroy();
      },
    );
  });

  it("closes the connection when the handler is done", async () => {
    await withServer({ handler: async () => {} }, async (running) => {
      const { socket } = await client(running.port);
      expect(await until(() => isEnded(socket))).toBe(true);
      socket.destroy();
    });
  });

  it("closes the connection when the handler fails", async () => {
    // A handler that threw would otherwise leave the socket open with nobody
    // reading it.
    await withServer(
      {
        handler: async () => {
          throw new Error("the handler broke");
        },
      },
      async (running) => {
        const { socket } = await client(running.port);
        expect(await until(() => isEnded(socket))).toBe(true);
        socket.destroy();
      },
    );
  });

  it("takes more than one client at once, and keeps them apart", async () => {
    const heard = new Map<Socket, string>();
    await withServer(
      {
        handler: async (socket) => {
          socket.on("data", (chunk: Buffer) => heard.set(socket, (heard.get(socket) ?? "") + chunk.toString()));
          await new Promise<void>((resolve) => socket.on("close", () => resolve()));
        },
      },
      async (running) => {
        const { socket: first } = await client(running.port);
        const { socket: second } = await client(running.port);
        expect(await until(() => running.connections === 2)).toBe(true);
        first.write("one");
        second.write("two");
        expect(await until(() => [...heard.values()].join("").length >= 6)).toBe(true);
        first.destroy();
        second.destroy();
        expect([...heard.values()].sort()).toEqual(["one", "two"]);
      },
    );
  });

  it("stops counting a client that has gone", async () => {
    await withServer({}, async (running) => {
      const { socket } = await client(running.port);
      expect(await until(() => running.connections === 1)).toBe(true);
      socket.destroy();
      expect(await until(() => running.connections === 0)).toBe(true);
    });
  });

  it("stops listening when it is closed", async () => {
    const running = await startTelnetServer({
      host: "127.0.0.1",
      port: 0,
      sleep: async () => {},
      handler: async () => {},
    });
    const port = running.port;
    await running.close();
    await expect(client(port)).rejects.toThrow();
  });

  it("drops every client when it is closed", async () => {
    const running = await startTelnetServer({
      host: "127.0.0.1",
      port: 0,
      sleep: async () => {},
      handler: async (socket) => {
        await new Promise<void>((resolve) => socket.on("close", () => resolve()));
      },
    });
    const { socket } = await client(running.port);
    await until(() => running.connections === 1);
    await running.close();
    expect(await until(() => isEnded(socket))).toBe(true);
  });

  it("says so when the port is already taken", async () => {
    // Rather than waiting forever on a listener that never came up.
    const running = await startTelnetServer({
      host: "127.0.0.1",
      port: 0,
      sleep: async () => {},
      handler: async () => {},
    });
    try {
      await expect(
        startTelnetServer({ host: "127.0.0.1", port: running.port, sleep: async () => {}, handler: async () => {} }),
      ).rejects.toThrow();
    } finally {
      await running.close();
    }
  });

  it("reports the port it was actually given", async () => {
    await withServer({}, async (running) => {
      expect(running.port).toBeGreaterThan(1023);
    });
  });

  it("survives a client that fails mid-connection", async () => {
    await withServer({}, async (running, seen) => {
      const { socket: first } = await client(running.port);
      await until(() => seen.length === 1);
      first.resetAndDestroy();
      const { socket: second } = await client(running.port);
      expect(await until(() => seen.length === 2)).toBe(true);
      second.destroy();
    });
  });
});
