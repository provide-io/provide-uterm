//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { connect, type Socket } from "node:net";
import { describe, expect, it } from "vitest";
import {
  assertBindAllowed,
  DEFAULT_NEGOTIATE_TIMEOUT_S,
  DEFAULT_TELNET_HOST,
  DEFAULT_TELNET_PORT,
  type RunningTelnetGateway,
  TelnetGateway,
  type UpstreamSession,
} from "./index.ts";

const IAC = 255;
const SB = 250;
const SE = 240;
const DO = 253;
const WILL = 251;
const TTYPE = 24;
const NEW_ENVIRON = 39;
const IS = 0;

/** A client answering with its terminal type. */
function ttypeIs(name: string): Buffer {
  return Buffer.from([IAC, SB, TTYPE, IS, ...[...name].map((c) => c.charCodeAt(0)), IAC, SE]);
}

/** An empty environment answer, which is enough to finish the handshake. */
const environIs = Buffer.from([IAC, SB, NEW_ENVIRON, IS, IAC, SE]);

/** What the gateway was asked to open, and what it was sent. */
interface Opened {
  colormode: string | undefined;
  term: string;
  env: Record<string, string>;
  sent: string[];
  closed: boolean;
}

/** A gateway whose upstream is a recorder. */
function gatewayWith(options: Partial<ConstructorParameters<typeof TelnetGateway>[0]> = {}) {
  const opened: Opened[] = [];
  const gateway = new TelnetGateway({
    wsUrl: "ws://upstream.test/session",
    connect: async (details) => {
      const record: Opened = { ...details, sent: [], closed: false };
      opened.push(record);
      const session: UpstreamSession = {
        send: async (data) => {
          record.sent.push(new TextDecoder().decode(data));
        },
        close: async () => {
          record.closed = true;
        },
      };
      return session;
    },
    ...options,
  });
  return { gateway, opened };
}

/** Connect to a running gateway and hand back the socket. */
function client(port: number): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const socket = connect({ host: "127.0.0.1", port }, () => resolve(socket));
    socket.once("error", reject);
  });
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

describe("where a telnet listener may bind", () => {
  it("refuses a routable address without an explicit opt-in", () => {
    // Telnet carries no authentication and no encryption, so binding it to a
    // routable address puts an unauthenticated shell on the network.
    for (const host of ["0.0.0.0", "192.168.1.10", "::", "example.test"]) {
      expect(() => assertBindAllowed(host, false)).toThrow("refusing to start an unauthenticated telnet gateway");
    }
  });

  it("allows loopback without anybody saying anything", () => {
    for (const host of ["127.0.0.1", "localhost", "::1"]) {
      expect(() => assertBindAllowed(host, false)).not.toThrow();
    }
  });

  it("allows a routable address once somebody has said so", () => {
    // The opt-in is the whole point: it is a statement that something else is
    // guarding this.
    expect(() => assertBindAllowed("0.0.0.0", true)).not.toThrow();
  });

  it("refuses before it opens anything", async () => {
    // A refused configuration must never hold a port, even briefly.
    const { gateway } = gatewayWith();
    await expect(gateway.start("0.0.0.0", 0)).rejects.toThrow("refusing to start");
  });

  it("defaults to loopback and the reference's port", () => {
    expect(DEFAULT_TELNET_HOST).toBe("127.0.0.1");
    expect(DEFAULT_TELNET_PORT).toBe(2112);
    expect(DEFAULT_NEGOTIATE_TIMEOUT_S).toBe(0.4);
  });
});

