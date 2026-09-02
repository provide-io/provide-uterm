//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTerminalStore } from "../../stores/terminalStore";

// HijackHost side-effect-imports the real session-element (to register
// <uterm-session>); stub that module out so our lightweight stub element below
// is the one registered, instead of the full Lit element (xterm/WS/DeckMux).
vi.mock("provide-uterm-frontend/session-element", () => ({ registerUtermSessionElement: vi.fn() }));

const deckMuxInstances = vi.hoisted(() => [] as Array<{
  enable: ReturnType<typeof vi.fn>;
  setOwnDimensions: ReturnType<typeof vi.fn>;
  handleMessage: ReturnType<typeof vi.fn>;
}>);
vi.mock("provide-uterm-frontend/deckmux", () => ({
  DeckMux: class {
    enable = vi.fn();
    setOwnDimensions = vi.fn();
    handleMessage = vi.fn();
    constructor() {
      deckMuxInstances.push(this);
    }
  },
}));

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
  deckMuxInstances.length = 0;
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

  it("creates DeckMux from presence sync and forwards lifecycle messages", () => {
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    render(<HijackHost sessionId="s1" surface="operator" />);
    instances[0]?.config?.onResize?.(100, 30);
    instances[0]?.config?.onPresenceMessage?.({
      type: "presence_sync",
      owner_id: "u1",
      config: { auto_transfer_idle_s: 12, keystroke_queue: "replay", ghost_box: false },
      users: [{ user_id: "u1", name: "Alice", color: "#f00" }],
    });

    const deckMux = deckMuxInstances[0];
    expect(deckMux?.enable).toHaveBeenCalledWith({ autoTransferIdleS: 12, keystrokeQueue: "replay", ghostBox: false });
    expect(deckMux?.setOwnDimensions).toHaveBeenCalledWith(100, 30);
    expect(deckMux?.handleMessage).toHaveBeenCalledWith({ type: "dm_owner_change", user_id: "u1" });

    instances[0]?.config?.onPresenceMessage?.({ type: "presence_update", user_id: "u1", name: "Alicia", cols: 101 });
    instances[0]?.config?.onPresenceMessage?.({ type: "control_transfer", to_user_id: "u1", from_user_id: "u2" });
    instances[0]?.config?.onPresenceMessage?.({ type: "presence_leave", user_id: "u2" });
    expect(deckMux?.handleMessage).toHaveBeenCalledWith(expect.objectContaining({ type: "dm_presence", user_id: "u1" }));
    expect(deckMux?.handleMessage).toHaveBeenCalledWith(
      expect.objectContaining({ type: "dm_control_transfer", to_name: "Alicia", from_user_id: "u2" }),
    );
    expect(deckMux?.handleMessage).toHaveBeenCalledWith(expect.objectContaining({ type: "dm_leave", user_id: "u2" }));
  });

  it("applies a roster that arrives after the startup sync", () => {
    // The server holds frames broadcast while a browser is still starting up
    // and flushes them once it is activated, so a second presence_sync now
    // lands moments after the browser's own. Its last user is whoever joined,
    // NOT this browser -- reassigning myUserId from it would make the client
    // believe it is someone else.
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    render(<HijackHost sessionId="s1" surface="operator" />);
    const presence = instances[0]?.config?.onPresenceMessage;

    presence?.({
      type: "presence_sync",
      config: {},
      users: [{ user_id: "me", name: "Me", color: "#f00" }],
    });
    presence?.({
      type: "presence_sync",
      config: {},
      users: [
        { user_id: "me", name: "Me", color: "#f00" },
        { user_id: "them", name: "Them", color: "#0f0" },
      ],
    });

    // One DeckMux, not one per sync.
    expect(deckMuxInstances).toHaveLength(1);
    const deckMux = deckMuxInstances[0];
    // Identity still taken from the browser's OWN sync.
    expect(deckMux?.handleMessage).toHaveBeenCalledWith({ type: "dm_hello", user_id: "me" });
    expect(deckMux?.handleMessage).not.toHaveBeenCalledWith({ type: "dm_hello", user_id: "them" });
    // The user who joined during the window is in the roster.
    expect(deckMux?.handleMessage).toHaveBeenCalledWith(
      expect.objectContaining({ type: "dm_join", user_id: "them" }),
    );

    // A buffered leave and handover apply against that roster, with the name
    // and colour learned from the sync that arrived with them.
    presence?.({ type: "control_transfer", to_user_id: "them", from_user_id: "me" });
    presence?.({ type: "presence_leave", user_id: "me" });

    expect(deckMux?.handleMessage).toHaveBeenCalledWith(
      expect.objectContaining({ type: "dm_control_transfer", to_user_id: "them", to_name: "Them", to_color: "#0f0" }),
    );
    expect(deckMux?.handleMessage).toHaveBeenCalledWith(expect.objectContaining({ type: "dm_leave", user_id: "me" }));
  });

  it("forwards terminal scroll, control requests, and keepalive dimensions", () => {
    vi.useFakeTimers();
    render(<HijackHost sessionId="s1" />);
    const widget = instances[0];
    widget?.config?.onResize?.(80, 24);
    widget?.dispatchEvent(new CustomEvent("uterm:scroll", { detail: { viewportY: 20, rows: 20, totalLines: 100 } }));
    widget?.dispatchEvent(new Event("deckmux:request_control"));
    widget?.dispatchEvent(new Event("deckmux:hand_off"));
    vi.advanceTimersByTime(15_000);

    expect(widget?.sendControlMessage).toHaveBeenCalledWith({
      type: "presence_update", scroll_line: 0.2, scroll_range: [0.2, 0.4],
    });
    expect(widget?.sendControlMessage).toHaveBeenCalledTimes(5);
    expect(widget?.sendControlMessage).toHaveBeenCalledWith({ type: "control_request" });
    vi.useRealTimers();
  });
});
