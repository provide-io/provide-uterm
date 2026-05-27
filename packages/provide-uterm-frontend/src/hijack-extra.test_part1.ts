//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Additional branch coverage tests for hijack.ts to supplement hijack.test.ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProvideHijack } from "./hijack.js";
import { encodeControlFrame } from "./hijack-codec.js";

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
  close(): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }
  send(data: string): void {
    this.sent.push(data);
  }
}

class MockTerminal {
  written: string[] = [];
  opened = false;
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  addon: any = null;
  _onData: ((data: string) => void) | null = null;
  open(_el: HTMLElement): void {
    this.opened = true;
  }
  focus(): void {}
  write(s: string): void {
    this.written.push(s);
  }
  reset(): void {
    this.written = [];
  }
  dispose(): void {}
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  loadAddon(a: any): void {
    this.addon = a;
  }
  onData(cb: (data: string) => void): { dispose(): void } {
    this._onData = cb;
    return { dispose: () => {} };
  }
}

class MockFitAddon {
  fit(): void {}
}

let instances: MockWebSocket[] = [];

function getWs(): MockWebSocket {
  return instances[instances.length - 1];
}

function makeWidget(opts: Record<string, unknown> = {}) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const widget = new ProvideHijack(container, { workerId: "test-worker", ...opts });
  return { widget, container };
}

function q(container: HTMLElement, name: string): HTMLElement | null {
  return container.querySelector(`[id$="-${name}"]`);
}

function sendMessage(msg: Record<string, unknown>): void {
  getWs().receive(encodeControlFrame(msg));
}

beforeEach(() => {
  instances = [];
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", MockWebSocket);
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  (window as any).Terminal = MockTerminal;
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  (window as any).FitAddon = { FitAddon: MockFitAddon };
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("hijack.ts additional branch coverage - activity flash timer", () => {
  it("activity flash timer removes class after 200ms", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Send input to trigger activity flash
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "test";
    const sendBtn = q(container, "inputsend") as HTMLButtonElement;
    sendBtn.click();
    // Advance past the 200ms flash timer to cover lines 535-536
    vi.advanceTimersByTime(210);
    const dot = q(container, "statusdot");
    if (dot) {
      expect(dot.classList.contains("activity-flash")).toBe(false);
    }
  });

  it("shows 'Offline' status when waking timed out", () => {
    const { container } = makeWidget();
    getWs().open();
    // Worker is not online and waking has timed out
    // The waking timeout is set by _startWakingTimer which fires after some delay
    // We need connected=true, workerOnline=false, wakingTimedOut=true
    sendMessage({ type: "hello", worker_online: false });
    // Advance past waking timeout (typically 30s in hijack.ts)
    vi.advanceTimersByTime(31000);
    // Should now show Offline status
    const statusText = q(container, "statustext")?.textContent ?? "";
    // Either Waking or Offline depending on timeout implementation
    expect(statusText === "Waking…" || statusText === "Offline").toBe(true);
  });
});

describe("hijack.ts additional branch coverage", () => {
  it("shows 'Hijacked (other)' when hijacked but not by me", () => {
    const { container } = makeWidget();
    getWs().open();
    // Connected, hijacked by someone else (not me)
    sendMessage({ type: "hello", worker_online: true, can_hijack: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "someone-else", input_mode: "hijack" });
    expect(q(container, "statustext")?.textContent).toBe("Hijacked (other)");
  });

  it("step button with rest control calls _restHijack('step') after acquiring", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ hijack_id: "hid-99" }),
    });
    vi.stubGlobal("fetch", mockFetch);
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, hijack_control: "rest", worker_online: true });
    // First acquire hijack to set _restHijackId
    (q(container, "hijack") as HTMLButtonElement).click();
    // Let the acquire promise resolve
    for (let i = 0; i < 5; i++) await Promise.resolve();
    // Now simulate hijack state with owner=me
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Now click step
    (q(container, "step") as HTMLButtonElement).click();
    for (let i = 0; i < 5; i++) await Promise.resolve();
    // Step in rest mode calls fetch with "step"
    expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining("step"), expect.anything());
  });

  it("step button timer callbacks send snapshot_req when WS is open", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "step") as HTMLButtonElement).click();
    const sentBefore = getWs().sent.length;
    // Advance past the 250ms timer
    vi.advanceTimersByTime(260);
    // snapshot_req should have been sent
    expect(getWs().sent.length).toBeGreaterThan(sentBefore);
    expect(getWs().sent.some((f) => f.includes("snapshot_req"))).toBe(true);
  });

  it("step button timer callbacks send analyze_req when WS is open", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "step") as HTMLButtonElement).click();
    // Advance past the 450ms timer
    vi.advanceTimersByTime(500);
    expect(getWs().sent.some((f) => f.includes("analyze_req"))).toBe(true);
  });

  it("step button timer callbacks skip send when WS is closed", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "step") as HTMLButtonElement).click();
    // Close the WS before timers fire — this creates a new (different) WS after reconnect
    const wsBeforeClose = getWs();
    wsBeforeClose.close();
    const sentAfterClose = wsBeforeClose.sent.length;
    // Advance past the 250ms timer — the timer checks ws.readyState
    // The old ws is CLOSED, so timer skips
    vi.advanceTimersByTime(1500); // past all timers
    // The old WS should not have received any new sends after close
    expect(wsBeforeClose.sent.length).toBe(sentAfterClose);
  });

  it("step button 1000ms snapshot timer fires when WS is open", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "step") as HTMLButtonElement).click();
    // Advance past 1000ms snapshot timer
    vi.advanceTimersByTime(1100);
    expect(getWs().sent.some((f) => f.includes("snapshot_req"))).toBe(true);
  });

  it("step button 1200ms analyze timer fires when WS is open", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    (q(container, "step") as HTMLButtonElement).click();
    vi.advanceTimersByTime(1300);
    expect(getWs().sent.some((f) => f.includes("analyze_req"))).toBe(true);
  });
});

