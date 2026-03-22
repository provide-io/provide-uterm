//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProvideHijack } from "./hijack.js";
import { encodeControlFrame, encodeDataFrame } from "./hijack-codec.js";

// ── WebSocket mock ────────────────────────────────────────────────────────────

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  readonly url: string;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    instances.push(this);
  }

  open(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  receive(data: string): void {
    this.onmessage?.({ data });
  }

  triggerError(): void {
    this.onerror?.();
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }

  send(data: string): void {
    this.sent.push(data);
  }
}

// Track all WS instances created per test
let instances: MockWebSocket[] = [];

// ── xterm mock ────────────────────────────────────────────────────────────────

class MockTerminal {
  written: string[] = [];
  opened = false;
  disposed = false;
  focused = false;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  addon: any = null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  _onData: ((data: string) => void) | null = null;
  open(_el: HTMLElement): void {
    this.opened = true;
  }
  focus(): void {
    this.focused = true;
  }
  write(s: string): void {
    this.written.push(s);
  }
  reset(): void {
    this.written = [];
  }
  dispose(): void {
    this.disposed = true;
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  loadAddon(a: any): void {
    this.addon = a;
  }
  onData(cb: (data: string) => void): { dispose(): void } {
    this._onData = cb;
    return { dispose: () => {} };
  }
  // Helper to simulate user typing
  simulateInput(data: string): void {
    this._onData?.(data);
  }
}

class MockFitAddon {
  fitCalled = 0;
  fit(): void {
    this.fitCalled++;
  }
}

// ── Test helpers ──────────────────────────────────────────────────────────────

function getWs(): MockWebSocket {
  const ws = instances[instances.length - 1];
  if (!ws) throw new Error("No WebSocket instance created");
  return ws;
}

function makeWidget(opts: Record<string, unknown> = {}): { widget: ProvideHijack; container: HTMLElement } {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const widget = new ProvideHijack(container, { workerId: "test-worker", ...opts });
  return { widget, container };
}

/** Query within container by ID suffix, e.g. q(container, "statustext") → finds [id$="-statustext"] */
function q(container: HTMLElement, name: string): HTMLElement | null {
  return container.querySelector(`[id$="-${name}"]`);
}

function sendMessage(msg: Record<string, unknown>): void {
  getWs().receive(encodeControlFrame(msg));
}

// ── Setup / teardown ──────────────────────────────────────────────────────────

beforeEach(() => {
  instances = [];
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", MockWebSocket);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).Terminal = MockTerminal;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).FitAddon = { FitAddon: MockFitAddon };
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  // Clean up DOM
  document.body.innerHTML = "";
});

// ── Construction ──────────────────────────────────────────────────────────────

describe("ProvideHijack construction", () => {
  it("creates a WebSocket on construction", () => {
    makeWidget();
    expect(instances).toHaveLength(1);
    expect(getWs().url).toContain("test-worker");
  });

  it("mounts DOM into the container", () => {
    const { container } = makeWidget();
    expect(container.querySelector(".provide-hijack")).toBeTruthy();
  });

  it("renders analysis panel when showAnalysis=true (default)", () => {
    const { container } = makeWidget();
    expect(container.querySelector(".hijack-analysis")).toBeTruthy();
  });

  it("omits analysis panel when showAnalysis=false", () => {
    const { container } = makeWidget({ showAnalysis: false });
    expect(container.querySelector(".hijack-analysis")).toBeFalsy();
  });

  it("defaults workerId to 'default' if not provided", () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    new ProvideHijack(container);
    expect(getWs().url).toContain("default");
  });

  it("uses absolute wsUrl as-is when provided", () => {
    makeWidget({ wsUrl: "ws://custom.host/path" });
    expect(getWs().url).toBe("ws://custom.host/path");
  });

  it("prepends protocol to relative wsUrl", () => {
    makeWidget({ wsUrl: "/ws/browser/myworker/term" });
    expect(getWs().url).toContain("/ws/browser/myworker/term");
  });

  it("uses wss: when location.protocol is https:", () => {
    Object.defineProperty(window, "location", {
      value: { protocol: "https:", host: "secure.example.com" },
      writable: true,
    });
    makeWidget();
    expect(getWs().url).toMatch(/^wss:/);
    Object.defineProperty(window, "location", {
      value: { protocol: "http:", host: "localhost" },
      writable: true,
    });
  });
});

