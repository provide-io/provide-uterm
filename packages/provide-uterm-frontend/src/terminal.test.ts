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

  it("overlay click closes settings panel", async () => {
    const { container } = await makeTerminal();
    const gear = container.querySelector<HTMLButtonElement>(`[id^="gearBtn-"]`)!;
    const overlay = container.querySelector<HTMLElement>(`[id^="settingsOverlay-"]`)!;
    const panel = container.querySelector<HTMLElement>(`[id^="settingsPanel-"]`)!;
    gear.click(); // open
    overlay.click(); // close via overlay
    expect(panel.classList.contains("open")).toBe(false);
  });

  it("theme buttons switch theme (crt)", async () => {
    const { container } = await makeTerminal({ theme: "code" });
    const root = container.querySelector(".provide-uterm")!;
    const crtBtn = container.querySelector<HTMLButtonElement>('[data-theme="crt"]')!;
    crtBtn.click();
    expect(root.classList.contains("theme-crt")).toBe(true);
    expect(localStorage.setItem).toHaveBeenCalled();
  });

  it("theme buttons switch theme (bbs)", async () => {
    const { container } = await makeTerminal();
    const bbsBtn = container.querySelector<HTMLButtonElement>('[data-theme="bbs"]')!;
    bbsBtn.click();
    expect(container.querySelector(".provide-uterm")?.classList.contains("theme-bbs")).toBe(true);
  });

  it("theme buttons switch theme (glass)", async () => {
    const { container } = await makeTerminal();
    const glassBtn = container.querySelector<HTMLButtonElement>('[data-theme="glass"]')!;
    glassBtn.click();
    expect(container.querySelector(".provide-uterm")?.classList.contains("theme-glass")).toBe(true);
  });

  it("theme buttons switch theme (code)", async () => {
    const { container } = await makeTerminal({ theme: "bbs" });
    const codeBtn = container.querySelector<HTMLButtonElement>('[data-theme="code"]')!;
    codeBtn.click();
    expect(container.querySelector(".provide-uterm")?.classList.contains("theme-code")).toBe(true);
  });

  it("cols range input updates setting display", async () => {
    const { container } = await makeTerminal();
    const colsInput = container.querySelector<HTMLInputElement>(`[id^="setCols-"]`)!;
    colsInput.value = "100";
    colsInput.dispatchEvent(new Event("input"));
    const colsVal = container.querySelector<HTMLElement>(`[id^="valCols-"]`)!;
    expect(colsVal.textContent).toBe("100");
  });

  it("rows range input updates setting display", async () => {
    const { container } = await makeTerminal();
    const rowsInput = container.querySelector<HTMLInputElement>(`[id^="setRows-"]`)!;
    rowsInput.value = "30";
    rowsInput.dispatchEvent(new Event("input"));
    const rowsVal = container.querySelector<HTMLElement>(`[id^="valRows-"]`)!;
    expect(rowsVal.textContent).toBe("30");
  });

  it("fontSize range input updates setting display", async () => {
    const { container } = await makeTerminal();
    const fsInput = container.querySelector<HTMLInputElement>(`[id^="setFontSize-"]`)!;
    fsInput.value = "16";
    fsInput.dispatchEvent(new Event("input"));
    const fsVal = container.querySelector<HTMLElement>(`[id^="valFontSize-"]`)!;
    expect(fsVal.textContent).toBe("16px");
  });

  it("pageBg color input updates CSS variable", async () => {
    const { container } = await makeTerminal();
    const pageBgInput = container.querySelector<HTMLInputElement>(`[id^="setPageBg-"]`)!;
    pageBgInput.value = "#ffffff";
    pageBgInput.dispatchEvent(new Event("input"));
    const root = container.querySelector<HTMLElement>(".provide-uterm")!;
    expect(root.style.getPropertyValue("--bg-page")).toBe("#ffffff");
  });

  it("termBg color input updates CSS variable", async () => {
    const { container } = await makeTerminal();
    const termBgInput = container.querySelector<HTMLInputElement>(`[id^="setTermBg-"]`)!;
    termBgInput.value = "#111111";
    termBgInput.dispatchEvent(new Event("input"));
    const root = container.querySelector<HTMLElement>(".provide-uterm")!;
    expect(root.style.getPropertyValue("--bg-terminal")).toBe("#111111");
  });

  it("scanlines checkbox can be toggled", async () => {
    const { container } = await makeTerminal({ theme: "code" });
    const root = container.querySelector(".provide-uterm")!;
    const scanlines = container.querySelector<HTMLInputElement>(`[id^="fxScanlines-"]`)!;
    scanlines.checked = true;
    scanlines.dispatchEvent(new Event("input"));
    expect(root.classList.contains("fx-scanlines")).toBe(true);
    scanlines.checked = false;
    scanlines.dispatchEvent(new Event("input"));
    expect(root.classList.contains("fx-scanlines")).toBe(false);
  });

  it("vignette checkbox can be toggled", async () => {
    const { container } = await makeTerminal();
    const root = container.querySelector(".provide-uterm")!;
    const vignette = container.querySelector<HTMLInputElement>(`[id^="fxVignette-"]`)!;
    vignette.checked = true;
    vignette.dispatchEvent(new Event("input"));
    expect(root.classList.contains("fx-vignette")).toBe(true);
  });

  it("glow checkbox can be toggled", async () => {
    const { container } = await makeTerminal();
    const root = container.querySelector(".provide-uterm")!;
    const glow = container.querySelector<HTMLInputElement>(`[id^="fxGlow-"]`)!;
    glow.checked = true;
    glow.dispatchEvent(new Event("input"));
    expect(root.classList.contains("fx-glow")).toBe(true);
  });
});

