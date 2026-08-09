//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { EgressBlockedError } from "../egress/index.ts";
import { loadGolden, must } from "../testing/golden.ts";
import {
  parseWsUrl,
  WebSocketSessionConnector,
  WS_COLS,
  WS_CONNECTOR_KEYS,
  WS_ROWS,
  type WsConnectorSocket,
  type WsFrame,
  WsReadTimeout,
} from "./index.ts";

interface RecordedFrame {
  binary?: boolean;
  data?: string;
  closed?: boolean;
}

interface ConfigCase {
  name: string;
  config: Record<string, unknown>;
  error: string | null;
  snapshot?: Record<string, unknown>;
  analysis?: string;
}

interface DriveCase {
  name: string;
  config: Record<string, unknown>;
  frames: RecordedFrame[];
  messages: Record<string, unknown>[];
  snapshot: Record<string, unknown>;
  analysis: string;
  sent: string[];
  closes: number;
  connected: boolean;
}

interface PeerCase {
  error: string | null;
  closes: number;
}

interface WsGolden {
  fixed_ts: number;
  url: string;
  parsed_urls: Record<string, { scheme: string; host: string }>;
  cols: number;
  rows: number;
  config_cases: ConfigCase[];
  drive_cases: DriveCase[];
  mode_refusal: { error: string | null };
  blocked_peer: PeerCase;
  unknown_peer: PeerCase;
  private_peer_allowed: PeerCase;
  private_peer_blocked: PeerCase;
  stop: { closes: number; connected: boolean };
}

const golden = loadGolden<WsGolden>("wsconnector_golden.json");

/** The corpus writes binary frames as latin-1 text, which is byte-for-character. */
function bytes(text: string): Uint8Array {
  return Uint8Array.from([...text].map((character) => character.charCodeAt(0)));
}

/** One recorded frame, as the socket hands it over. `undefined` means gone. */
function frameOf(record: RecordedFrame): WsFrame | undefined {
  if (record.closed === true) {
    return undefined;
  }
  return record.binary === true
    ? { binary: true, data: bytes(record.data as string) }
    : { binary: false, data: record.data as string };
}

/** A socket that answers from a script rather than a network. */
class ScriptedSocket implements WsConnectorSocket {
  readonly sent: string[] = [];
  closes = 0;
  #frames: (WsFrame | undefined)[];
  #peerIp: string | undefined;

  constructor(frames: (WsFrame | undefined)[] = [], peerIp: string | undefined = "203.0.113.7") {
    this.#frames = [...frames];
    this.#peerIp = peerIp;
  }

  async receive(): Promise<WsFrame> {
    if (this.#frames.length === 0) {
      // Nothing to read is a timeout, which is not a disconnection.
      throw new WsReadTimeout("nothing to read");
    }
    const frame = this.#frames.shift();
    if (frame === undefined) {
      throw new Error("socket closed");
    }
    return frame;
  }

  async send(data: string): Promise<void> {
    this.sent.push(data);
  }

  async close(): Promise<void> {
    this.closes += 1;
  }

  peerIp(): string | undefined {
    return this.#peerIp;
  }
}

/** One connector, wired to a socket and a clock that does not move. */
function connectorFor(
  config: Record<string, unknown>,
  socket: ScriptedSocket = new ScriptedSocket(),
): WebSocketSessionConnector {
  return new WebSocketSessionConnector(
    "sess-1",
    "Demo Session",
    { url: golden.url, ...config },
    { connect: async () => socket, now: () => golden.fixed_ts },
  );
}

/** Replay one recorded case and collect everything it produced. */
async function drive(record: DriveCase): Promise<{
  messages: Record<string, unknown>[];
  socket: ScriptedSocket;
  connector: WebSocketSessionConnector;
}> {
  const socket = new ScriptedSocket(record.frames.map(frameOf));
  const connector = connectorFor(record.config, socket);
  await connector.start();
  const messages: Record<string, unknown>[] = [];
  for (const step of stepsFor(record)) {
    messages.push(...(await step(connector)));
  }
  return { messages, socket, connector };
}

type Step = (connector: WebSocketSessionConnector) => Promise<Record<string, unknown>[]>;