// ── Connection lifecycle ──────────────────────────────────────────────────────

describe("WebSocket lifecycle", () => {
  it("sends snapshot_req on open", () => {
    const { widget: _ } = makeWidget();
    getWs().open();
    const frame = getWs().sent.find((s) => s.includes('"snapshot_req"'));
    expect(frame).toBeTruthy();
  });

  it("resets state on close", () => {
    const { container } = makeWidget();
    getWs().open();
    getWs().close();
    // Status dot should indicate disconnected
    expect(q(container, "statustext")?.textContent).toBe("Reconnecting in 1s…");
  });

  it("schedules reconnect after close", () => {
    makeWidget();
    getWs().close();
    expect(instances).toHaveLength(1); // still only 1
    vi.advanceTimersByTime(1100);
    expect(instances).toHaveLength(2); // reconnected
  });

  it("does not double-schedule reconnect", () => {
    makeWidget();
    getWs().close();
    getWs().close(); // second close should be a no-op (stale handler guard)
    vi.advanceTimersByTime(1100);
    // Would have been called twice if not guarded, creating 3 total — ensure only 2
    expect(instances.length).toBeLessThanOrEqual(2);
  });

  it("closes existing WS before reconnecting", () => {
    makeWidget();
    const first = getWs();
    first.open();
    first.close();
    vi.advanceTimersByTime(1100);
    expect(instances).toHaveLength(2);
    expect(first.readyState).toBe(MockWebSocket.CLOSED);
  });

  it("onerror triggers close", () => {
    makeWidget();
    const ws = getWs();
    ws.open();
    ws.triggerError();
    expect(ws.readyState).toBe(MockWebSocket.CLOSED);
  });

  it("sets status to bad on WebSocket constructor error", () => {
    vi.stubGlobal("WebSocket", () => {
      throw new Error("connection refused");
    });
    const { container } = makeWidget();
    expect(q(container, "statustext")?.textContent).toContain("Failed");
  });

  it("sends resume token if stored in sessionStorage", () => {
    vi.stubGlobal("sessionStorage", {
      getItem: vi.fn().mockReturnValue("stored-token"),
      setItem: vi.fn(),
    });
    makeWidget();
    getWs().open();
    const resumeFrame = getWs().sent.find((s) => s.includes("resume"));
    expect(resumeFrame).toBeTruthy();
  });

  it("saves resume token received in hello message", () => {
    const setItem = vi.fn();
    vi.stubGlobal("sessionStorage", { getItem: vi.fn().mockReturnValue(null), setItem });
    makeWidget();
    getWs().open();
    sendMessage({ type: "hello", resume_token: "new-token", worker_online: true });
    expect(setItem).toHaveBeenCalledWith("uterm_resume_test-worker", "new-token");
  });

  it("handles sessionStorage errors gracefully", () => {
    vi.stubGlobal("sessionStorage", {
      getItem: vi.fn().mockImplementation(() => {
        throw new Error("storage disabled");
      }),
      setItem: vi.fn().mockImplementation(() => {
        throw new Error("storage disabled");
      }),
    });
    makeWidget();
    // Should not throw
    getWs().open();
    sendMessage({ type: "hello", resume_token: "tok", worker_online: true });
  });
});

// ── disconnect / dispose ──────────────────────────────────────────────────────

