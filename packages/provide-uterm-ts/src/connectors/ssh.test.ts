//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { EgressBlockedError } from "../egress/index.ts";
import { loadGolden } from "../testing/golden.ts";
import {
  SSH_COLS,
  SSH_CONNECTOR_KEYS,
  SSH_ROWS,
  type SshChunk,
  type SshConnectOptions,
  type SshConnectorSession,
  SshReadTimeout,
  SshSessionConnector,
} from "./index.ts";

interface RecordedChunk {
  binary: boolean;
  data: string;
}

interface ConfigCase {
  name: string;
  config: Record<string, unknown>;
  error: string | null;
  snapshot?: Record<string, unknown>;
  analysis?: string;
  client_keys?: string[];
}

interface DriveCase {
  name: string;
  config: Record<string, unknown>;
  chunks: RecordedChunk[];
  messages: Record<string, unknown>[];
  snapshot: Record<string, unknown>;
  analysis: string;
  sent: string[];
  connected: boolean;
}

interface SshGolden {
  fixed_ts: number;
  cols: number;
  rows: number;
  config_cases: ConfigCase[];
  drive_cases: DriveCase[];
  mode_refusal: { error: string | null };
  stop: { eof: number; process_closes: number; conn_closes: number; conn_waits: number; connected: boolean };
  partial_states: Record<string, { connected: boolean; polls: number }>;
}

const golden = loadGolden<SshGolden>("sshconnector_golden.json");

/** The settings every driven case starts from: a host key file. */
const CHECKED = { known_hosts: "/etc/ssh/known_hosts" };

function bytes(text: string): Uint8Array {
  return Uint8Array.from([...text].map((character) => character.charCodeAt(0)));
}

function latin1(data: Uint8Array): string {
  return [...data].map((byte) => String.fromCharCode(byte)).join("");
}

function chunkOf(record: RecordedChunk): SshChunk {
  return record.binary ? { binary: true, data: bytes(record.data) } : { binary: false, data: record.data };
}

/** A session that answers from a script rather than a shell. */
class ScriptedSession implements SshConnectorSession {
  readonly written: Uint8Array[] = [];
  /** What was done to it, in the order it was done. */
  readonly teardown: string[] = [];
  eofs = 0;
  processCloses = 0;
  connectionCloses = 0;
  #chunks: SshChunk[];
  #peerIp: string | undefined;

  constructor(chunks: SshChunk[] = [], peerIp: string | undefined = "203.0.113.7") {
    this.#chunks = [...chunks];
    this.#peerIp = peerIp;
  }

  async read(): Promise<SshChunk> {
    if (this.#chunks.length === 0) {
      // Nothing to read is a shell waiting at a prompt, not a closed session.
      throw new SshReadTimeout("nothing to read");
    }
    return this.#chunks.shift() as SshChunk;
  }

  async write(data: Uint8Array): Promise<void> {
    this.written.push(data);
  }

  async writeEof(): Promise<void> {
    this.teardown.push("eof");
    this.eofs += 1;
  }

  async closeProcess(): Promise<void> {
    this.teardown.push("process");
    this.processCloses += 1;
  }

  async closeConnection(): Promise<void> {
    this.teardown.push("connection");
    this.connectionCloses += 1;
  }

  peerIp(): string | undefined {
    return this.#peerIp;
  }
}

/** One connector, wired to a scripted session and a clock that does not move. */
function connectorFor(
  config: Record<string, unknown>,
  session: ScriptedSession = new ScriptedSession(),
  onInsecureHostCheck?: (sessionId: string, host: string) => void,
): SshSessionConnector {
  return new SshSessionConnector(
    "sess-1",
    "Demo Session",
    { ...CHECKED, ...config },
    {
      connect: async () => session,
      now: () => golden.fixed_ts,
      ...(onInsecureHostCheck === undefined ? {} : { onInsecureHostCheck }),
    },
  );
}

/** Build with exactly these settings, nothing added. */
function build(config: Record<string, unknown>): SshSessionConnector {
  return new SshSessionConnector("sess-1", "Demo Session", config, {
    connect: async () => new ScriptedSession(),
    now: () => golden.fixed_ts,
    onInsecureHostCheck: () => undefined,
  });
}

type Step = (connector: SshSessionConnector) => Promise<Record<string, unknown>[]>;

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
    "one chunk of output": [poll],
    "output with high-byte drawing characters": [poll],
    "output that arrives as text": [poll],
    "output as text with a character latin-1 cannot hold": [poll],
    "output as text at the last character latin-1 can hold": [poll],
    "two chunks, counted": [poll, poll],
    "nothing to read": [poll],
    "an empty read": [poll],
    "input sent upstream": [input("ls -la\r\n")],
    "input outside ASCII sent upstream": [input("echo naïve\r\n")],
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