/**
 * The script each recorded case was driven with.
 *
 * Kept alongside the generator's own list rather than recorded, because what
 * is being checked is the connector's answers and not the questions.
 */
function stepsFor(record: DriveCase): Step[] {
  const poll: Step = (connector) => connector.pollMessages() as Promise<Record<string, unknown>[]>;
  const input =
    (data: string): Step =>
    (connector) =>
      connector.handleInput(data) as Promise<Record<string, unknown>[]>;
  const control =
    (action: string): Step =>
    (connector) =>
      connector.handleControl(action) as Promise<Record<string, unknown>[]>;
  const mode =
    (value: string): Step =>
    (connector) =>
      connector.setMode(value) as Promise<Record<string, unknown>[]>;
  const scripts: Record<string, Step[]> = {
    "a session that says nothing": [],
    "a text frame": [poll],
    "a text frame with characters outside ASCII": [poll],
    "a binary frame": [poll],
    "both kinds of frame, counted": [poll, poll],
    "nothing to read": [poll],
    "a socket that has closed": [poll],
    "a socket that closes after a frame": [poll, poll],
    "a poll after the socket closed": [poll, poll],
    "input sent upstream": [input("list\r\n")],
    "input outside ASCII sent upstream": [input("naïve\r\n")],
    "control taken and held": [control("pause")],
    "control taken and released": [control("pause"), control("resume")],
    "a step requested": [control("step")],
    "a control action nobody defined": [control("rewind")],
    "the mode changed to hijack": [mode("hijack")],
    "the mode changed back": [mode("open")],
    "the screen cleared": [poll, (connector) => connector.clear() as Promise<Record<string, unknown>[]>],
    "the overlay turned off": [poll],
    "more output than the buffer holds": [poll],
    "a line wider than the screen": [poll],
    "more lines than the screen has, with no overlay": [poll],
    "more output than the screen holds": [poll],
  };
  return scripts[record.name] as Step[];
}

describe("the settings a WebSocket session takes", () => {
  it.each(golden.config_cases)("$name", async (record) => {
    const build = () =>
      new WebSocketSessionConnector("sess-1", "Demo Session", record.config, {
        connect: async () => new ScriptedSocket(),
        now: () => golden.fixed_ts,
      });
    if (record.error !== null) {
      expect(build).toThrow(record.error);
      return;
    }
    const connector = build();
    expect(await connector.getSnapshot()).toEqual(record.snapshot);
    expect(await connector.getAnalysis()).toBe(record.analysis);
  });

  it("requires an endpoint at all", () => {
    // A session with no endpoint would exist and never work; refusing here
    // means the server does not start instead.
    for (const config of [{}, { url: null }]) {
      expect(() => connectorFor({ ...config, url: config.url })).toThrow(
        "websocket connector requires connector_config.url",
      );
    }
  });

  it("refuses an endpoint that is not a WebSocket", () => {
    for (const url of ["http://feed.example/session", "feed.example/session", ""]) {
      expect(() => connectorFor({ url })).toThrow("websocket connector_config.url scheme must be ws or wss");
    }
  });

  it("refuses an endpoint with no host", () => {
    expect(() => connectorFor({ url: "wss:///session" })).toThrow("websocket connector_config.url must include a host");
  });

  it("reads an endpoint the way the reference reads it", () => {
    // Including the parts that only matter for the refusal: a bracketed IPv6
    // host has a host, and a string with no scheme has neither.
    for (const [url, parsed] of Object.entries(golden.parsed_urls)) {
      expect({ url, ...parseWsUrl(url) }).toEqual({ url, ...parsed });
    }
  });

  it("takes an endpoint with a port, a path and a query", () => {
    expect(() => connectorFor({ url: "wss://feed.example:8443/s?token=x" })).not.toThrow();
  });

  it("refuses a name it does not have, sorted", () => {
    expect(() => connectorFor({ endpoint: "x", mode: "open" })).toThrow(
      "unknown websocket connector_config keys: ['endpoint', 'mode']",
    );
  });

  it("takes the settings the reference takes", () => {
    expect([...WS_CONNECTOR_KEYS].sort()).toEqual([
      "block_private_connector_targets",
      "hub_overlay",
      "input_mode",
      "url",
    ]);
  });

  it("draws for the screen the reference draws for", () => {
    expect([WS_COLS, WS_ROWS]).toEqual([golden.cols, golden.rows]);
  });
});