describe("disconnect and dispose", () => {
  it("disconnect closes WS", () => {
    const { widget } = makeWidget();
    getWs().open();
    widget.disconnect();
    expect(getWs().readyState).toBe(MockWebSocket.CLOSED);
  });

  it("disconnect cancels reconnect timer", () => {
    const { widget } = makeWidget();
    getWs().close(); // schedules reconnect
    widget.disconnect();
    vi.advanceTimersByTime(2000);
    expect(instances).toHaveLength(1); // no reconnect happened
  });

  it("dispose removes DOM", () => {
    const { widget, container } = makeWidget();
    widget.dispose();
    expect(container.querySelector(".provide-hijack")).toBeFalsy();
  });

  it("dispose disposes xterm terminal", () => {
    const { widget } = makeWidget();
    getWs().open();
    // Trigger term creation via a term message
    sendMessage({ type: "term", data: "hi" });
    widget.dispose();
    // Terminal is disposed (no throws)
  });
});

// ── Message dispatch ──────────────────────────────────────────────────────────

describe("message dispatch", () => {
  it("term message writes to terminal", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "term", data: "output text" });
    // No throw = success; xterm mock records writes
  });

  it("snapshot message resets and writes screen", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "snapshot", screen: "hello\nworld" });
    // Just verify no throw
  });

  it("snapshot message sets prompt id", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "snapshot", screen: "", prompt_detected: { prompt_id: "p42" } });
    expect(q(container, "prompt")?.textContent).toBe("prompt: p42");
  });

  it("snapshot message with no prompt clears prompt display", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "snapshot", screen: "" });
    expect(q(container, "prompt")?.textContent).toBe("");
  });

  it("analysis message sets pre textContent", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "snapshot", screen: "" }); // creates term
    sendMessage({ type: "analysis", formatted: "line 1\nline 2" });
    expect(q(container, "analysistext")?.textContent).toBe("line 1\nline 2");
  });

  it("hello message updates state flags", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({
      type: "hello",
      can_hijack: true,
      hijacked: false,
      hijacked_by_me: false,
      worker_online: true,
      input_mode: "open",
    });
    expect(q(container, "statustext")?.textContent).toBe("Connected (shared)");
  });

  it("hello message with capabilities fallback for hijack_control", () => {
    makeWidget();
    getWs().open();
    // Should not throw; capabilities field is read
    sendMessage({ type: "hello", capabilities: { hijack_control: "rest", hijack_step_supported: false } });
  });

  it("hello message with resume_supported=false", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "hello", resume_supported: false });
  });

  it("worker_connected sets workerOnline", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "worker_connected" });
    // workerOnline=true, connected=true → "Connected (watching)"
    expect(q(container, "statustext")?.textContent).toBe("Connected (watching)");
  });

  it("hijack_state owner=me starts heartbeat", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me", input_mode: "hijack" });
    // heartbeat should be running — advance past interval
    const sentBefore = getWs().sent.length;
    vi.advanceTimersByTime(5100);
    expect(getWs().sent.length).toBeGreaterThan(sentBefore);
  });

  it("hijack_state owner=other clears heartbeat", () => {
    makeWidget();
    getWs().open();
    // First acquire
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Then someone else takes it
    sendMessage({ type: "hijack_state", hijacked: true, owner: "other" });
    const sentBefore = getWs().sent.length;
    vi.advanceTimersByTime(5100);
    // No heartbeat sent (cleared)
    expect(getWs().sent.length).toBe(sentBefore);
  });

  it("hijack_state with input_mode updates status", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true, can_hijack: false });
    sendMessage({ type: "hijack_state", hijacked: false, owner: null, input_mode: "open" });
    expect(q(container, "statustext")?.textContent).toBe("Connected (shared)");
  });

  it("worker_disconnected resets online state", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    sendMessage({ type: "worker_disconnected" });
    expect(q(container, "statustext")?.textContent).toBe("Worker offline");
  });

  it("input_mode_changed updates status", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true, can_hijack: false });
    sendMessage({ type: "input_mode_changed", input_mode: "open" });
    expect(q(container, "statustext")?.textContent).toBe("Connected (shared)");
  });

  it("heartbeat_ack is a no-op", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "heartbeat_ack" });
    expect(q(container, "statustext")?.textContent).toBe("Connected (watching)");
  });

  it("error message sets bad status", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "error", message: "access denied" });
    expect(q(container, "statustext")?.textContent).toBe("Error: access denied");
  });

  it("error message with no message uses fallback", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "error" });
    expect(q(container, "statustext")?.textContent).toBe("Error: unknown");
  });

  it("protocol error on bad message closes WS and schedules reconnect", () => {
    makeWidget();
    getWs().open();
    // Corrupt frame triggers _setStatus("bad","Protocol error") then ws.close()
    // ws.close() fires onclose → _scheduleReconnect() → "Reconnecting in Ns…"
    getWs().receive("\x10X"); // invalid control prefix
    expect(getWs().readyState).toBe(MockWebSocket.CLOSED);
  });

  it("data frame becomes term message", () => {
    makeWidget();
    getWs().open();
    getWs().receive(encodeDataFrame("raw output"));
    // No throw = term message handled
  });
});

