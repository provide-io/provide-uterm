//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { EgressBlockedError } from "../egress/index.ts";
import { loadGolden, must } from "../testing/golden.ts";
import {
  TELNET_COLS,
  TELNET_CONNECTOR_KEYS,
  TELNET_ROWS,
  type TelnetConnectorTransport,
  TelnetSessionConnector,
} from "./index.ts";

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
  chunks: string[];
  messages: Record<string, unknown>[];
  snapshot: Record<string, unknown>;
  analysis: string;
  sent: string[];
  connected: boolean;
}

interface TelnetGolden {
  fixed_ts: number;
  cols: number;
  rows: number;
  config_cases: ConfigCase[];
  drive_cases: DriveCase[];
  mode_refusal: { error: string | null };
  blocked_peer: { error: string | null; connected: boolean; disconnects: number };
  unknown_peer: { connected: boolean };
}

const golden = loadGolden<TelnetGolden>("telnetconnector_golden.json");

/** The corpus writes bytes as latin-1 text, which is byte-for-character. */
function bytes(text: string): Uint8Array {
  return Uint8Array.from([...text].map((character) => character.charCodeAt(0)));
}

function latin1(data: Uint8Array): string {
  return [...data].map((byte) => String.fromCharCode(byte)).join("");
}

/** A transport that answers from a script rather than a socket. */
class ScriptedTransport implements TelnetConnectorTransport {
  readonly sent: Uint8Array[] = [];
  connected = false;
  disconnects = 0;
  #chunks: Uint8Array[];
  #peerIp: string | undefined;
  #failReceive: boolean;

  constructor(chunks: Uint8Array[] = [], peerIp: string | undefined = "203.0.113.7", failReceive = false) {
    this.#chunks = [...chunks];
    this.#peerIp = peerIp;
    this.#failReceive = failReceive;
  }

  async connect(): Promise<void> {
    this.connected = true;
  }

  async disconnect(): Promise<void> {
    this.connected = false;
    this.disconnects += 1;
  }

  isConnected(): boolean {
    return this.connected;
  }

  peerIp(): string | undefined {
    return this.#peerIp;
  }

  async receive(): Promise<Uint8Array> {
    if (this.#failReceive) {
      throw new Error("connection reset");
    }
    return this.#chunks.shift() ?? new Uint8Array(0);
  }

  async send(data: Uint8Array): Promise<void> {
    this.sent.push(data);
  }
}

/** One connector, wired to a script and a clock that does not move. */
function connectorFor(
  config: Record<string, unknown>,
  transport: TelnetConnectorTransport = new ScriptedTransport(),
): TelnetSessionConnector {
  return new TelnetSessionConnector("sess-1", "Demo Session", config, { transport, now: () => golden.fixed_ts });
}

/** Replay one recorded case and collect everything it produced. */
async function drive(record: DriveCase): Promise<{ messages: Record<string, unknown>[]; sent: string[] }> {
  const transport = new ScriptedTransport(record.chunks.map(bytes));
  const connector = connectorFor(record.config, transport);
  await connector.start();
  const messages: Record<string, unknown>[] = [];
  for (const step of stepsFor(record)) {
    messages.push(...(await step(connector)));
  }
  return { messages, sent: transport.sent.map(latin1) };
}

/**
 * The script each recorded case was driven with.
 *
 * Kept alongside the generator's own list rather than recorded, because what
 * is being checked is the connector's answers and not the questions.
 */