describe("a WebSocket session in use", () => {
  it.each(golden.drive_cases)("$name", async (record) => {
    const { messages, socket, connector } = await drive(record);
    expect(messages).toEqual(record.messages);
    expect(socket.sent).toEqual(record.sent);
    expect(socket.closes).toBe(record.closes);
    expect(connector.isConnected()).toBe(record.connected);
  });

  it.each(golden.drive_cases)("$name — and what it looks like afterwards", async (record) => {
    const { connector } = await drive(record);
    expect(await connector.getSnapshot()).toEqual(record.snapshot);
    expect(await connector.getAnalysis()).toBe(record.analysis);
  });

  it("counts a text frame in bytes, not characters", async () => {
    // So a viewer's byte count means the same thing for either kind of frame.
    const record = golden.drive_cases.find(
      (entry) => entry.name === "a text frame with characters outside ASCII",
    ) as DriveCase;
    const { connector } = await drive(record);
    // Eleven bytes for eight characters: `é` is two and `☃` is three.
    expect(await connector.getAnalysis()).toContain("bytes_received: 11");
    expect((must(record.frames[0], "the first frame").data as string).length).toBe(8);
  });

  it("reads a binary frame as CP437", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "a binary frame") as DriveCase;
    const { messages } = await drive(record);
    expect((messages[0] as { data: string }).data).toBe("╔═╗\n");
  });

  it("says nothing at all when there is nothing to read", async () => {
    // A quiet session is not a closed one.
    const record = golden.drive_cases.find((entry) => entry.name === "nothing to read") as DriveCase;
    const { messages, connector } = await drive(record);
    expect(messages).toEqual([]);
    expect(connector.isConnected()).toBe(true);
  });

  it("says a closed socket is closed, once", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "a poll after the socket closed") as DriveCase;
    const { messages, socket, connector } = await drive(record);
    expect(messages).toHaveLength(1);
    expect((messages[0] as { screen: string }).screen).toContain("WebSocket connection closed.");
    expect(connector.isConnected()).toBe(false);
    expect(socket.closes).toBe(1);
  });

  it("sends what was typed as a text frame", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "input outside ASCII sent upstream") as DriveCase;
    const { socket } = await drive(record);
    expect(socket.sent).toEqual(["naïve\r\n"]);
  });

  it("keeps the newest output when the buffer fills", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "more output than the buffer holds") as DriveCase;
    const screen = ((await drive(record)).messages[1] as { screen: string }).screen;
    expect(screen).toContain("LAST");
    expect(screen).not.toContain("FIRST");
  });

  it("clamps the cursor to the screen it draws for", async () => {
    for (const [name, cursor] of [
      ["a line wider than the screen", { x: WS_COLS - 1, y: 0 }],
      ["more lines than the screen has, with no overlay", { x: 6, y: WS_ROWS - 1 }],
    ] as const) {
      const record = golden.drive_cases.find((entry) => entry.name === name) as DriveCase;
      expect((await drive(record)).messages[1]).toMatchObject({ cursor });
    }
  });

  it("scrolls the overlay off with everything else when the screen fills", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "more output than the screen holds") as DriveCase;
    const screen = ((await drive(record)).messages[1] as { screen: string }).screen;
    expect(screen.split("\n")).toHaveLength(WS_ROWS);
    expect(screen).not.toContain("Demo Session (sess-1)");
  });
});