// ── Heartbeat ─────────────────────────────────────────────────────────────────

describe("heartbeat", () => {
  it("sends heartbeat to WS when hijackedByMe", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const sentBefore = getWs().sent.length;
    vi.advanceTimersByTime(5100);
    const newFrames = getWs().sent.slice(sentBefore);
    expect(newFrames.some((f) => f.includes("heartbeat"))).toBe(true);
  });

  it("skips heartbeat when not hijackedByMe", () => {
    makeWidget();
    getWs().open();
    const sentBefore = getWs().sent.length;
    vi.advanceTimersByTime(5100);
    expect(getWs().sent.length).toBe(sentBefore);
  });

  it("skips WS heartbeat in rest mode (no WS frames for heartbeat)", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "hello", hijack_control: "rest" });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const sentBefore = getWs().sent.length;
    vi.advanceTimersByTime(5100);
    // No heartbeat WS frame sent — rest mode skips WS and calls fetch
    // (fetch returns null because _restHijackId is null, but no WS frames sent)
    expect(getWs().sent.length).toBe(sentBefore);
  });
});

// ── Button clicks ─────────────────────────────────────────────────────────────

describe("button clicks", () => {
  it("hijack button sends hijack_request via WS", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    (q(container, "hijack") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("hijack_request"))).toBe(true);
  });

  it("hijack button is no-op when WS not open", () => {
    const { container } = makeWidget();
    (q(container, "hijack") as HTMLButtonElement).click();
    // Sent nothing
    expect(getWs().sent).toHaveLength(0);
  });

  it("hijack button calls REST acquire when hijack_control=rest", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ hijack_id: "hid-1" }) }),
    );
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, hijack_control: "rest", worker_online: true });
    (q(container, "hijack") as HTMLButtonElement).click();
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(expect.stringContaining("acquire"), expect.anything());
  });

  it("step button is no-op when not hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    (q(container, "step") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("hijack_step"))).toBe(false);
  });

  it("step button sends hijack_step when hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "step") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("hijack_step"))).toBe(true);
  });

  it("release button sends hijack_release", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "release") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("hijack_release"))).toBe(true);
  });

  it("release button calls REST acquire then release when hijack_control=rest", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ hijack_id: "hid-99" }) }),
    );
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, hijack_control: "rest", worker_online: true });
    // Acquire first to set _restHijackId
    (q(container, "hijack") as HTMLButtonElement).click();
    // Flush Promise microtasks so _restHijack's async chain settles
    for (let i = 0; i < 5; i++) await Promise.resolve();
    // Now release
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "release") as HTMLButtonElement).click();
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(expect.stringContaining("release"), expect.anything());
  });

  it("resync button sends snapshot_req", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    (q(container, "resync") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("snapshot_req"))).toBe(true);
  });

  it("analyze button sends analyze_req when hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "analyze") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("analyze_req"))).toBe(true);
  });

  it("analyze button is no-op when not hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    (q(container, "analyze") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f.includes("analyze_req"))).toBe(false);
  });

  it("kbd toggle button toggles mobile keys visibility", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const toggleBtn = q(container, "kbdtoggle") as HTMLButtonElement;
    toggleBtn.click(); // show
    const mkRow = q(container, "mobilekeys");
    // visibility depends on connected+canInput+mobileKeysVisible — just check no throw
    expect(mkRow).toBeTruthy();
  });

  it("mobile key buttons send input when hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const escBtn = Array.from(container.querySelectorAll(".mkey")).find(
      (b) => b.textContent === "ESC",
    ) as HTMLButtonElement;
    escBtn.click();
    // ESC is sent as a raw data frame (encodeDataFrame("\x1b") = "\x1b"), not JSON
    expect(getWs().sent.some((f) => f.includes("\x1b"))).toBe(true);
  });

  it("mobile key buttons are no-op when not hijackedByMe and not open mode", () => {
    const { container } = makeWidget();
    getWs().open();
    const escBtn = Array.from(container.querySelectorAll(".mkey")).find(
      (b) => b.textContent === "ESC",
    ) as HTMLButtonElement;
    const sentBefore = getWs().sent.length;
    escBtn.click();
    expect(getWs().sent.length).toBe(sentBefore);
  });
});