function stepsFor(record: DriveCase): ((connector: TelnetSessionConnector) => Promise<Record<string, unknown>[]>)[] {
  const poll = (connector: TelnetSessionConnector) => connector.pollMessages() as Promise<Record<string, unknown>[]>;
  const scripts: Record<string, ((connector: TelnetSessionConnector) => Promise<Record<string, unknown>[]>)[]> = {
    "a session that says nothing": [],
    "one chunk of output": [poll],
    "output with high-byte drawing characters": [poll],
    "two chunks, counted": [poll, poll],
    "nothing to read": [poll],
    "input sent upstream": [(connector) => connector.handleInput("list\r\n") as Promise<Record<string, unknown>[]>],
    "input the endpoint cannot spell": [
      (connector) => connector.handleInput("naïve\r\n") as Promise<Record<string, unknown>[]>,
    ],
    "control taken and held": [(connector) => connector.handleControl("pause") as Promise<Record<string, unknown>[]>],
    "control taken and released": [
      (connector) => connector.handleControl("pause") as Promise<Record<string, unknown>[]>,
      (connector) => connector.handleControl("resume") as Promise<Record<string, unknown>[]>,
    ],
    "a step requested": [(connector) => connector.handleControl("step") as Promise<Record<string, unknown>[]>],
    "a control action nobody defined": [
      (connector) => connector.handleControl("rewind") as Promise<Record<string, unknown>[]>,
    ],
    "the mode changed to hijack": [(connector) => connector.setMode("hijack") as Promise<Record<string, unknown>[]>],
    "the mode changed back": [(connector) => connector.setMode("open") as Promise<Record<string, unknown>[]>],
    "the screen cleared": [poll, (connector) => connector.clear() as Promise<Record<string, unknown>[]>],
    "the overlay turned off": [poll],
    "more output than the screen holds": [poll],
    "more output than the buffer holds": [poll],
    "a line wider than the screen": [poll],
    "more lines than the screen has, with no overlay": [poll],
  };
  return scripts[record.name] as ((connector: TelnetSessionConnector) => Promise<Record<string, unknown>[]>)[];
}

describe("the settings a telnet session takes", () => {
  it.each(golden.config_cases)("$name", async (record) => {
    if (record.error !== null) {
      expect(() => connectorFor(record.config)).toThrow(record.error);
      return;
    }
    const connector = connectorFor(record.config);
    expect(await connector.getSnapshot()).toEqual(record.snapshot);
    expect(await connector.getAnalysis()).toBe(record.analysis);
  });

  it("refuses a name it does not have, rather than ignoring it", () => {
    // The entry that carries these settings *folds* unrecognised keys in
    // rather than refusing them, so this is where a mistyped one is caught —
    // and the difference between a session that will not start and one whose
    // host setting silently did nothing.
    expect(() => connectorFor({ hostname: "bbs.example" })).toThrow(
      "unknown telnet connector_config keys: ['hostname']",
    );
  });

  it("names every one it does not have, sorted", () => {
    expect(() => connectorFor({ prot: 23, hostname: "h" })).toThrow(
      "unknown telnet connector_config keys: ['hostname', 'prot']",
    );
  });

  it("takes the ones the reference takes", () => {
    expect([...TELNET_CONNECTOR_KEYS].sort()).toEqual([
      "block_private_connector_targets",
      "host",
      "hub_overlay",
      "input_mode",
      "port",
    ]);
  });

  it("draws for the screen the reference draws for", () => {
    expect([TELNET_COLS, TELNET_ROWS]).toEqual([golden.cols, golden.rows]);
  });
});

describe("a telnet session in use", () => {
  it.each(golden.drive_cases)("$name", async (record) => {
    const { messages, sent } = await drive(record);
    expect(messages).toEqual(record.messages);
    expect(sent).toEqual(record.sent);
  });

  it.each(golden.drive_cases)("$name — and what it looks like afterwards", async (record) => {
    const transport = new ScriptedTransport(record.chunks.map(bytes));
    const connector = connectorFor(record.config, transport);
    await connector.start();
    for (const step of stepsFor(record)) {
      await step(connector);
    }
    expect(await connector.getSnapshot()).toEqual(record.snapshot);
    expect(await connector.getAnalysis()).toBe(record.analysis);
    expect(connector.isConnected()).toBe(record.connected);
  });

  it("reads high bytes as CP437 rather than replacing them", () => {
    // An endpoint old enough to need this connector draws its boxes with high
    // bytes; reading them as UTF-8 would turn every one into a replacement
    // character.
    const record = golden.drive_cases.find((entry) => entry.name === "output with high-byte drawing characters");
    expect((must(record, "the high-byte drive case").messages[0] as { data: string }).data).toBe("╔═╗\n");
  });

  it("scrolls the overlay off with everything else when the screen fills", async () => {
    // The last screenful wins, header included, so a busy session shows only
    // its output. A port that pinned the header would show a different screen
    // than every other port does.
    const record = golden.drive_cases.find((entry) => entry.name === "more output than the screen holds") as DriveCase;
    const { messages } = await drive(record);
    const screen = (messages[1] as { screen: string }).screen;
    expect(screen.split("\n")).toHaveLength(TELNET_ROWS);
    expect(screen).not.toContain("Demo Session (sess-1)");
    expect(screen.split("\n")[0]).toBe("line 15");
  });

  it("shows the overlay while there is room for it", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "one chunk of output") as DriveCase;
    const { messages } = await drive(record);
    expect((messages[1] as { screen: string }).screen).toContain("Demo Session (sess-1)");
  });

  it("shows nothing but the endpoint's output when the overlay is off", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "the overlay turned off") as DriveCase;
    const { messages } = await drive(record);
    expect((messages[1] as { screen: string }).screen).toBe("bare output\n");
  });

  it("sends what was typed upstream", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "input sent upstream") as DriveCase;
    expect((await drive(record)).sent).toEqual(["list\r\n"]);
  });

  it("replaces a character the endpoint cannot spell rather than refusing it", async () => {
    // CP437 has no `ï`. Dropping the line would lose what somebody typed; the
    // reference substitutes, and a session stays usable.
    const record = golden.drive_cases.find((entry) => entry.name === "input the endpoint cannot spell") as DriveCase;
    expect((await drive(record)).sent).toEqual(record.sent);
  });
});

