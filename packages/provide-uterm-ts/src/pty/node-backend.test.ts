//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { PtyConnector, QUEUE_CAP, spawnNodePty } from "./index.ts";

/** Poll until `predicate` holds, or give up. A real child takes real time. */
async function until(predicate: () => boolean, attempts = 200): Promise<boolean> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (predicate()) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return predicate();
}

describe("a real pseudo-terminal", () => {
  it("says nothing yet rather than reporting an ending", async () => {
    // The distinction the reference gets from a non-blocking descriptor: a
    // terminal that called its first quiet moment an ending would end every
    // session before its first output.
    const backend = await spawnNodePty({ command: "/bin/sh", args: ["-c", "sleep 0.3; echo late"] });
    expect(backend.read()).toBeUndefined();
    expect(backend.isAlive()).toBe(true);
    await until(() => !backend.isAlive(), 100);
    await backend.close();
  });

  it("hands over the last output before reporting the ending", async () => {
    // Output that arrived before the child exited must not be lost to the
    // ending that followed it.
    const backend = await spawnNodePty({ command: "/bin/echo", args: ["final words"] });
    let seen = "";
    await until(() => {
      seen += new TextDecoder().decode(backend.read() ?? new Uint8Array());
      return seen.includes("final words") && !backend.isAlive();
    });
    expect(seen).toContain("final words");
    expect(backend.read()).toEqual(new Uint8Array());
    await backend.close();
  });

  it("runs where it was told to, at the size it was given", async () => {
    // A session opened in the wrong directory, or sized wrongly, is a session
    // that renders wrongly from its first line.
    const backend = await spawnNodePty({
      command: "/bin/sh",
      args: ["-c", "pwd; tput cols"],
      cwd: "/tmp",
      cols: 132,
      rows: 43,
    });
    let seen = "";
    await until(() => {
      seen += new TextDecoder().decode(backend.read() ?? new Uint8Array());
      return seen.includes("132");
    });
    await backend.close();
    expect(seen).toContain("/tmp");
    expect(seen).toContain("132");
  });

  it("runs a command and hands back what it printed", async () => {
    const backend = await spawnNodePty({ command: "/bin/echo", args: ["hello from a real pty"] });
    let seen = "";
    await until(() => {
      seen += new TextDecoder().decode(backend.read());
      return seen.includes("hello from a real pty");
    });
    await backend.close();
    expect(seen).toContain("hello from a real pty");
  });

  it("gives the connector a session it can actually poll", async () => {
    // The whole point of the backend interface: the state machine that was
    // checked against the corpus drives a real shell unchanged.
    const connector = new PtyConnector("sess-1", "a real session", { command: "/bin/sh" });
    const backend = await spawnNodePty({ command: "/bin/sh", args: ["-c", "echo ready"] });
    connector.attach(backend);

    let screen = "";
    for (let attempt = 0; attempt < 200 && !screen.includes("ready"); attempt += 1) {
      const messages = await connector.pollMessages();
      if (messages.length > 0) {
        screen = String(messages[0]?.screen);
      }
      if (!screen.includes("ready")) {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
    }
    await connector.stop();
    expect(screen).toContain("ready");
  });

  it("carries what somebody types through to the shell", async () => {
    const connector = new PtyConnector("sess-1", "a real session", { command: "/bin/sh" });
    const backend = await spawnNodePty({ command: "/bin/sh", args: [] });
    connector.attach(backend);

    await connector.handleInput("echo typed-this\nexit\n");
    let screen = "";
    for (let attempt = 0; attempt < 300 && !screen.includes("typed-this"); attempt += 1) {
      const messages = await connector.pollMessages();
      if (messages.length > 0) {
        screen = String(messages[0]?.screen);
      }
      if (!screen.includes("typed-this")) {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
    }
    await connector.stop();
    expect(screen).toContain("typed-this");
  });

  it("runs a command given no arguments at all", async () => {
    const backend = await spawnNodePty({ command: "/bin/date" });
    expect(backend.pid).toBeGreaterThan(0);
    await until(() => !backend.isAlive());
    await backend.close();
  });

  it("notices the child exiting", async () => {
    const backend = await spawnNodePty({ command: "/bin/sh", args: ["-c", "exit 0"] });
    expect(await until(() => !backend.isAlive())).toBe(true);
    await backend.close();
  });

  it("refuses to write once the child has gone", async () => {
    // What a real terminal does, and what the connector reads as the session
    // ending rather than as a fault.
    const backend = await spawnNodePty({ command: "/bin/sh", args: ["-c", "exit 0"] });
    await until(() => !backend.isAlive());
    expect(() => backend.write(new TextEncoder().encode("ls\n"))).toThrow();
    await backend.close();
  });

  it("can be closed twice", async () => {
    // Closing an already-dead child must not kill whatever reused its number.
    const backend = await spawnNodePty({ command: "/bin/sh", args: ["-c", "exit 0"] });
    await backend.close();
    await expect(backend.close()).resolves.toBeUndefined();
  });

  it("passes the environment it was given", async () => {
    const backend = await spawnNodePty({
      command: "/bin/sh",
      args: ["-c", "echo $UTERM_TEST_MARKER"],
      env: { UTERM_TEST_MARKER: "marker-value" },
    });
    let seen = "";
    await until(() => {
      seen += new TextDecoder().decode(backend.read());
      return seen.includes("marker-value");
    });
    await backend.close();
    expect(seen).toContain("marker-value");
  });

  it("hands over each chunk once", async () => {
    // Reading takes what has arrived; reading again must not repeat it.
    const backend = await spawnNodePty({ command: "/bin/echo", args: ["once"] });
    let seen = "";
    await until(() => {
      seen += new TextDecoder().decode(backend.read());
      return seen.includes("once");
    });
    const again = new TextDecoder().decode(backend.read());
    await backend.close();
    expect(again).not.toContain("once");
  });

  it("bounds what it keeps for a child nobody is reading", async () => {
    // A child writing faster than anybody polls must cost a fixed amount of
    // memory rather than an unbounded one.
    expect(QUEUE_CAP).toBe(1 << 20);
    // Two megabytes, so the cap genuinely has to discard.
    const backend = await spawnNodePty({
      command: "/bin/sh",
      // `yes | head`, not `dd if=/dev/zero bs= count=`: the dd form is the
      // binary_padding_via_dd EDR signature. This only needs 2MB so the cap discards.
      args: ["-c", "yes a | head -c 2097152"],
    });
    await until(() => !backend.isAlive());
    await new Promise((resolve) => setTimeout(resolve, 100));
    const kept = backend.read() ?? new Uint8Array();
    await backend.close();
    expect(kept.length).toBeLessThanOrEqual(QUEUE_CAP);
    expect(kept.length).toBeGreaterThan(0);
  });
});
