//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  BUILTIN_CONNECTOR_TYPES,
  buildConnector,
  registerConnector,
  registeredTypes,
  type SessionConnector,
  SHELL_COLS,
  SHELL_ROWS,
  SHELL_TRANSCRIPT_LIMIT,
  ShellSessionConnector,
  type WorkerMessage,
} from "./index.ts";

interface ConnectorsGolden {
  session_id: string;
  display_name: string;
  script: Array<{
    name: string;
    kind: "input" | "control";
    payload: string;
    messages: WorkerMessage[];
    analysis: string;
    connected: boolean;
  }>;
  lifecycle: {
    connected_before_start: boolean;
    connected_after_start: boolean;
    connected_after_stop: boolean;
    poll_is_empty: boolean;
    initial_snapshot: WorkerMessage;
    initial_analysis: string;
    cleared: WorkerMessage[];
    set_mode_hijack: WorkerMessage[];
    set_mode_invalid: string;
  };
  config: {
    valid_keys: string[];
    unknown_key: string;
    several_unknown_keys: string;
    no_config_at_all: string | null;
    input_mode_hijack: string;
    input_mode_default: string;
  };
  registry: { builtin_types: string[]; unknown_type: string; shell_builds: string };
  transcript_limit: number;
  cols: number;
  rows: number;
}

const golden = loadGolden<ConnectorsGolden>("connectors_golden.json");

/** A connector on a counter clock, so its timestamps are the recorded ones. */
function connector(config: Record<string, unknown> = {}): ShellSessionConnector {
  let now = 1000;
  return new ShellSessionConnector(golden.session_id, golden.display_name, config, {
    now: () => {
      now += 1;
      return now;
    },
  });
}

/** Replace every timestamp, as the generator does. */
function stable(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(stable);
  }
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, key === "ts" ? "<ts>" : stable(item)]));
  }
  return value;
}

describe("the recorded session", () => {
  it("produces the same messages for the whole script", async () => {
    // One connector driven through the whole script, because almost every
    // step depends on the ones before it — the transcript, the nickname, the
    // mode and the turn counter all carry.
    const shell = connector();
    await shell.start();
    for (const step of golden.script) {
      const messages =
        step.kind === "input" ? await shell.handleInput(step.payload) : await shell.handleControl(step.payload);
      expect({ name: step.name, messages: stable(messages) }).toStrictEqual({
        name: step.name,
        messages: step.messages,
      });
      expect({ name: step.name, analysis: await shell.getAnalysis() }).toStrictEqual({
        name: step.name,
        analysis: step.analysis,
      });
    }
  });
});

describe("the lifecycle", () => {
  it("starts disconnected", async () => {
    // A connector that reported itself live before start would have the
    // runtime routing input into nothing.
    const shell = connector();
    expect(shell.isConnected()).toBe(golden.lifecycle.connected_before_start);
    await shell.start();
    expect(shell.isConnected()).toBe(golden.lifecycle.connected_after_start);
    await shell.stop();
    expect(shell.isConnected()).toBe(golden.lifecycle.connected_after_stop);
  });

  it("polls nothing of its own", async () => {
    // This connector is driven entirely by input; a poll that invented
    // messages would show frames the session never produced.
    const shell = connector();
    expect((await shell.pollMessages()).length === 0).toBe(golden.lifecycle.poll_is_empty);
  });

  it("renders the opening screen", async () => {
    const shell = connector();
    expect(stable(await shell.getSnapshot())).toStrictEqual(golden.lifecycle.initial_snapshot);
  });

  it("describes itself before anything has happened", async () => {
    const shell = connector();
    expect(await shell.getAnalysis()).toBe(golden.lifecycle.initial_analysis);
  });

  it("clears to a snapshot", async () => {
    const shell = connector();
    expect(stable(await shell.clear())).toStrictEqual(golden.lifecycle.cleared);
  });
});