async function drive(record: DriveCase): Promise<{
  messages: Record<string, unknown>[];
  session: ScriptedSession;
  connector: SshSessionConnector;
}> {
  const session = new ScriptedSession(record.chunks.map(chunkOf));
  const connector = connectorFor(record.config, session);
  await connector.start();
  const messages: Record<string, unknown>[] = [];
  for (const step of stepsFor(record)) {
    messages.push(...(await step(connector)));
  }
  return { messages, session, connector };
}

describe("the settings an SSH session takes", () => {
  it.each(golden.config_cases)("$name", async (record) => {
    if (record.error !== null) {
      expect(() => build(record.config)).toThrow(record.error);
      return;
    }
    const connector = build(record.config);
    expect(await connector.getSnapshot()).toEqual(record.snapshot);
    expect(await connector.getAnalysis()).toBe(record.analysis);
    expect([...connector.clientKeys]).toEqual(record.client_keys);
  });

  it("refuses a session that would not check the host key", () => {
    // The whole attack host keys exist to stop: a connector pointed at a name
    // would otherwise accept whatever answered.
    expect(() => build({})).toThrow(
      "ssh_connector requires known_hosts for session 'sess-1' connecting to 127.0.0.1; " +
        "set connector_config.known_hosts to a known_hosts file path, " +
        "or set insecure_no_host_check=true to disable host key verification",
    );
  });

  it("says both ways out of it", () => {
    expect(() => build({ known_hosts: "/etc/ssh/known_hosts" })).not.toThrow();
    expect(() => build({ insecure_no_host_check: true })).not.toThrow();
  });

  it("reads the escape hatch the way Python reads it", () => {
    // An empty list is false in Python and true in JavaScript. Reading this
    // flag with `Boolean()` would turn `insecure_no_host_check = []` into a
    // switched-off host-key check.
    for (const value of [[], 0, "", false, null, {}]) {
      expect(() => build({ insecure_no_host_check: value })).toThrow("requires known_hosts");
    }
    for (const value of [1, "yes", true, ["x"], { why: "testing" }]) {
      expect(() => build({ insecure_no_host_check: value })).not.toThrow();
    }
  });

  it("says so out loud when the check is off", () => {
    // A session that trusts whatever answers should be visible in the log of a
    // server that starts.
    const told: [string, string][] = [];
    new SshSessionConnector(
      "sess-1",
      "Demo Session",
      { insecure_no_host_check: true, host: "shell.example" },
      { connect: async () => new ScriptedSession(), onInsecureHostCheck: (id, host) => told.push([id, host]) },
    );
    expect(told).toEqual([["sess-1", "shell.example"]]);
  });

  it("says nothing when the check is on", () => {
    const told: string[] = [];
    connectorFor({}, new ScriptedSession(), (id) => told.push(id));
    expect(told).toEqual([]);
  });

  it("is built with the check off even when nobody is listening", () => {
    expect(
      () =>
        new SshSessionConnector(
          "sess-1",
          "d",
          { insecure_no_host_check: true },
          { connect: async () => new ScriptedSession() },
        ),
    ).not.toThrow();
  });

  it("refuses a key path rather than ignoring it", () => {
    // A session naming a key file and silently connecting without it would
    // look like it worked until the far end asked for the key.
    expect(() => build({ ...CHECKED, client_key_path: "/home/ada/.ssh/id_ed25519" })).toThrow(
      "ssh connector_config.client_key_path is not supported",
    );
  });

  it("checks the names it was given before anything else", () => {
    // So an operator fixes the typo they made rather than the consequence of
    // it.
    expect(() => build({ hostname: "h" })).toThrow("unknown ssh connector_config keys: ['hostname']");
    expect(() => build({ client_key_path: "/x" })).toThrow("client_key_path is not supported");
  });

  it("collects key material in the order it was named", () => {
    const connector = build({ ...CHECKED, client_keys: ["/a", null, "/b"], client_key: "/c", client_key_data: "/d" });
    expect([...connector.clientKeys]).toEqual(["/a", "/b", "/c", "/d"]);
  });

  it("takes one key named on its own", () => {
    expect([...build({ ...CHECKED, client_keys: "/only" }).clientKeys]).toEqual(["/only"]);
  });

  it("takes the settings the reference takes", () => {
    expect([...SSH_CONNECTOR_KEYS].sort()).toEqual([
      "block_private_connector_targets",
      "client_key",
      "client_key_data",
      "client_key_path",
      "client_keys",
      "host",
      "hub_overlay",
      "input_mode",
      "insecure_no_host_check",
      "known_hosts",
      "password",
      "port",
      "username",
    ]);
  });

  it("refuses a port that is not a whole number", () => {
    expect(() => build({ ...CHECKED, port: "22.5" })).toThrow("invalid literal for int() with base 10: '22.5'");
  });

  it("draws for the screen the reference draws for", () => {
    expect([SSH_COLS, SSH_ROWS]).toEqual([golden.cols, golden.rows]);
  });
});

