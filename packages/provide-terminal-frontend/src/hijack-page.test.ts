//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// hijack-page.ts is a module that immediately creates a HijackDemoPage instance.
// We test it by setting up the DOM and globals before importing.
import { afterEach, describe, expect, it, vi } from "vitest";

function setupDom(): void {
  document.body.innerHTML = `
    <div id="app"></div>
    <select id="demo-mode"><option value="hijack">Hijack</option><option value="open">Open</option></select>
    <div id="demo-session-status"></div>
    <div id="demo-session-note"></div>
    <button id="demo-apply">Apply</button>
    <button id="demo-reset">Reset</button>
  `;
}

// Track constructor calls - use a module-level array that the class writes to
let hijackCalls: Array<{ container: HTMLElement; config: unknown }> = [];

class MockHijackClass {
  constructor(container: HTMLElement, config: unknown) {
    hijackCalls.push({ container, config });
  }
}

describe("hijack-page module", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
    vi.resetModules();
    hijackCalls = [];
  });

  it("throws when #app element is missing", async () => {
    // No DOM setup — #app doesn't exist
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    await expect(import("./hijack-page.js")).rejects.toThrow("Missing required element: #app");
  });

  it("throws when ProvideHijack is not available", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: undefined,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    await expect(import("./hijack-page.js")).rejects.toThrow("ProvideHijack is not available");
  });

  it("creates widget with default worker ID", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            input_mode: "hijack",
            lifecycle_state: "running",
            display_name: "Demo",
          }),
      }),
    );
    await import("./hijack-page.js");
    expect(hijackCalls).toHaveLength(1);
    expect((hijackCalls[0].config as { workerId: string }).workerId).toBe("demo-session");
  });

  it("uses worker param from URL search", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "?worker=custom-worker", protocol: "http:", host: "localhost" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            input_mode: "hijack",
            lifecycle_state: "running",
            display_name: "Custom",
          }),
      }),
    );
    await import("./hijack-page.js");
    expect((hijackCalls[0].config as { workerId: string }).workerId).toBe("custom-worker");
  });

  it("exposes demoHijack on window with all methods", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            input_mode: "open",
            lifecycle_state: "running",
            display_name: "Test",
          }),
      }),
    );
    await import("./hijack-page.js");
    // biome-ignore lint/suspicious/noExplicitAny: test access to window
    const demoHijack = (window as any).demoHijack;
    expect(typeof demoHijack.loadSession).toBe("function");
    expect(typeof demoHijack.applyMode).toBe("function");
    expect(typeof demoHijack.resetSession).toBe("function");
    expect(typeof demoHijack.workerId).toBe("string");
  });

  it("loadSession updates status element on success", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            input_mode: "hijack",
            lifecycle_state: "running",
            display_name: "Test Session",
          }),
      }),
    );
    await import("./hijack-page.js");
    await new Promise((r) => setTimeout(r, 20));
    const statusEl = document.getElementById("demo-session-status")!;
    expect(statusEl.textContent).toContain("Test Session");
    expect(statusEl.classList.contains("error")).toBe(false);
  });

  it("loadSession shows error on failure", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
      }),
    );
    await import("./hijack-page.js");
    await new Promise((r) => setTimeout(r, 20));
    const statusEl = document.getElementById("demo-session-status")!;
    expect(statusEl.textContent).toContain("Session load failed");
    expect(statusEl.classList.contains("error")).toBe(true);
  });

  it("applyMode posts mode change then reloads session", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          input_mode: "open",
          lifecycle_state: "running",
          display_name: "Test",
        }),
    });
    vi.stubGlobal("fetch", mockFetch);
    await import("./hijack-page.js");
    await new Promise((r) => setTimeout(r, 20));
    // biome-ignore lint/suspicious/noExplicitAny: test access
    const demoHijack = (window as any).demoHijack;
    await demoHijack.applyMode();
    // Called for: initial loadSession + applyMode POST + loadSession after apply
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  it("applyMode shows error on failure", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    let callCount = 0;
    const mockFetch = vi.fn().mockImplementation(() => {
      callCount++;
      if (callCount === 1) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ input_mode: "hijack", lifecycle_state: "running", display_name: "T" }),
        });
      }
      return Promise.resolve({ ok: false, status: 500 });
    });
    vi.stubGlobal("fetch", mockFetch);
    await import("./hijack-page.js");
    await new Promise((r) => setTimeout(r, 20));
    // biome-ignore lint/suspicious/noExplicitAny: test access
    const demoHijack = (window as any).demoHijack;
    await demoHijack.applyMode();
    const statusEl = document.getElementById("demo-session-status")!;
    expect(statusEl.textContent).toContain("Mode switch failed");
    expect(statusEl.classList.contains("error")).toBe(true);
  });

  it("resetSession posts restart and updates note", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          input_mode: "hijack",
          lifecycle_state: "running",
          display_name: "Test",
        }),
    });
    vi.stubGlobal("fetch", mockFetch);
    await import("./hijack-page.js");
    await new Promise((r) => setTimeout(r, 20));
    // biome-ignore lint/suspicious/noExplicitAny: test access
    const demoHijack = (window as any).demoHijack;
    await demoHijack.resetSession();
    const noteEl = document.getElementById("demo-session-note")!;
    expect(noteEl.textContent).toBe("Session reset.");
  });

  it("resetSession shows error on failure", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    let callCount = 0;
    const mockFetch = vi.fn().mockImplementation(() => {
      callCount++;
      if (callCount === 1) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ input_mode: "hijack", lifecycle_state: "running", display_name: "T" }),
        });
      }
      return Promise.resolve({ ok: false, status: 500 });
    });
    vi.stubGlobal("fetch", mockFetch);
    await import("./hijack-page.js");
    await new Promise((r) => setTimeout(r, 20));
    // biome-ignore lint/suspicious/noExplicitAny: test access
    const demoHijack = (window as any).demoHijack;
    await demoHijack.resetSession();
    const statusEl = document.getElementById("demo-session-status")!;
    expect(statusEl.textContent).toContain("Reset failed");
    expect(statusEl.classList.contains("error")).toBe(true);
  });

  it("loadSession handles paused lifecycle state", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            input_mode: "hijack",
            lifecycle_state: "paused",
            display_name: "Paused Session",
          }),
      }),
    );
    await import("./hijack-page.js");
    await new Promise((r) => setTimeout(r, 20));
    const statusEl = document.getElementById("demo-session-status")!;
    expect(statusEl.textContent).toContain("paused");
  });

  it("loadSession handles missing display_name gracefully", async () => {
    setupDom();
    vi.stubGlobal("window", {
      ...window,
      ProvideHijack: MockHijackClass,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            input_mode: "hijack",
            lifecycle_state: "running",
            display_name: "",
          }),
      }),
    );
    await import("./hijack-page.js");
    await new Promise((r) => setTimeout(r, 20));
    const statusEl = document.getElementById("demo-session-status")!;
    expect(statusEl.textContent).toContain("Session");
  });
});