describe("a listener somebody connects to", () => {
  /** Runs `body` against a started gateway, and always stops it. */
  async function withGateway(
    options: Parameters<typeof gatewayWith>[0],
    body: (running: RunningTelnetGateway, opened: Opened[]) => Promise<void>,
  ): Promise<void> {
    const { gateway, opened } = gatewayWith(options);
    const running = await gateway.start("127.0.0.1", 0);
    try {
      await body(running, opened);
    } finally {
      await running.close();
    }
  }

  it("listens on a real port and takes a real connection", async () => {
    await withGateway({ iacNegotiate: false }, async (running) => {
      expect(running.port).toBeGreaterThan(0);
      const socket = await client(running.port);
      expect(await until(() => running.connections === 1)).toBe(true);
      socket.destroy();
    });
  });

  it("asks a client what it is the moment it connects", async () => {
    await withGateway({}, async (running) => {
      const socket = await client(running.port);
      let seen = Buffer.alloc(0);
      socket.on("data", (chunk) => {
        seen = Buffer.concat([seen, chunk]);
      });
      await until(() => seen.length >= 6);
      socket.destroy();
      expect([...seen]).toEqual([IAC, DO, TTYPE, IAC, DO, NEW_ENVIRON]);
    });
  });

  it("opens the session knowing what the client said", async () => {
    await withGateway({}, async (running, opened) => {
      const socket = await client(running.port);
      socket.write(Buffer.from([IAC, WILL, TTYPE]));
      socket.write(ttypeIs("xterm-256color"));
      socket.write(environIs);
      expect(await until(() => opened.length === 1)).toBe(true);
      socket.destroy();
      expect(opened[0]?.term).toBe("xterm-256color");
      expect(opened[0]?.colormode).toBe("256");
    });
  });

  it("opens the session anyway when a client says nothing", async () => {
    // A client that never answers is still a client; it gets the configured
    // colour mode rather than a connection that hangs.
    await withGateway({ iacNegotiateTimeoutS: 0.01, colorMode: "16" }, async (running, opened) => {
      const socket = await client(running.port);
      expect(await until(() => opened.length === 1)).toBe(true);
      socket.destroy();
      expect(opened[0]?.term).toBe("");
      expect(opened[0]?.colormode).toBe("16");
    });
  });

  it("forwards what the client types, with the protocol taken out", async () => {
    await withGateway({ iacNegotiate: false }, async (running, opened) => {
      const socket = await client(running.port);
      await until(() => opened.length === 1);
      socket.write(Buffer.from([104, 105, IAC, WILL, TTYPE, 33]));
      expect(await until(() => (opened[0]?.sent.join("") ?? "").includes("hi!"))).toBe(true);
      socket.destroy();
      expect(opened[0]?.sent.join("")).toBe("hi!");
    });
  });

  it("passes on what the client's environment said", async () => {
    // It is what the upstream opens the session with; losing it loses the
    // colour the client asked for.
    await withGateway({}, async (running, opened) => {
      const socket = await client(running.port);
      socket.write(ttypeIs("vt100"));
      socket.write(
        Buffer.from([
          IAC,
          SB,
          NEW_ENVIRON,
          IS,
          0,
          ...[..."COLORTERM"].map((c) => c.charCodeAt(0)),
          1,
          ...[..."truecolor"].map((c) => c.charCodeAt(0)),
          IAC,
          SE,
        ]),
      );
      expect(await until(() => opened.length === 1)).toBe(true);
      socket.destroy();
      expect(opened[0]?.env).toEqual({ COLORTERM: "truecolor" });
      expect(opened[0]?.colormode).toBe("passthrough");
    });
  });

  it("opens the session as soon as the client has answered", async () => {
    // Not when the window runs out: a client that answered should not wait
    // for a timeout it already beat.
    await withGateway({ iacNegotiateTimeoutS: 30 }, async (running, opened) => {
      const started = Date.now();
      const socket = await client(running.port);
      socket.write(ttypeIs("vt100"));
      socket.write(environIs);
      expect(await until(() => opened.length === 1)).toBe(true);
      socket.destroy();
      expect(Date.now() - started).toBeLessThan(5_000);
    });
  });

  it("forwards each byte once after a negotiated handshake", async () => {
    // Two readers on one socket would feed the negotiator twice and send
    // everything the client typed twice.
    await withGateway({}, async (running, opened) => {
      const socket = await client(running.port);
      socket.write(ttypeIs("vt100"));
      socket.write(environIs);
      await until(() => opened.length === 1);
      socket.write(Buffer.from("hello"));
      expect(await until(() => (opened[0]?.sent.join("") ?? "").includes("hello"))).toBe(true);
      await new Promise((resolve) => setTimeout(resolve, 50));
      socket.destroy();
      expect(opened[0]?.sent.join("")).toBe("hello");
    });
  });

  it("keeps answering the client after the session is open", async () => {
    // A client can offer an option at any time, and silence is how a
    // handshake stalls.
    await withGateway({}, async (running, opened) => {
      const socket = await client(running.port);
      const chunks: Buffer[] = [];
      socket.on("data", (chunk) => chunks.push(chunk));
      socket.write(ttypeIs("vt100"));
      socket.write(environIs);
      await until(() => opened.length === 1);
      const before = Buffer.concat(chunks).length;
      socket.write(Buffer.from([IAC, WILL, NEW_ENVIRON]));
      expect(await until(() => Buffer.concat(chunks).length > before)).toBe(true);
      socket.destroy();
      expect(Buffer.concat(chunks).subarray(before)).toEqual(Buffer.from([IAC, SB, NEW_ENVIRON, 1, IAC, SE]));
    });
  });

  it("says so when the port is already taken", async () => {
    // Rather than waiting forever on a listener that never came up.
    const { gateway: first } = gatewayWith({ iacNegotiate: false });
    const running = await first.start("127.0.0.1", 0);
    try {
      const { gateway: second } = gatewayWith({ iacNegotiate: false });
      await expect(second.start("127.0.0.1", running.port)).rejects.toThrow();
    } finally {
      await running.close();
    }
  });

  it("keeps what a client typed before it was asked", async () => {
    // A client that types ahead has still typed; dropping it would lose a
    // command somebody entered.
    await withGateway({ iacNegotiateTimeoutS: 0.05 }, async (running, opened) => {
      const socket = await client(running.port);
      socket.write(Buffer.from("early"));
      socket.write(ttypeIs("vt100"));
      socket.write(environIs);
      expect(await until(() => (opened[0]?.sent.join("") ?? "").includes("early"))).toBe(true);
      await new Promise((resolve) => setTimeout(resolve, 50));
      socket.destroy();
      // Once, not twice: replaying what was held would double a command.
      expect(opened[0]?.sent.join("")).toBe("early");
    });
  });

  it("answers a client that agrees to say what it is", async () => {
    await withGateway({}, async (running) => {
      const socket = await client(running.port);
      const chunks: Buffer[] = [];
      socket.on("data", (chunk) => chunks.push(chunk));
      await until(() => chunks.length > 0);
      socket.write(Buffer.from([IAC, WILL, TTYPE]));
      expect(await until(() => Buffer.concat(chunks).includes(Buffer.from([IAC, SB, TTYPE, 1, IAC, SE])))).toBe(true);
      socket.destroy();
    });
  });

  it("closes the session when the client goes away", async () => {
    await withGateway({ iacNegotiate: false }, async (running, opened) => {
      const socket = await client(running.port);
      await until(() => opened.length === 1);
      socket.destroy();
      expect(await until(() => opened[0]?.closed === true)).toBe(true);
    });
  });

  it("takes more than one client at once", async () => {
    await withGateway({ iacNegotiate: false }, async (running, opened) => {
      const sockets = await Promise.all([client(running.port), client(running.port), client(running.port)]);
      expect(await until(() => opened.length === 3)).toBe(true);
      expect(running.connections).toBe(3);
      for (const socket of sockets) {
        socket.destroy();
      }
      expect(await until(() => running.connections === 0)).toBe(true);
    });
  });

  it("keeps clients apart", async () => {
    await withGateway({ iacNegotiate: false }, async (running, opened) => {
      const first = await client(running.port);
      const second = await client(running.port);
      await until(() => opened.length === 2);
      first.write(Buffer.from("one"));
      second.write(Buffer.from("two"));
      expect(await until(() => opened.every((session) => session.sent.length > 0))).toBe(true);
      first.destroy();
      second.destroy();
      const sent = opened.map((session) => session.sent.join("")).sort();
      expect(sent).toEqual(["one", "two"]);
    });
  });

  it("stops listening when it is closed", async () => {
    const { gateway } = gatewayWith({ iacNegotiate: false });
    const running = await gateway.start("127.0.0.1", 0);
    const port = running.port;
    await running.close();
    await expect(client(port)).rejects.toThrow();
  });

  it("drops every client when it is closed", async () => {
    const { gateway, opened } = gatewayWith({ iacNegotiate: false });
    const running = await gateway.start("127.0.0.1", 0);
    const socket = await client(running.port);
    await until(() => opened.length === 1);
    await running.close();
    expect(await until(() => socket.destroyed)).toBe(true);
  });

  it("survives a client that fails mid-connection", async () => {
    // One client's broken socket is that client's problem.
    await withGateway({ iacNegotiate: false }, async (running, opened) => {
      const first = await client(running.port);
      await until(() => opened.length === 1);
      first.resetAndDestroy();
      const second = await client(running.port);
      expect(await until(() => opened.length === 2)).toBe(true);
      second.destroy();
    });
  });

  it("survives an upstream that refuses to open", async () => {
    // A session server that is down should not take the listener with it.
    const gateway = new TelnetGateway({
      wsUrl: "ws://upstream.test/session",
      iacNegotiate: false,
      connect: async () => {
        throw new Error("upstream is down");
      },
    });
    const running = await gateway.start("127.0.0.1", 0);
    try {
      const socket = await client(running.port);
      expect(await until(() => socket.destroyed)).toBe(true);
      // Still listening.
      const second = await client(running.port);
      second.destroy();
    } finally {
      await running.close();
    }
  });

  it("forwards nothing when a client sends only protocol", async () => {
    // Negotiation does not end the stripping: an option arriving later is
    // still protocol, and passing it upstream would type it into the session.
    await withGateway({ iacNegotiate: false }, async (running, opened) => {
      const socket = await client(running.port);
      await until(() => opened.length === 1);
      socket.write(Buffer.from([IAC, WILL, TTYPE]));
      await new Promise((resolve) => setTimeout(resolve, 50));
      socket.destroy();
      expect(opened[0]?.sent.join("")).toBe("");
    });
  });

  it("can be asked for a port and given that port", async () => {
    // Zero means "whichever is free", and the number it got is what a caller
    // has to be told.
    await withGateway({ iacNegotiate: false }, async (running) => {
      expect(running.port).not.toBe(0);
      expect(running.port).toBeGreaterThan(1023);
    });
  });
});
