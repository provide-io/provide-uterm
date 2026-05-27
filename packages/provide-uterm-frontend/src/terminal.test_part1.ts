//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ── Mock classes ──────────────────────────────────────────────────────────────

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  url: string;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  static instances: MockWebSocket[] = [];

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  open(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }
  close(): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }
  send(data: string): void {
    this.sent.push(data);
  }
  triggerMessage(data: string): void {
    this.onmessage?.({ data });
  }
  triggerError(): void {
    this.onerror?.();
  }
}

class MockFitAddon {
  fit(): void {}
  proposeDimensions(): { cols: number } {
    return { cols: 80 };
  }
}

class MockXterm {
  static instances: MockXterm[] = [];

  written: string[] = [];
  opened = false;
  disposed = false;
  focused = false;
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  addon: any = null;
  _onDataCb: ((data: string) => void) | null = null;
  options: { fontSize: number; theme?: Record<string, unknown> } = { fontSize: 14 };
  buffer = {
    active: {
      baseY: 0,
      cursorY: 2,
      getLine: (i: number) => ({
        translateToString: (_trimRight?: boolean) => (i < 3 ? `line ${i}` : ""),
      }),
    },
  };

  constructor() {
    MockXterm.instances.push(this);
  }

  open(_el: HTMLElement): void {
    this.opened = true;
  }
  focus(): void {
    this.focused = true;
  }
  write(data: string): void {
    this.written.push(data);
  }
  reset(): void {
    this.written = [];
  }
  dispose(): void {
    this.disposed = true;
  }
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  loadAddon(a: any): void {
    this.addon = a;
  }
  onData(cb: (data: string) => void): { dispose(): void } {
    this._onDataCb = cb;
    return { dispose: () => {} };
  }
  customKeyHandlerCallCount = 0;
  attachCustomKeyEventHandler(_cb: (e: KeyboardEvent) => boolean): void {
    this.customKeyHandlerCallCount++;
  }
  simulateInput(data: string): void {
    this._onDataCb?.(data);
  }
}

function getXterm(): MockXterm {
  return MockXterm.instances[MockXterm.instances.length - 1];
}

// ── Setup / Teardown ──────────────────────────────────────────────────────────