// ── Text input ────────────────────────────────────────────────────────────────

describe("text input field", () => {
  it("sends input on Enter key when hijackedByMe", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "hello";
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(getWs().sent.some((f) => f === "hello")).toBe(true);
    expect(field.value).toBe(""); // cleared after send
  });

  it("does not send on Enter when field is empty", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "";
    const sentBefore = getWs().sent.length;
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(getWs().sent.length).toBe(sentBefore);
  });

  it("unescapes \\r \\n \\t \\e in input", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "\\r\\n\\t\\e";
    const sendBtn = q(container, "inputsend") as HTMLButtonElement;
    sendBtn.click();
    expect(getWs().sent.some((f) => f === "\r\n\t\x1b")).toBe(true);
  });

  it("send button sends input", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "test";
    (q(container, "inputsend") as HTMLButtonElement).click();
    expect(getWs().sent.some((f) => f === "test")).toBe(true);
  });

  it("does not send when not hijackedByMe and not open mode", () => {
    const { container } = makeWidget();
    getWs().open();
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "blocked";
    const sentBefore = getWs().sent.length;
    (q(container, "inputsend") as HTMLButtonElement).click();
    expect(getWs().sent.length).toBe(sentBefore);
  });
});

// ── Reconnect / nudge ─────────────────────────────────────────────────────────

describe("reconnect logic", () => {
  it("backoff delay increases with each attempt", () => {
    makeWidget();
    // First close → 1s delay
    getWs().close();
    vi.advanceTimersByTime(1100);
    // Second close → 2s delay
    getWs().close();
    const instancesBefore = instances.length;
    vi.advanceTimersByTime(1100);
    expect(instances.length).toBe(instancesBefore); // not yet reconnected (2s delay)
    vi.advanceTimersByTime(1000);
    expect(instances.length).toBe(instancesBefore + 1);
  });

  it("nudge reconnect cancels pending timer and reconnects immediately", () => {
    const { widget } = makeWidget();
    getWs().close(); // schedules 1s reconnect
    // Simulate nudge via typing while disconnected — need to call connect directly
    widget.connect(); // calls _connectWs which clears the WS and creates a new one
    expect(instances).toHaveLength(2);
  });
});

// ── mobileKeys=false ──────────────────────────────────────────────────────────

describe("mobileKeys=false option", () => {
  it("does not build mobile keys", () => {
    const { container } = makeWidget({ mobileKeys: false });
    expect(container.querySelectorAll(".mkey")).toHaveLength(0);
  });
});