describe("a telnet session that goes wrong", () => {
  it("hangs up on an address the policy refuses", async () => {
    // The guard at create time saw a name; this sees the host actually
    // reached, which is what stops a name resolving to one address and
    // connecting to another.
    const transport = new ScriptedTransport([], "169.254.169.254");
    const connector = connectorFor({}, transport);
    await expect(connector.start()).rejects.toBeInstanceOf(EgressBlockedError);
    expect(golden.blocked_peer.error).toBe("EgressBlockedError");
    expect(connector.isConnected()).toBe(golden.blocked_peer.connected);
    expect(transport.disconnects).toBe(golden.blocked_peer.disconnects);
  });

  it("proceeds when nobody could determine the address", async () => {
    // This only ever aborts on an address positively identified as blocked.
    const connector = connectorFor({}, new ScriptedTransport([], undefined));
    await connector.start();
    expect(connector.isConnected()).toBe(golden.unknown_peer.connected);
  });

  it("still checks a private address when the config asks it to", async () => {
    const connector = connectorFor({ block_private_connector_targets: true }, new ScriptedTransport([], "10.0.0.5"));
    await expect(connector.start()).rejects.toBeInstanceOf(EgressBlockedError);
  });

  it("allows a private address when the config does not", async () => {
    // Reaching an internal server is the tool's purpose, so this is the
    // default.
    const connector = connectorFor({}, new ScriptedTransport([], "10.0.0.5"));
    await connector.start();
    expect(connector.isConnected()).toBe(true);
  });

  it("ends quietly when a read fails", async () => {
    // The poll loop is what keeps every viewer's screen current; raising there
    // would take the loop down with it.
    const transport = new ScriptedTransport([], "203.0.113.7", true);
    const connector = connectorFor({}, transport);
    await connector.start();
    expect(await connector.pollMessages()).toEqual([]);
    expect(connector.isConnected()).toBe(false);
    expect(transport.disconnects).toBe(1);
  });

  it("proceeds when the address comes back empty", async () => {
    // As with an address nobody could determine: this only aborts on one
    // positively identified as blocked.
    const connector = connectorFor({}, new ScriptedTransport([], ""));
    await connector.start();
    expect(connector.isConnected()).toBe(true);
  });

  it("still refuses even when hanging up fails", async () => {
    // The refusal is the point; whether the socket closed cleanly is not.
    class StubbornTransport extends ScriptedTransport {
      override async disconnect(): Promise<void> {
        throw new Error("already gone");
      }
    }
    const connector = connectorFor({}, new StubbornTransport([], "169.254.169.254"));
    await expect(connector.start()).rejects.toBeInstanceOf(EgressBlockedError);
  });

  it("ends quietly even when hanging up fails after a bad read", async () => {
    class StubbornTransport extends ScriptedTransport {
      override async disconnect(): Promise<void> {
        throw new Error("already gone");
      }
    }
    const transport = new StubbornTransport([], "203.0.113.7", true);
    const connector = connectorFor({}, transport);
    await connector.start();
    expect(await connector.pollMessages()).toEqual([]);
    expect(connector.isConnected()).toBe(false);
  });

  it("keeps the newest output when the buffer fills, not the oldest", async () => {
    // What a viewer wants to see is what just happened.
    const record = golden.drive_cases.find((entry) => entry.name === "more output than the buffer holds") as DriveCase;
    const { messages } = await drive(record);
    const screen = (messages[1] as { screen: string }).screen;
    expect(screen).toContain("LAST");
    expect(screen).not.toContain("FIRST");
  });

  it("clamps the cursor to the screen it draws for", async () => {
    // A cursor past the last column or row would put a browser's caret
    // somewhere its own grid does not have.
    for (const [name, cursor] of [
      ["a line wider than the screen", { x: TELNET_COLS - 1, y: 0 }],
      ["more lines than the screen has, with no overlay", { x: 6, y: TELNET_ROWS - 1 }],
    ] as const) {
      const record = golden.drive_cases.find((entry) => entry.name === name) as DriveCase;
      expect((await drive(record)).messages[1]).toMatchObject({ cursor });
    }
  });

  it("says nothing once it is not connected", async () => {
    const connector = connectorFor({});
    expect(await connector.pollMessages()).toEqual([]);
  });

  it("does not send what it cannot send", async () => {
    const transport = new ScriptedTransport();
    const connector = connectorFor({}, transport);
    await connector.handleInput("hello");
    expect(transport.sent).toEqual([]);
  });

  it("refuses a port that is not a whole number", () => {
    // A port written wrong is a config a server should not start on, not one
    // that quietly listens somewhere else — `parseInt` would have made 23.5
    // into 23.
    expect(() => connectorFor({ port: "23.5" })).toThrow("invalid literal for int() with base 10: '23.5'");
    expect(() => connectorFor({ port: "telnet" })).toThrow("invalid literal for int() with base 10: 'telnet'");
  });

  it("takes a port the reference would take", () => {
    for (const port of ["2323", " 2323 ", 2323, 2323.0]) {
      expect(() => connectorFor({ port })).not.toThrow();
    }
  });

  it("knows it is disconnected when the transport goes down under it", async () => {
    const transport = new ScriptedTransport();
    const connector = connectorFor({}, transport);
    await connector.start();
    transport.connected = false;
    expect(connector.isConnected()).toBe(false);
  });

  it("does not read before it has started", async () => {
    // The transport may well be usable; the connector is not yet live.
    const transport = new ScriptedTransport([bytes("early\n")]);
    await transport.connect();
    const connector = connectorFor({}, transport);
    expect(await connector.pollMessages()).toEqual([]);
  });

  it("stays stopped even when hanging up fails", async () => {
    class StubbornTransport extends ScriptedTransport {
      override async disconnect(): Promise<void> {
        this.disconnects += 1;
      }
    }
    const transport = new StubbornTransport();
    const connector = connectorFor({}, transport);
    await connector.start();
    await connector.stop();
    expect(transport.isConnected()).toBe(true);
    expect(connector.isConnected()).toBe(false);
  });

  it("refuses a mode the session does not have", async () => {
    const connector = connectorFor({});
    await expect(connector.setMode("readonly")).rejects.toThrow(golden.mode_refusal.error as string);
  });

  it("releases the hold when input goes back to shared", async () => {
    // So a session cannot be left shared but paused with nobody holding it.
    const connector = connectorFor({ input_mode: "hijack" }, new ScriptedTransport());
    await connector.start();
    await connector.handleControl("pause");
    await connector.setMode("open");
    expect((await connector.getSnapshot()).screen).toContain("Live");
  });

  it("stops when it is told to", async () => {
    const transport = new ScriptedTransport();
    const connector = connectorFor({}, transport);
    await connector.start();
    await connector.stop();
    expect(connector.isConnected()).toBe(false);
    expect(transport.disconnects).toBe(1);
  });

  it("times its messages by the clock it was given", async () => {
    // Which is how the recorded corpus is reproducible at all.
    const connector = connectorFor({}, new ScriptedTransport());
    expect((await connector.getSnapshot()).ts).toBe(golden.fixed_ts);
  });

  it("uses the wall clock when it is given none", async () => {
    const before = Date.now() / 1000;
    const connector = new TelnetSessionConnector("s", "d", {}, { transport: new ScriptedTransport() });
    expect((await connector.getSnapshot()).ts as number).toBeGreaterThanOrEqual(before);
  });
});