describe("the snapshot", () => {
  it("carries the geometry the reference reports", () => {
    // A mismatch here has the far end drawing for the wrong screen.
    expect(SHELL_COLS).toBe(golden.cols);
    expect(SHELL_ROWS).toBe(golden.rows);
    expect(golden.lifecycle.initial_snapshot.cols).toBe(golden.cols);
    expect(golden.lifecycle.initial_snapshot.rows).toBe(golden.rows);
  });

  it("hashes the screen, so an unchanged frame can be recognised", async () => {
    // The hash is what lets a client skip a redraw; two different screens
    // sharing one would freeze the display.
    const shell = connector();
    const first = (await shell.getSnapshot()).screen_hash;
    await shell.handleInput("something new");
    const second = (await shell.getSnapshot()).screen_hash;
    expect(first).not.toBe(second);
    expect(String(first)).toHaveLength(16);
  });

  it("gives the same screen the same hash", async () => {
    const one = connector();
    const two = connector();
    expect((await one.getSnapshot()).screen_hash).toBe((await two.getSnapshot()).screen_hash);
  });

  it("keeps the cursor on the screen", async () => {
    // A cursor past the last row or column is a cursor the client cannot
    // draw.
    const shell = connector();
    for (let index = 0; index < 30; index += 1) {
      await shell.handleInput(`line ${index}`);
    }
    const snapshot = await shell.getSnapshot();
    const cursor = snapshot.cursor as { x: number; y: number };
    expect(cursor.x).toBeLessThan(golden.cols);
    expect(cursor.y).toBeLessThan(golden.rows);
  });

  it("trims the whitespace off a command's argument", async () => {
    // Otherwise the nickname is stored with the spaces the operator typed,
    // and the prompt is drawn indented by them.
    const step = golden.script.find((entry) => entry.name === "nick with extra spaces");
    expect(String(step?.messages[0]?.screen)).toContain("nickname: bob");
    expect(String(step?.messages[0]?.screen)).not.toContain("nickname:   bob");
  });

  it("says a prompt is on screen", async () => {
    const shell = connector();
    expect((await shell.getSnapshot()).prompt_detected).toStrictEqual({ prompt_id: "shell_prompt" });
  });
});

describe("the transcript", () => {
  it("keeps only the most recent entries", async () => {
    // Unbounded it would grow with the session; the bound is what makes the
    // screen a fixed size.
    const shell = connector();
    for (let index = 0; index < SHELL_TRANSCRIPT_LIMIT * 3; index += 1) {
      await shell.handleInput(`message ${index}`);
    }
    const screen = String((await shell.getSnapshot()).screen);
    expect(screen).not.toContain("message 0");
    expect(screen).toContain(`message ${SHELL_TRANSCRIPT_LIMIT * 3 - 1}`);
  });

  it("uses the recorded bound", () => {
    expect(SHELL_TRANSCRIPT_LIMIT).toBe(golden.transcript_limit);
  });
});

describe("the configuration", () => {
  it("accepts the one key it knows", () => {
    expect(golden.config.valid_keys).toStrictEqual(["input_mode"]);
    expect(() => connector({ input_mode: "hijack" })).not.toThrow();
  });

  it("refuses a key it does not know", () => {
    // A typo in a session's config would otherwise be accepted and silently
    // ignored, and the session would run in a mode nobody asked for.
    expect(() => connector({ host: "x" })).toThrow(golden.config.unknown_key);
  });

  it("names every unknown key, in order", () => {
    // Sorted, so the message is the same however the object was built.
    expect(() => connector({ zeta: 1, alpha: 2 })).toThrow(golden.config.several_unknown_keys);
    expect(golden.config.several_unknown_keys).toContain("['alpha', 'zeta']");
  });

  it("accepts no config at all", () => {
    expect(golden.config.no_config_at_all).toBeNull();
    expect(() => new ShellSessionConnector(golden.session_id, golden.display_name)).not.toThrow();
  });

  it("defaults the input mode", async () => {
    const shell = connector();
    expect(await shell.getAnalysis()).toContain(`input_mode: ${golden.config.input_mode_default}`);
  });

  it("takes the input mode from the config", async () => {
    const shell = connector({ input_mode: "hijack" });
    expect(await shell.getAnalysis()).toContain(`input_mode: ${golden.config.input_mode_hijack}`);
  });
});