describe("an SSH session in use", () => {
  it.each(golden.drive_cases)("$name", async (record) => {
    const { messages, session, connector } = await drive(record);
    expect(messages).toEqual(record.messages);
    expect(session.written.map(latin1)).toEqual(record.sent);
    expect(connector.isConnected()).toBe(record.connected);
  });

  it.each(golden.drive_cases)("$name — and what it looks like afterwards", async (record) => {
    const { connector } = await drive(record);
    expect(await connector.getSnapshot()).toEqual(record.snapshot);
    expect(await connector.getAnalysis()).toBe(record.analysis);
  });

  it("reads high bytes as CP437", async () => {
    const record = golden.drive_cases.find(
      (entry) => entry.name === "output with high-byte drawing characters",
    ) as DriveCase;
    expect(((await drive(record)).messages[0] as { data: string }).data).toBe("╔═╗\n");
  });

  it("keeps the last character latin-1 can hold", async () => {
    // The boundary is inclusive, so `ÿ` is a byte and not a question mark —
    // and CP437 reads that byte as a non-breaking space.
    const record = golden.drive_cases.find(
      (entry) => entry.name === "output as text at the last character latin-1 can hold",
    ) as DriveCase;
    expect(((await drive(record)).messages[0] as { data: string }).data).toBe("\u00a0\n");
  });

  it("replaces a character latin-1 cannot hold, as the reference does", async () => {
    // Text is put back into bytes before it is read as CP437, so a snowman
    // becomes a question mark rather than passing through.
    const record = golden.drive_cases.find(
      (entry) => entry.name === "output as text with a character latin-1 cannot hold",
    ) as DriveCase;
    const { messages, connector } = await drive(record);
    expect((messages[0] as { data: string }).data).toBe("snow ?\n");
    expect(await connector.getAnalysis()).toContain("bytes_received: 7");
  });

  it("sends what was typed as UTF-8", async () => {
    // Unlike telnet's CP437: a modern shell reads UTF-8, and this connector's
    // own overlay is the only thing here that speaks CP437.
    const session = new ScriptedSession();
    const connector = connectorFor({}, session);
    await connector.start();
    await connector.handleInput("naïve");
    expect([...(session.written[0] as Uint8Array)]).toEqual([...new TextEncoder().encode("naïve")]);
  });

  it("says nothing at all when there is nothing to read", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "nothing to read") as DriveCase;
    const { messages, connector } = await drive(record);
    expect(messages).toEqual([]);
    expect(connector.isConnected()).toBe(true);
  });

  it("says nothing on an empty read either", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "an empty read") as DriveCase;
    expect((await drive(record)).messages).toEqual([]);
  });

  it("keeps the newest output when the buffer fills", async () => {
    const record = golden.drive_cases.find((entry) => entry.name === "more output than the buffer holds") as DriveCase;
    const screen = ((await drive(record)).messages[1] as { screen: string }).screen;
    expect(screen).toContain("LAST");
    expect(screen).not.toContain("FIRST");
  });

  it("clamps the cursor to the screen it draws for", async () => {
    for (const [name, cursor] of [
      ["a line wider than the screen", { x: SSH_COLS - 1, y: 0 }],
      ["more lines than the screen has, with no overlay", { x: 6, y: SSH_ROWS - 1 }],
    ] as const) {
      const record = golden.drive_cases.find((entry) => entry.name === name) as DriveCase;
      expect((await drive(record)).messages[1]).toMatchObject({ cursor });
    }
  });
});

