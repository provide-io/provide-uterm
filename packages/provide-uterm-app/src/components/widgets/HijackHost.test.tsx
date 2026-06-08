//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTerminalStore } from "../../stores/terminalStore";
import { HijackHost } from "./HijackHost";

// HijackHost renders a <uterm-session> custom element and drives it
// imperatively (config + connect()). Register a lightweight stub element so the
// host wires up against a real DOM node without pulling in the full Lit session
// element (xterm, WebSocket, DeckMux, etc.).
interface SessionConfig {
  workerId: string;
  showAnalysis?: boolean;
  mobileKeys?: boolean;
  onResize?: (cols: number, rows: number) => void;
  onPresenceMessage?: (msg: Record<string, unknown>) => void;
}

const instances: StubSession[] = [];

class StubSession extends HTMLElement {
  config: SessionConfig | null = null;
  connect = vi.fn();
  sendControlMessage = vi.fn();
  terminalElement: HTMLElement = document.createElement("div");
  constructor() {
    super();
    instances.push(this);
  }
}

if (!customElements.get("uterm-session")) {
  customElements.define("uterm-session", StubSession);
}

function resetStore() {
  useTerminalStore.setState({ mounted: false, error: null, cols: 0, rows: 0 });
}

beforeEach(() => {
  instances.length = 0;
  resetStore();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetStore();
});

describe("HijackHost", () => {
  it("renders a <uterm-session> element and marks the store mounted", () => {
    const { container } = render(<HijackHost sessionId="test-session" />);
    expect(container.querySelector("uterm-session")).not.toBeNull();
    expect(useTerminalStore.getState().mounted).toBe(true);
  });

  it("configures the session with workerId and an onResize callback", () => {
    render(<HijackHost sessionId="my-worker" />);
    expect(instances).toHaveLength(1);
    expect(instances[0]?.config?.workerId).toBe("my-worker");
    expect(typeof instances[0]?.config?.onResize).toBe("function");
  });

  it("sets showAnalysis and mobileKeys false for a non-operator surface", () => {
    render(<HijackHost sessionId="s1" surface="user" />);
    expect(instances[0]?.config?.showAnalysis).toBe(false);
    expect(instances[0]?.config?.mobileKeys).toBe(false);
  });

  it("sets showAnalysis and mobileKeys true for an operator surface", () => {
    render(<HijackHost sessionId="s1" surface="operator" />);
    expect(instances[0]?.config?.showAnalysis).toBe(true);
    expect(instances[0]?.config?.mobileKeys).toBe(true);
  });

  it("connects the session and marks the store mounted on mount", () => {
    render(<HijackHost sessionId="s1" />);
    expect(instances[0]?.connect).toHaveBeenCalledTimes(1);
    expect(useTerminalStore.getState().mounted).toBe(true);
  });

  it("installs a single 15s keepalive interval for idle-prune protection", () => {
    const spy = vi.spyOn(globalThis, "setInterval");
    render(<HijackHost sessionId="s1" />);
    // Exactly one interval, at 15s cadence — anything faster is a regression
    // to per-keystroke polling.
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0]?.[1]).toBe(15_000);
  });

  it("forwards onResize to the terminal-dimension store", () => {
    render(<HijackHost sessionId="s1" />);
    instances[0]?.config?.onResize?.(120, 40);
    const { cols, rows } = useTerminalStore.getState();
    expect(cols).toBe(120);
    expect(rows).toBe(40);
  });

  it("clears the keepalive interval on unmount", () => {
    const clearSpy = vi.spyOn(globalThis, "clearInterval");
    const { unmount } = render(<HijackHost sessionId="s1" />);
    unmount();
    expect(clearSpy).toHaveBeenCalled();
  });
});
