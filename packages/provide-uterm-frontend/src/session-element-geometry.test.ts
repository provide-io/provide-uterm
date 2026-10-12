//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { registerUtermSessionElement, type UtermSessionElement } from "./session-element.js";

registerUtermSessionElement();

// A snapshot names the size the session's terminal is. A page that sizes its
// own terminal follows it through `uterm:geometry`, which fires once the
// terminal exists, before the snapshot is painted, and only when the size moved.

class MockTerminal {
  writes: string[] = [];
  open() {}
  focus() {}
  dispose() {}
  loadAddon() {}
  write(data: string) {
    this.writes.push(data);
  }
  onData() {
    return { dispose() {} };
  }
  onScroll() {
    return { dispose() {} };
  }
}

type SessionInternals = { _handleMessage: (message: unknown) => void };

let element: UtermSessionElement | null = null;

beforeEach(() => {
  vi.stubGlobal("Terminal", MockTerminal);
});

afterEach(() => {
  element?.remove();
  element = null;
  vi.unstubAllGlobals();
});

async function makeSession(): Promise<UtermSessionElement> {
  element = document.createElement("uterm-session") as UtermSessionElement;
  element.config = { workerId: "geometry" };
  document.body.appendChild(element);
  await element.updateComplete;
  return element;
}

function receive(session: UtermSessionElement, frame: Record<string, unknown>): void {
  (session as unknown as SessionInternals)._handleMessage(frame);
}

describe("uterm:geometry", () => {
  it("announces a view's first snapshot, to a listener that already finds the terminal", async () => {
    const session = await makeSession();
    const seen: Array<{ detail: unknown; terminal: unknown; writesBefore: number }> = [];
    session.addEventListener("uterm:geometry", (event) => {
      const terminal = session.terminal as unknown as MockTerminal | null;
      seen.push({
        detail: (event as CustomEvent).detail,
        terminal,
        writesBefore: terminal?.writes.length ?? -1,
      });
    });

    receive(session, { type: "snapshot", screen: "", cols: 100, rows: 40 });

    expect(seen).toHaveLength(1);
    expect(seen[0]?.detail).toEqual({ cols: 100, rows: 40 });
    expect(seen[0]?.terminal).not.toBeNull();
    // Before the paint, so the listener can resize the terminal it lands in.
    expect(seen[0]?.writesBefore).toBe(0);
  });

  it("says nothing when the size has not moved, and again when it has", async () => {
    const session = await makeSession();
    const details: unknown[] = [];
    session.addEventListener("uterm:geometry", (event) => details.push((event as CustomEvent).detail));

    receive(session, { type: "snapshot", screen: "", cols: 100, rows: 40 });
    receive(session, { type: "snapshot", screen: "", cols: 100, rows: 40 });
    receive(session, { type: "snapshot", screen: "", cols: 80, rows: 40 });

    expect(details).toEqual([
      { cols: 100, rows: 40 },
      { cols: 80, rows: 40 },
    ]);
  });

  it("says nothing for a snapshot that names no size", async () => {
    const session = await makeSession();
    const listener = vi.fn();
    session.addEventListener("uterm:geometry", listener);

    receive(session, { type: "snapshot", screen: "" });
    receive(session, { type: "snapshot", screen: "", cols: "100", rows: 40 });

    expect(listener).not.toHaveBeenCalled();
  });

  it("says nothing for a size that is not a positive whole number", async () => {
    const session = await makeSession();
    const listener = vi.fn();
    session.addEventListener("uterm:geometry", listener);

    for (const [cols, rows] of [
      [Number.NaN, 40],
      [100, Number.NaN],
      [0, 40],
      [100, -1],
      [100.5, 40],
      [Number.POSITIVE_INFINITY, 40],
    ]) {
      receive(session, { type: "snapshot", screen: "", cols, rows });
    }

    expect(listener).not.toHaveBeenCalled();
  });

  it("announces the size again to the terminal a new worker gets", async () => {
    const session = await makeSession();
    const details: unknown[] = [];
    session.addEventListener("uterm:geometry", (event) => details.push((event as CustomEvent).detail));

    receive(session, { type: "snapshot", screen: "", cols: 100, rows: 40 });
    session.config = { workerId: "another" };
    session.connect();
    receive(session, { type: "snapshot", screen: "", cols: 100, rows: 40 });

    expect(details).toEqual([
      { cols: 100, rows: 40 },
      { cols: 100, rows: 40 },
    ]);
  });
});