describe("an SSH session shutting down or going wrong", () => {
  it("closes the shell on an address the policy refuses", async () => {
    // The handshake, host key included, completed against the name — this
    // reads where the connection actually landed, before anything is typed
    // into it.
    const session = new ScriptedSession([], "169.254.169.254");
    const connector = connectorFor({}, session);
    await expect(connector.start()).rejects.toBeInstanceOf(EgressBlockedError);
    expect(session.connectionCloses).toBe(1);
    expect(connector.isConnected()).toBe(false);
  });

  it("proceeds when the address could not be determined, or is empty", async () => {
    for (const peer of [undefined, ""]) {
      const connector = connectorFor({}, new ScriptedSession([], peer));
      await connector.start();
      expect(connector.isConnected()).toBe(true);
    }
  });

  it("allows a private address by default and refuses it on request", async () => {
    const allowed = connectorFor({}, new ScriptedSession([], "10.0.0.5"));
    await allowed.start();
    expect(allowed.isConnected()).toBe(true);

    const blocked = connectorFor({ block_private_connector_targets: true }, new ScriptedSession([], "10.0.0.5"));
    await expect(blocked.start()).rejects.toBeInstanceOf(EgressBlockedError);
  });

  it("still refuses even when closing fails", async () => {
    class StubbornSession extends ScriptedSession {
      override async closeConnection(): Promise<void> {
        throw new Error("already gone");
      }
    }
    const connector = connectorFor({}, new StubbornSession([], "169.254.169.254"));
    await expect(connector.start()).rejects.toBeInstanceOf(EgressBlockedError);
  });

  it("ends the input stream before it closes anything", async () => {
    // So the far end sees a shell that exits rather than one that is killed.
    const session = new ScriptedSession();
    const connector = connectorFor({}, session);
    await connector.start();
    await connector.stop();
    // In that order: the far end sees a shell that exits rather than one
    // whose connection vanished under it.
    expect(session.teardown).toEqual(["eof", "process", "connection"]);
    expect(session.eofs).toBe(golden.stop.eof);
    expect(session.processCloses).toBe(golden.stop.process_closes);
    expect(session.connectionCloses).toBe(golden.stop.conn_closes);
    expect(connector.isConnected()).toBe(golden.stop.connected);
  });

  it("stops cleanly having never started", async () => {
    const connector = connectorFor({});
    await connector.stop();
    expect(connector.isConnected()).toBe(false);
  });

  it("stops even when every step of it fails", async () => {
    class StubbornSession extends ScriptedSession {
      override async writeEof(): Promise<void> {
        throw new Error("gone");
      }
      override async closeProcess(): Promise<void> {
        throw new Error("gone");
      }
      override async closeConnection(): Promise<void> {
        throw new Error("gone");
      }
    }
    const connector = connectorFor({}, new StubbornSession());
    await connector.start();
    await connector.stop();
    expect(connector.isConnected()).toBe(false);
  });

  it("says nothing once it is not connected", async () => {
    const connector = connectorFor({});
    expect(await connector.pollMessages()).toEqual([]);
    expect(golden.partial_states["the flag cleared"]?.polls).toBe(0);
  });

  it("does not send what it cannot send", async () => {
    const session = new ScriptedSession();
    const connector = connectorFor({}, session);
    await connector.handleInput("hello");
    expect(session.written).toEqual([]);
  });

  it("passes a read failure on rather than swallowing it", async () => {
    // Only a timeout is quiet; a real failure is the caller's to see.
    class BrokenSession extends ScriptedSession {
      override async read(): Promise<SshChunk> {
        throw new Error("channel closed");
      }
    }
    const connector = connectorFor({}, new BrokenSession());
    await connector.start();
    await expect(connector.pollMessages()).rejects.toThrow("channel closed");
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

  it("asks for the shell it drew for", async () => {
    let asked: SshConnectOptions | undefined;
    const connector = new SshSessionConnector(
      "sess-1",
      "Demo Session",
      {
        ...CHECKED,
        host: "shell.example",
        port: 2222,
        username: "ada",
        password: "hunter2", // pragma: allowlist secret
        client_keys: ["/a", "/b"],
      },
      {
        connect: async (options) => {
          asked = options;
          return new ScriptedSession();
        },
      },
    );
    await connector.start();
    expect(asked).toEqual({
      host: "shell.example",
      port: 2222,
      username: "ada",
      password: "hunter2", // pragma: allowlist secret
      knownHosts: "/etc/ssh/known_hosts",
      clientKeys: ["/a", "/b"],
      cols: SSH_COLS,
      rows: SSH_ROWS,
    });
  });

  it("passes no known-hosts file when the check is off", async () => {
    let asked: SshConnectOptions | undefined;
    const connector = new SshSessionConnector(
      "sess-1",
      "d",
      { insecure_no_host_check: true },
      {
        connect: async (options) => {
          asked = options;
          return new ScriptedSession();
        },
        onInsecureHostCheck: () => undefined,
      },
    );
    await connector.start();
    expect(asked?.knownHosts).toBeUndefined();
    expect(asked?.password).toBeUndefined();
  });

  it("uses the wall clock when it is given none", async () => {
    const before = Date.now() / 1000;
    const connector = new SshSessionConnector("s", "d", CHECKED, { connect: async () => new ScriptedSession() });
    expect((await connector.getSnapshot()).ts as number).toBeGreaterThanOrEqual(before);
  });
});