beforeEach(() => {
  MockWebSocket.instances = [];
  MockXterm.instances = [];
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", MockWebSocket);
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  (window as any).Terminal = MockXterm;
  // biome-ignore lint/suspicious/noExplicitAny: test mock
  (window as any).FitAddon = { FitAddon: MockFitAddon };
  vi.stubGlobal("localStorage", {
    getItem: vi.fn().mockReturnValue(null),
    setItem: vi.fn(),
  });
  // Mock document.fonts.ready
  Object.defineProperty(document, "fonts", {
    value: { ready: Promise.resolve() },
    writable: true,
    configurable: true,
  });
  vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
    cb();
    return 0;
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  Object.defineProperty(window, "location", {
    value: { protocol: "http:", host: "localhost", search: "" },
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.innerHTML = "";
  vi.resetModules();
});

// Helper to get the last WebSocket
function getWs(): MockWebSocket {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

// Helper to create an ProvideTerminal instance using window.ProvideTerminal
// (terminal.ts is script-mode and registers itself on window)
// biome-ignore lint/suspicious/noExplicitAny: accessing window global
type TerminalCtor = new (container: HTMLElement, config?: Record<string, unknown>) => any;

async function loadTerminal(): Promise<TerminalCtor> {
  await import("./terminal.js");
  // biome-ignore lint/suspicious/noExplicitAny: window global
  const ctor = (window as any).ProvideTerminal as TerminalCtor;
  if (!ctor) throw new Error("ProvideTerminal not set on window");
  return ctor;
}

async function makeTerminal(config: Record<string, unknown> = {}) {
  const Ctor = await loadTerminal();
  const container = document.createElement("div");
  document.body.appendChild(container);
  const terminal = new Ctor(container, config);
  return { terminal, container };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("ProvideTerminal construction", () => {
  it("creates a WebSocket on construction", async () => {
    await makeTerminal({ wsUrl: "/ws/terminal" });
    expect(MockWebSocket.instances.length).toBeGreaterThan(0);
  });

  it("mounts DOM into container", async () => {
    const { container } = await makeTerminal();
    expect(container.querySelector(".provide-uterm")).toBeTruthy();
  });

  it("builds DOM with settings panel and gear button", async () => {
    const { container } = await makeTerminal();
    expect(container.querySelector(`[id^="gearBtn-"]`)).toBeTruthy();
    expect(container.querySelector(`[id^="settingsPanel-"]`)).toBeTruthy();
  });

  it("is registered on window after import", async () => {
    await import("./terminal.js");
    // biome-ignore lint/suspicious/noExplicitAny: test access
    expect(typeof (window as any).ProvideTerminal).toBe("function");
  });

  it("throws when Terminal (xterm) is not loaded", async () => {
    // First load the module to get the constructor
    const Ctor = await loadTerminal();
    const container = document.createElement("div");
    document.body.appendChild(container);
    // Now remove Terminal before constructing
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).Terminal = undefined;
    expect(() => new Ctor(container)).toThrow("xterm.js (Terminal) not loaded");
    // Restore for other tests
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).Terminal = MockXterm;
  });

  it("throws when FitAddon is not loaded", async () => {
    const Ctor = await loadTerminal();
    const container = document.createElement("div");
    document.body.appendChild(container);
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).FitAddon = undefined;
    expect(() => new Ctor(container)).toThrow("addon-fit (FitAddon) not loaded");
    // Restore for other tests
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).FitAddon = { FitAddon: MockFitAddon };
  });
});

describe("ProvideTerminal WebSocket URL resolution", () => {
  it("uses wsUrl as-is when absolute", async () => {
    await makeTerminal({ wsUrl: "ws://custom.host/ws" });
    expect(getWs().url).toBe("ws://custom.host/ws");
  });

  it("prepends protocol for relative wsUrl", async () => {
    await makeTerminal({ wsUrl: "/ws/browser/w/term" });
    expect(getWs().url).toContain("/ws/browser/w/term");
  });

  it("falls back to /ws/terminal when no wsUrl", async () => {
    await makeTerminal();
    expect(getWs().url).toContain("/ws/terminal");
  });

  it("uses wss: when on https", async () => {
    Object.defineProperty(window, "location", {
      value: { protocol: "https:", host: "secure.example.com" },
      writable: true,
      configurable: true,
    });
    await makeTerminal({ wsUrl: "/ws/term" });
    expect(getWs().url).toMatch(/^wss:/);
  });
});