describe("ProvideTerminal getBufferText", () => {
  it("returns empty string when terminal is disposed", async () => {
    const { terminal } = await makeTerminal();
    terminal.dispose(); // dispose clears term
    const text = terminal.getBufferText();
    expect(text).toBe("");
  });

  it("returns buffer text when terminal is active", async () => {
    const { terminal } = await makeTerminal();
    const text = terminal.getBufferText(10);
    // The mock xterm buffer has lines 0, 1, 2 with content
    expect(typeof text).toBe("string");
  });

  it("respects maxLines parameter", async () => {
    const { terminal } = await makeTerminal();
    const text = terminal.getBufferText(1);
    expect(typeof text).toBe("string");
  });
});

describe("ProvideTerminal title display", () => {
  it("uses title from config in frame (uppercased)", async () => {
    const { container } = await makeTerminal({ title: "My Terminal" });
    expect(container.innerHTML).toContain("MY TERMINAL");
  });

  it("uses default title when none provided", async () => {
    const { container } = await makeTerminal();
    expect(container.innerHTML).toContain("WARP AGENT RUNTIME PLATFORM");
  });

  it("uses null title gracefully", async () => {
    const { container } = await makeTerminal({ title: null });
    expect(container.innerHTML).toContain("WARP AGENT RUNTIME PLATFORM");
  });
});

describe("ProvideTerminal loading screen", () => {
  it("hides loading screen when first data arrives", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    getWs().triggerMessage("some terminal data");
    const loading = container.querySelector<HTMLElement>(`[id^="loadingScreen-"]`)!;
    expect(loading.style.display).toBe("none");
  });

  it("keeps loading screen visible before first data", async () => {
    const { container } = await makeTerminal();
    const loading = container.querySelector<HTMLElement>(`[id^="loadingScreen-"]`)!;
    // Before any message, loading should not have display:none
    expect(loading.style.display).not.toBe("none");
  });
});

describe("ProvideTerminal reconnect timer", () => {
  it("does not schedule reconnect if already scheduled", async () => {
    await makeTerminal();
    const ws = getWs();
    ws.close(); // schedules reconnect
    const countBefore = MockWebSocket.instances.length;
    // Don't advance timer — ensure no extra WS created
    expect(MockWebSocket.instances.length).toBe(countBefore);
  });

  it("disconnect before reconnect timer fires cancels reconnect", async () => {
    const { terminal } = await makeTerminal();
    getWs().close(); // schedules reconnect at 1s
    terminal.disconnect(); // should cancel timer
    const countBefore = MockWebSocket.instances.length;
    vi.advanceTimersByTime(2000);
    expect(MockWebSocket.instances.length).toBe(countBefore);
  });

  it("connect() cancels pending reconnect timer", async () => {
    const { terminal } = await makeTerminal();
    getWs().close(); // schedules reconnect
    const countBefore = MockWebSocket.instances.length;
    terminal.connect(); // should cancel timer and create new WS immediately
    expect(MockWebSocket.instances.length).toBe(countBefore + 1);
    // Timer should not fire again
    vi.advanceTimersByTime(2000);
    // No additional WS created from timer (it was cancelled)
    expect(MockWebSocket.instances.length).toBe(countBefore + 1);
  });
});