describe("a WebSocket session that goes wrong", () => {
  it("closes the socket on an address the policy refuses", async () => {
    // The handshake completed against the endpoint's name, so certificate
    // validation is untouched — this is what sees where the socket actually
    // went.
    const socket = new ScriptedSocket([], "169.254.169.254");
    const connector = connectorFor({}, socket);
    await expect(connector.start()).rejects.toBeInstanceOf(EgressBlockedError);
    expect(golden.blocked_peer.error).toBe("EgressBlockedError");
    expect(socket.closes).toBe(golden.blocked_peer.closes);
    expect(connector.isConnected()).toBe(false);
  });

  it("proceeds when the address could not be determined", async () => {
    const socket = new ScriptedSocket([], undefined);
    const connector = connectorFor({}, socket);
    await connector.start();
    expect(connector.isConnected()).toBe(true);
    expect(socket.closes).toBe(golden.unknown_peer.closes);
  });

  it("proceeds when the address is empty", async () => {
    const connector = connectorFor({}, new ScriptedSocket([], ""));
    await connector.start();
    expect(connector.isConnected()).toBe(true);
  });

  it("allows a private address by default and refuses it on request", async () => {
    // Reaching an internal server is the tool's purpose, so allowing it is the
    // default; a hosted deployment turns it off.
    const allowed = connectorFor({}, new ScriptedSocket([], "10.0.0.5"));
    await allowed.start();
    expect(allowed.isConnected()).toBe(true);
    expect(golden.private_peer_allowed.error).toBeNull();

    const blockedSocket = new ScriptedSocket([], "10.0.0.5");
    const blocked = connectorFor({ block_private_connector_targets: true }, blockedSocket);
    await expect(blocked.start()).rejects.toBeInstanceOf(EgressBlockedError);
    expect(blockedSocket.closes).toBe(golden.private_peer_blocked.closes);
  });

  it("still refuses even when closing the socket fails", async () => {
    class StubbornSocket extends ScriptedSocket {
      override async close(): Promise<void> {
        throw new Error("already gone");
      }
    }
    const connector = connectorFor({}, new StubbornSocket([], "169.254.169.254"));
    await expect(connector.start()).rejects.toBeInstanceOf(EgressBlockedError);
  });

  it("says nothing once it is not connected", async () => {
    const connector = connectorFor({});
    expect(await connector.pollMessages()).toEqual([]);
  });

  it("does not send what it cannot send", async () => {
    const socket = new ScriptedSocket();
    const connector = connectorFor({}, socket);
    await connector.handleInput("hello");
    expect(socket.sent).toEqual([]);
  });

  it("stops, and stays stopped when closing fails", async () => {
    const socket = new ScriptedSocket();
    const connector = connectorFor({}, socket);
    await connector.start();
    await connector.stop();
    expect(socket.closes).toBe(golden.stop.closes);
    expect(connector.isConnected()).toBe(golden.stop.connected);

    class StubbornSocket extends ScriptedSocket {
      override async close(): Promise<void> {
        throw new Error("already gone");
      }
    }
    const stubborn = connectorFor({}, new StubbornSocket());
    await stubborn.start();
    await stubborn.stop();
    expect(stubborn.isConnected()).toBe(false);
  });

  it("stops cleanly having never started", async () => {
    const connector = connectorFor({});
    await connector.stop();
    expect(connector.isConnected()).toBe(false);
  });

  it("ends quietly when closing fails after the socket goes", async () => {
    class StubbornSocket extends ScriptedSocket {
      override async close(): Promise<void> {
        throw new Error("already gone");
      }
    }
    const connector = connectorFor({}, new StubbornSocket([undefined]));
    await connector.start();
    expect(await connector.pollMessages()).toHaveLength(1);
    expect(connector.isConnected()).toBe(false);
  });

  it("refuses a mode the session does not have", async () => {
    const connector = connectorFor({});
    await expect(connector.setMode("readonly")).rejects.toThrow(golden.mode_refusal.error as string);
  });

  it("releases the hold when input goes back to shared", async () => {
    const connector = connectorFor({ input_mode: "hijack" });
    await connector.start();
    await connector.handleControl("pause");
    await connector.setMode("open");
    expect((await connector.getSnapshot()).screen).toContain("Live");
  });

  it("uses the wall clock when it is given none", async () => {
    const before = Date.now() / 1000;
    const connector = new WebSocketSessionConnector(
      "s",
      "d",
      { url: golden.url },
      { connect: async () => new ScriptedSocket() },
    );
    expect((await connector.getSnapshot()).ts as number).toBeGreaterThanOrEqual(before);
  });
});