describe("changing the mode", () => {
  it("emits a hello and a snapshot", async () => {
    // The hello is how the far end learns the mode changed; a snapshot alone
    // would redraw the screen without telling it anything. Recorded after a
    // clear, so the transcript is empty and the frame is the recorded one.
    const shell = connector();
    await shell.clear();
    expect(stable(await shell.setMode("hijack"))).toStrictEqual(golden.lifecycle.set_mode_hijack);
  });

  it("refuses a mode that is not one", async () => {
    await expect(connector().setMode("sideways")).rejects.toThrow(golden.lifecycle.set_mode_invalid);
  });

  it("releases the pause when going back to open", async () => {
    // Otherwise a session left paused stays paused with nobody holding it.
    const shell = connector();
    await shell.handleControl("pause");
    expect(await shell.getAnalysis()).toContain("paused: True");
    await shell.setMode("open");
    expect(await shell.getAnalysis()).toContain("paused: False");
  });

  it("keeps the pause when going to hijack", async () => {
    const shell = connector();
    await shell.handleControl("pause");
    await shell.setMode("hijack");
    expect(await shell.getAnalysis()).toContain("paused: True");
  });
});

describe("the registry", () => {
  it("knows the built-in types", () => {
    expect([...BUILTIN_CONNECTOR_TYPES].sort()).toStrictEqual(golden.registry.builtin_types);
  });

  it("builds a shell connector", () => {
    const built = buildConnector(golden.session_id, golden.display_name, "shell", {});
    expect(built).toBeInstanceOf(ShellSessionConnector);
    expect(golden.registry.shell_builds).toBe("ShellSessionConnector");
  });

  it("refuses a type it does not know", () => {
    // A session created with a typo must not silently land on some other
    // transport.
    expect(() => buildConnector(golden.session_id, golden.display_name, "carrier-pigeon", {})).toThrow(
      golden.registry.unknown_type,
    );
  });

  it("quotes the type it refused", () => {
    expect(golden.registry.unknown_type).toContain("'carrier-pigeon'");
  });

  it("takes a registration", () => {
    const made: string[] = [];
    const factory = (sessionId: string): SessionConnector => {
      made.push(sessionId);
      return new ShellSessionConnector(sessionId, "custom");
    };
    registerConnector("custom", factory);
    expect(registeredTypes().has("custom")).toBe(true);
    buildConnector("w9", "custom", "custom", {});
    expect(made).toStrictEqual(["w9"]);
  });

  it("passes the config through to the factory", () => {
    // A connector that never saw its config would run on defaults, which is
    // the failure the config-key check exists to prevent.
    let seen: Record<string, unknown> | undefined;
    registerConnector("recording", (sessionId, displayName, config) => {
      seen = config;
      return new ShellSessionConnector(sessionId, displayName);
    });
    buildConnector("w9", "name", "recording", { input_mode: "hijack" });
    expect(seen).toStrictEqual({ input_mode: "hijack" });
  });

  it("lets a registration replace a built-in", () => {
    // The registry is how a deployment substitutes its own transport.
    const original = buildConnector("w9", "name", "shell", {});
    expect(original).toBeInstanceOf(ShellSessionConnector);
    registerConnector("shell", (sessionId, displayName) => new ShellSessionConnector(sessionId, displayName));
    expect(buildConnector("w9", "name", "shell", {})).toBeInstanceOf(ShellSessionConnector);
  });
});