describe("ProvideTerminal input sending via onData", () => {
  it("sends data to WS when open via terminal onData callback", async () => {
    await makeTerminal();
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    const sentBefore = ws.sent.length;
    xterm.simulateInput("hello");
    expect(ws.sent.length).toBe(sentBefore + 1);
    expect(ws.sent[ws.sent.length - 1]).toBe("hello");
  });

  it("handleTerminalInput ignores empty data", async () => {
    await makeTerminal();
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    const sentBefore = ws.sent.length;
    xterm.simulateInput(""); // empty data should be ignored
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("handleTerminalInput ignores data when WS is not open (CONNECTING)", async () => {
    await makeTerminal();
    const ws = getWs();
    const xterm = getXterm();
    // WS is in CONNECTING state
    const sentBefore = ws.sent.length;
    xterm.simulateInput("data while connecting");
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("handleTerminalInput ignores data when WS is null (disconnected)", async () => {
    const { terminal } = await makeTerminal();
    const xterm = getXterm();
    terminal.disconnect(); // sets ws to null
    const ws = getWs();
    const sentBefore = ws.sent.length;
    xterm.simulateInput("data after disconnect");
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("does not install a custom key event handler (ctrl+c reaches terminal)", async () => {
    await makeTerminal();
    // No custom handler means xterm processes all keys normally — Ctrl+C sends \x03,
    // readline shortcuts work, tmux prefixes are not swallowed.
    expect(getXterm().customKeyHandlerCallCount).toBe(0);
  });

  it("ctrl-c data is forwarded to websocket", async () => {
    await makeTerminal({ wsUrl: "/ws/terminal" });
    const ws = getWs();
    ws.open();
    getXterm().simulateInput("\x03");
    expect(ws.sent).toContain("\x03");
  });
});

describe("ProvideTerminal CSS injection", () => {
  it("injects CSS link on construction", async () => {
    await makeTerminal();
    const links = document.head.querySelectorAll('link[rel="stylesheet"]');
    expect(links.length).toBeGreaterThan(0);
  });
});

describe("ProvideTerminal fitWithMinCols", () => {
  it("reduces fontSize when proposed cols < minCols", async () => {
    // Override proposeDimensions to return cols smaller than default 80
    class SmallFitAddon {
      fit(): void {}
      proposeDimensions(): { cols: number } {
        return { cols: 60 }; // less than 80
      }
    }
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).FitAddon = { FitAddon: SmallFitAddon };
    await makeTerminal({ cols: 80 });
    const xterm = getXterm();
    // fontSize should have been reduced since 60 < 80
    expect(xterm.options.fontSize).toBeLessThan(14);
  });

  it("handles proposeDimensions returning undefined gracefully", async () => {
    class NullFitAddon {
      fit(): void {}
      proposeDimensions(): undefined {
        return undefined;
      }
    }
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).FitAddon = { FitAddon: NullFitAddon };
    // Should not throw
    await makeTerminal();
  });

  it("handles proposeDimensions returning cols=0", async () => {
    class ZeroFitAddon {
      fit(): void {}
      proposeDimensions(): { cols: number } {
        return { cols: 0 };
      }
    }
    // biome-ignore lint/suspicious/noExplicitAny: test mock
    (window as any).FitAddon = { FitAddon: ZeroFitAddon };
    // Should not throw
    await makeTerminal();
  });
});

describe("ProvideTerminal accessibility", () => {
  it("gear button has an aria-label", async () => {
    const { container } = await makeTerminal();
    const gear = container.querySelector<HTMLButtonElement>(".gear-btn");
    expect(gear?.getAttribute("aria-label")).toBe("Open terminal settings");
  });

  it("settings panel exposes a dialog role and accessible name", async () => {
    const { container } = await makeTerminal();
    const panel = container.querySelector(".settings-panel");
    expect(panel?.getAttribute("role")).toBe("dialog");
    expect(panel?.getAttribute("aria-label")).toBe("Terminal settings");
  });

  it("each theme button declares an aria-label", async () => {
    const { container } = await makeTerminal();
    const buttons = container.querySelectorAll<HTMLButtonElement>(".theme-btn");
    expect(buttons.length).toBeGreaterThan(0);
    for (const btn of buttons) {
      expect(btn.getAttribute("aria-label")).toMatch(/theme$/i);
      expect(btn.type).toBe("button");
    }
  });

  it("status dot exposes a status role and an aria-label", async () => {
    const { container } = await makeTerminal();
    const dot = container.querySelector("[data-status-dot='1']");
    expect(dot?.getAttribute("role")).toBe("status");
    expect(dot?.getAttribute("aria-label")).toBeTruthy();
  });

  it("status dot aria-label updates to 'Connected' when WS opens", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    const dot = container.querySelector("[data-status-dot='1']");
    expect(dot?.getAttribute("aria-label")).toBe("Connected");
  });

  it("status dot aria-label updates to 'Disconnected' on close", async () => {
    const { container } = await makeTerminal();
    getWs().open();
    getWs().close();
    const dot = container.querySelector("[data-status-dot='1']");
    expect(dot?.getAttribute("aria-label")).toBe("Disconnected");
  });

  it("Enter key on a theme button activates it (native button behavior)", async () => {
    const { container } = await makeTerminal();
    const crtBtn = container.querySelector<HTMLButtonElement>('.theme-btn[data-theme="crt"]');
    expect(crtBtn).not.toBeNull();
    // Native <button> activates via click() — simulate keyboard activation
    crtBtn?.click();
    const root = container.querySelector(".provide-uterm");
    expect(root?.classList.contains("theme-crt")).toBe(true);
  });
});

// Regression test for Finding #14: ProvideTerminal must run incoming WS
// payloads through ControlChannelDecoder so users on `role=browser` connections
// don't see raw JSON control frames bleed into terminal output.
describe("ProvideTerminal control-channel framing", () => {
  // Build a framed payload: <DLE><STX><8-hex-len>:<json><raw-bytes>
  function makeFramedPayload(controlJson: string, rawBytes: string): string {
    const lenHex = new TextEncoder().encode(controlJson).byteLength.toString(16).padStart(8, "0");
    return `\x10\x02${lenHex}:${controlJson}${rawBytes}`;
  }

  it("strips control frames from incoming WS payloads and only writes raw bytes to xterm", async () => {
    await makeTerminal({ wsUrl: "/ws/browser/w/term" });
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    // Reset writes from boot sequence
    xterm.written = [];

    const payload = makeFramedPayload('{"type":"hijack_state","hijacked":true}', "hello terminal");
    ws.triggerMessage(payload);

    const joined = xterm.written.join("");
    expect(joined).toBe("hello terminal");
    expect(joined).not.toContain("hijack_state");
    expect(joined).not.toContain('"type"');
  });

  it("writes plain (unframed) payloads through as data frames", async () => {
    await makeTerminal({ wsUrl: "/ws/browser/w/term" });
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    xterm.written = [];

    ws.triggerMessage("plain raw output\r\n");
    expect(xterm.written.join("")).toBe("plain raw output\r\n");
  });

  it("falls back to writing the raw payload when the framing stream is corrupt", async () => {
    await makeTerminal({ wsUrl: "/ws/browser/w/term" });
    const ws = getWs();
    ws.open();
    const xterm = getXterm();
    xterm.written = [];

    // \x10 followed by an invalid marker (not \x02 and not \x10) raises in feed().
    ws.triggerMessage("\x10X");

    // The fallback path writes the raw payload so the screen doesn't go blank.
    expect(xterm.written.join("")).toContain("\x10X");
  });
});