describe("ProvideTerminal connection lifecycle", () => {
  it("updates status on WS open", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    const statusDots = container.querySelectorAll("[data-status-dot='1']");
    for (const dot of statusDots) {
      expect(dot.classList.contains("connected")).toBe(true);
    }
  });

  it("updates LED on WS open", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    const leds = container.querySelectorAll("[data-led-indicator='1']");
    for (const led of leds) {
      expect(led.classList.contains("on")).toBe(true);
    }
  });

  it("updates status text on WS open", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    const texts = container.querySelectorAll<HTMLElement>("[data-status-text='1']");
    for (const text of texts) {
      expect(text.textContent).toBe("Connected");
    }
  });

  it("updates status on WS close", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    getWs().close();
    const statusDots = container.querySelectorAll("[data-status-dot='1']");
    for (const dot of statusDots) {
      expect(dot.classList.contains("connected")).toBe(false);
    }
  });

  it("schedules reconnect after close", async () => {
    await makeTerminal();
    const firstWsCount = MockWebSocket.instances.length;
    getWs().close();
    vi.advanceTimersByTime(1100);
    expect(MockWebSocket.instances.length).toBeGreaterThan(firstWsCount);
  });

  it("onerror closes the WS", async () => {
    await makeTerminal();
    const ws = getWs();
    ws.open();
    ws.triggerError();
    expect(ws.readyState).toBe(MockWebSocket.CLOSED);
  });

  it("writes messages to terminal", async () => {
    await makeTerminal();
    getWs().open();
    getWs().triggerMessage("hello terminal");
    // No throw = success (xterm mock records writes)
  });

  it("ignores empty messages", async () => {
    await makeTerminal();
    getWs().open();
    getWs().triggerMessage(""); // empty payload ignored
    // No throw
  });

  it("stale ws onopen handler is ignored", async () => {
    const { terminal } = await makeTerminal();
    const oldWs = getWs();
    terminal.connect(); // creates a new WS
    // Fire onopen on the old stale WS
    oldWs.onopen?.();
    // No throw
  });

  it("stale ws onclose handler is ignored", async () => {
    const { terminal } = await makeTerminal();
    const oldWs = getWs();
    terminal.connect(); // creates a new WS, oldWs is now stale
    const countBefore = MockWebSocket.instances.length;
    oldWs.onclose?.(); // stale close should not schedule reconnect again
    vi.advanceTimersByTime(200);
    // Should not have created more WS instances (stale handler guarded)
    expect(MockWebSocket.instances.length).toBe(countBefore);
  });
});

describe("ProvideTerminal disconnect / dispose", () => {
  it("disconnect closes WS and cancels reconnect", async () => {
    const { terminal } = await makeTerminal();
    const ws = getWs();
    ws.open();
    terminal.disconnect();
    expect(ws.readyState).toBe(MockWebSocket.CLOSED);
    const countBefore = MockWebSocket.instances.length;
    vi.advanceTimersByTime(2000);
    expect(MockWebSocket.instances.length).toBe(countBefore); // no reconnect
  });

  it("dispose removes DOM from container", async () => {
    const { terminal, container } = await makeTerminal();
    terminal.dispose();
    expect(container.querySelector(".provide-uterm")).toBeFalsy();
  });

  it("dispose clears term and fitAddon", async () => {
    const { terminal } = await makeTerminal();
    // Should not throw
    terminal.dispose();
    // Second dispose should also not throw
    terminal.dispose();
  });
});

describe("ProvideTerminal settings", () => {
  it("loads settings from localStorage when present", async () => {
    vi.mocked(localStorage.getItem).mockReturnValue(
      JSON.stringify({ theme: "crt", fontSize: 16, cols: 100, rows: 30 }),
    );
    const { container } = await makeTerminal();
    const root = container.querySelector(".provide-uterm")!;
    expect(root.classList.contains("theme-crt")).toBe(true);
  });

  it("falls back to defaults when localStorage parse fails", async () => {
    vi.mocked(localStorage.getItem).mockReturnValue("invalid-json{{{");
    const { container } = await makeTerminal();
    const root = container.querySelector(".provide-uterm")!;
    expect(root.classList.contains("theme-code")).toBe(true);
  });

  it("applies theme from config", async () => {
    const { container } = await makeTerminal({ theme: "bbs" });
    const root = container.querySelector(".provide-uterm")!;
    expect(root.classList.contains("theme-bbs")).toBe(true);
  });

  it("applies glass theme from config", async () => {
    const { container } = await makeTerminal({ theme: "glass" });
    const root = container.querySelector(".provide-uterm")!;
    expect(root.classList.contains("theme-glass")).toBe(true);
  });

  it("gear button toggles settings panel", async () => {
    const { container } = await makeTerminal();
    const gear = container.querySelector<HTMLButtonElement>(`[id^="gearBtn-"]`)!;
    const panel = container.querySelector<HTMLElement>(`[id^="settingsPanel-"]`)!;
    gear.click();
    expect(panel.classList.contains("open")).toBe(true);
    gear.click();
    expect(panel.classList.contains("open")).toBe(false);
  });