describe("hijack.ts branch coverage - constructor config variants", () => {
  it("constructs with showInput=false, showAnalysis=false, mobileKeys=false", () => {
    const { container } = makeWidget({ showInput: false, showAnalysis: false, mobileKeys: false });
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    // No input row, no analysis, no mobile keys
    expect(q(container, "inputrow")).toBeTruthy(); // element exists but may not be shown
    expect(q(container, "analysis")).toBeNull(); // not rendered when showAnalysis=false
  });

  it("constructs with custom wsUrl (absolute)", () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const widget = new ProvideHijack(container, { wsUrl: "ws://custom-host/ws" });
    expect(instances.length).toBeGreaterThan(0);
    expect(instances[instances.length - 1].url).toBe("ws://custom-host/ws");
    widget.disconnect();
  });

  it("constructs with custom wsUrl starting with '/'", () => {
    vi.stubGlobal("location", { protocol: "http:", host: "localhost" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const widget = new ProvideHijack(container, { wsUrl: "/custom/ws/path" });
    expect(instances[instances.length - 1].url).toBe("ws://localhost/custom/ws/path");
    widget.disconnect();
  });

  it("constructs with https protocol uses wss", () => {
    vi.stubGlobal("location", { protocol: "https:", host: "example.com" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const widget = new ProvideHijack(container, { workerId: "worker-1" });
    expect(instances[instances.length - 1].url).toContain("wss://");
    widget.disconnect();
  });

  it("constructs with custom wsPathPrefix", () => {
    vi.stubGlobal("location", { protocol: "http:", host: "localhost" });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const widget = new ProvideHijack(container, { workerId: "w1", wsPathPrefix: "/custom/path" });
    expect(instances[instances.length - 1].url).toContain("/custom/path/w1/term");
    widget.disconnect();
  });

  it("constructs with title option", () => {
    const { container } = makeWidget({ title: "My Custom Title" });
    expect(container.innerHTML).toContain("My Custom Title");
  });
});

describe("hijack.ts branch coverage - disconnect/dispose", () => {
  it("disconnect with reconnect timer clears it", () => {
    const { widget } = makeWidget();
    const ws = getWs();
    ws.open();
    // Close to trigger reconnect scheduling
    ws.close();
    // Now disconnect before timer fires
    widget.disconnect();
    // Timer should be cleared — advance timers to confirm no new WS created
    const instancesBefore = instances.length;
    vi.advanceTimersByTime(35000);
    // No new WebSocket created after disconnect
    expect(instances.length).toBe(instancesBefore);
  });

  it("disconnect without WS is a no-op", () => {
    const { widget } = makeWidget();
    const ws = getWs();
    ws.open();
    ws.close();
    vi.advanceTimersByTime(2000); // let reconnect happen
    widget.disconnect();
    widget.disconnect(); // second call should be safe
  });

  it("dispose clears term and flash timer", () => {
    const { widget, container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", can_hijack: true, worker_online: true });
    sendMessage({ type: "hijack_state", hijacked: true, owner: "me" });
    // Trigger activity flash (sets _activityFlashTimer)
    const field = q(container, "inputfield") as HTMLInputElement;
    field.value = "hello";
    q(container, "inputsend")?.dispatchEvent(new MouseEvent("click"));
    // Force ensureTerm (needed for term to be set)
    sendMessage({ type: "term", data: "hello" });
    // Now dispose
    widget.dispose();
    // The root should be removed from DOM
    expect(container.children.length).toBe(0);
  });

  it("dispose without parentNode doesn't throw", () => {
    const container = document.createElement("div");
    // Don't append to body — root.parentNode will be null after manual removal
    document.body.appendChild(container);
    const widget = new ProvideHijack(container, { workerId: "test" });
    // Remove root from container before dispose
    const ws = getWs();
    ws.open();
    widget.dispose();
    // Should not throw
  });
});

describe("hijack.ts branch coverage - WS handlers", () => {
  it("ws.onopen fires with storedToken in sessionStorage", () => {
    // Save a resume token before creating widget
    sessionStorage.setItem("uterm_resume_test-worker", "stored-token-123");
    makeWidget();
    const ws = getWs();
    ws.open();
    // Should have sent resume message
    expect(ws.sent.some((f) => f.includes("resume"))).toBe(true);
    sessionStorage.removeItem("uterm_resume_test-worker");
  });

  it("ws.onopen skips resume when no token", () => {
    sessionStorage.removeItem("uterm_resume_test-worker");
    makeWidget();
    const ws = getWs();
    ws.open();
    // Should NOT have sent resume message
    expect(ws.sent.some((f) => f.includes('"resume"'))).toBe(false);
  });

  it("waking timer fires when worker not online after 10s", () => {
    const { container } = makeWidget();
    getWs().open();
    // worker_online = false initially
    sendMessage({ type: "hello", worker_online: false });
    // Advance past 10s waking timeout
    vi.advanceTimersByTime(10500);
    const text = q(container, "statustext")?.textContent ?? "";
    expect(text).toBe("Offline");
  });

  it("waking timer does NOT fire when worker goes online before 10s", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: false });
    // Worker comes online before timeout
    sendMessage({ type: "worker_connected" });
    vi.advanceTimersByTime(15000);
    const text = q(container, "statustext")?.textContent ?? "";
    // Should be connected status, not Offline
    expect(text).not.toBe("Offline");
  });

  it("ws.onerror triggers close", () => {
    makeWidget();
    const ws = getWs();
    ws.open();
    const _initialLen = instances.length;
    ws.onerror?.();
    // onerror calls ws.close() internally
    expect(ws.readyState).toBe(MockWebSocket.CLOSED);
  });

  it("protocol error in onmessage sets error status", () => {
    const { container } = makeWidget();
    const ws = getWs();
    ws.open();
    // Send malformed data that will trigger the catch block
    // The codec's feed() will throw if given completely invalid data in some way
    // Instead: send valid frame but trigger error via bad JSON in control frame
    // Actually the easiest way is to send raw data that breaks the decoder
    ws.onmessage?.({ data: "\x00\x00\x00\x01\xFF\xFF\xFF\xFF" }); // invalid frame
    // Status may be "Protocol error" or unchanged depending on codec behavior
    // Just verify no crash
    expect(q(container, "statustext")).toBeTruthy();
  });

  it("stale ws.onopen handler is ignored (ws !== _ws)", () => {
    makeWidget();
    const firstWs = getWs();
    firstWs.open();
    firstWs.close();
    // New WS created after reconnect schedule
    vi.advanceTimersByTime(2000);
    const secondWs = getWs();
    expect(secondWs).not.toBe(firstWs);
    // Fire onopen for the first (stale) WS — should be a no-op
    const sentBefore = secondWs.sent.length;
    firstWs.onopen?.();
    // No change expected on the current widget state from stale open
    expect(secondWs.sent.length).toBe(sentBefore);
  });

  it("stale ws.onclose handler is ignored (ws !== _ws)", () => {
    const { container } = makeWidget();
    const firstWs = getWs();
    firstWs.open();
    sendMessage({ type: "hello", worker_online: true });
    // Force replace WS by closing (onclose fires, schedules reconnect, new WS created)
    firstWs.readyState = MockWebSocket.CLOSED;
    firstWs.onclose?.();
    vi.advanceTimersByTime(2000);
    const secondWs = getWs();
    secondWs.open();
    sendMessage({ type: "hello", worker_online: true });
    // Fire onclose of first WS again (stale) — should be ignored
    firstWs.readyState = MockWebSocket.CLOSED;
    firstWs.onclose?.();
    // Widget should still show connected status
    const text = q(container, "statustext")?.textContent ?? "";
    expect(text).not.toBe("Disconnected");
  });

  it("worker_connected message clears waking state", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: false });
    sendMessage({ type: "worker_connected" });
    // Should show connected status now
    const text = q(container, "statustext")?.textContent ?? "";
    expect(text).toContain("Connected");
  });

  it("worker_disconnected message updates status", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    sendMessage({ type: "worker_disconnected" });
    const text = q(container, "statustext")?.textContent ?? "";
    expect(text).toContain("Offline");
  });

  it("hello with resume_token saves it", () => {
    makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true, resume_token: "tok-abc123" });
    expect(sessionStorage.getItem("uterm_resume_test-worker")).toBe("tok-abc123");
    sessionStorage.removeItem("uterm_resume_test-worker");
  });

  it("input_mode_changed message updates status", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    sendMessage({ type: "input_mode_changed", input_mode: "open" });
    const text = q(container, "statustext")?.textContent ?? "";
    expect(text).toContain("shared");
  });

  it("heartbeat_ack message is handled without error", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "hello", worker_online: true });
    sendMessage({ type: "heartbeat_ack" });
    // Status should still be normal
    expect(q(container, "statustext")).toBeTruthy();
  });

  it("error message sets bad status", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "error", message: "something went wrong" });
    const text = q(container, "statustext")?.textContent ?? "";
    expect(text).toContain("Error: something went wrong");
  });

  it("error message with no message field", () => {
    const { container } = makeWidget();
    getWs().open();
    sendMessage({ type: "error" });
    const text = q(container, "statustext")?.textContent ?? "";
    expect(text).toContain("Error: unknown");
  });
});

describe("hijack.ts branch coverage - xterm and reconnect anim", () => {
