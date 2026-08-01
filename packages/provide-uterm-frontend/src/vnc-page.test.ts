//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  attachVnc,
  buildVncWsUrl,
  NOVNC_RFB_MODULE,
  readVncPageParams,
  resolveRfbConstructor,
  type RfbConstructor,
  sanitizeId,
  statusFromCloseCode,
  VncConsolePage,
} from "./vnc-page.js";

const _here = dirname(fileURLToPath(import.meta.url));

describe("vnc-page helpers", () => {
  it("sanitizeId accepts safe identifiers", () => {
    expect(sanitizeId("lab-vnc")).toBe("lab-vnc");
    expect(sanitizeId("a_b.1-2")).toBe("a_b.1-2");
    expect(sanitizeId("../evil")).toBe("");
    expect(sanitizeId("bad id")).toBe("");
    expect(sanitizeId(null, "x")).toBe("x");
  });

  it("readVncPageParams parses query string", () => {
    const p = readVncPageParams(
      "?worker_id=w1&hijack_id=00000000-0000-0000-0000-0000000000ab&target_id=lab-vnc&view_only=1",
    );
    expect(p.workerId).toBe("w1");
    expect(p.hijackId).toBe("00000000-0000-0000-0000-0000000000ab");
    expect(p.targetId).toBe("lab-vnc");
    expect(p.viewOnly).toBe(true);
  });

  it("buildVncWsUrl builds binary WS path", () => {
    const url = buildVncWsUrl(
      {
        workerId: "w1",
        hijackId: "00000000-0000-0000-0000-0000000000ab",
        targetId: "lab-vnc",
      },
      { protocol: "https:", host: "example.test:8443" },
    );
    expect(url).toBe(
      "wss://example.test:8443/worker/w1/hijack/00000000-0000-0000-0000-0000000000ab/gui/vnc?target_id=lab-vnc",
    );
  });

  it("buildVncWsUrl rejects missing ids", () => {
    expect(() => buildVncWsUrl({ workerId: "", hijackId: "h", targetId: "t" })).toThrow(/required/);
  });

  it("statusFromCloseCode maps policy and upstream codes", () => {
    expect(statusFromCloseCode(1008).state).toBe("denied");
    expect(statusFromCloseCode(1013).state).toBe("unavailable");
    expect(statusFromCloseCode(1000).state).toBe("disconnected");
  });
});

describe("VncConsolePage", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  function setupDom(): void {
    document.body.innerHTML = `
      <div id="vnc-status" data-state="idle">Ready</div>
      <div id="vnc-detail"></div>
      <div id="vnc-dims">—</div>
      <div id="vnc-screen"></div>
      <button id="vnc-connect" class="primary"></button>
      <button id="vnc-disconnect" disabled></button>
    `;
  }

  it("throws when required elements are missing", () => {
    document.body.innerHTML = `<div id="vnc-status"></div>`;
    expect(
      () =>
        new VncConsolePage(
          document,
          {
            workerId: "",
            hijackId: "",
            targetId: "",
            viewOnly: false,
            token: null,
          },
          class {
            viewOnly = false;
            scaleViewport = false;
            clipViewport = false;
            resizeSession = false;
            background = "";
            addEventListener(): void {}
            removeEventListener(): void {}
            disconnect(): void {}
          },
        ),
    ).toThrow(/Missing required element/);
  });

  it("connect creates RFB against gui/vnc URL and paints status", async () => {
    setupDom();
    const constructed: { url: string; target: HTMLElement }[] = [];
    class FakeRFB {
      viewOnly = false;
      scaleViewport = false;
      clipViewport = false;
      resizeSession = false;
      background = "";
      qualityLevel = 6;
      compressionLevel = 2;
      private listeners = new Map<string, Array<(ev: Event) => void>>();
      constructor(target: HTMLElement, url: string) {
        constructed.push({ url, target });
      }
      addEventListener(type: string, listener: (ev: Event) => void): void {
        const list = this.listeners.get(type) || [];
        list.push(listener);
        this.listeners.set(type, list);
      }
      removeEventListener(): void {}
      disconnect(): void {
        for (const fn of this.listeners.get("disconnect") || []) {
          fn(new CustomEvent("disconnect", { detail: { clean: true, code: 1000 } }));
        }
      }
      emitConnect(): void {
        for (const fn of this.listeners.get("connect") || []) {
          fn(new Event("connect"));
        }
      }
    }

    const page = new VncConsolePage(
      document,
      {
        workerId: "w1",
        hijackId: "00000000-0000-0000-0000-0000000000ab",
        targetId: "lab-vnc",
        viewOnly: false,
        token: null,
      },
      FakeRFB as unknown as new (
        target: HTMLElement,
        url: string | WebSocket,
        options?: Record<string, unknown>,
      ) => FakeRFB,
    );

    // Auto-connect is async when ids present — wait for RFB construction.
    await viWaitFor(() => constructed.length === 1);
    expect(constructed[0].url).toContain("/worker/w1/hijack/");
    expect(constructed[0].url).toContain("/gui/vnc?target_id=lab-vnc");
    expect(constructed[0].url.startsWith("ws")).toBe(true);
    expect(page.statusState).toBe("connecting");

    const rfb = (page as unknown as { rfb: FakeRFB | null }).rfb;
    rfb?.emitConnect();
    expect(page.statusState).toBe("connected");
    expect(document.getElementById("vnc-status")?.textContent).toMatch(/Connected/);
  });

  it("source imports real noVNC RFB module path (not a reimplementation)", () => {
    const src = readFileSync(join(_here, "vnc-page.ts"), "utf8");
    expect(src).toContain(NOVNC_RFB_MODULE);
    expect(src).toMatch(/import\(["']@novnc\/novnc["']\)/);
    const pkg = JSON.parse(readFileSync(join(_here, "..", "package.json"), "utf8")) as {
      dependencies?: Record<string, string>;
    };
    expect(pkg.dependencies?.["@novnc/novnc"]).toBeTruthy();
    // Prefer latest line (1.7+ ships native ESM core/).
    const ver = pkg.dependencies?.["@novnc/novnc"] ?? "";
    expect(ver).toMatch(/\^?1\.(?:[7-9]|\d{2,})\./);
  });

  it("resolveRfbConstructor unwraps nested default exports", () => {
    class Fake {}
    expect(resolveRfbConstructor(Fake)).toBe(Fake);
    expect(resolveRfbConstructor({ default: Fake })).toBe(Fake);
    expect(resolveRfbConstructor({ default: { default: Fake } })).toBe(Fake);
    expect(() => resolveRfbConstructor({ default: { foo: 1 } })).toThrow(/not a constructor/);
  });

  it("describes missing params and rejects a manual connection", async () => {
    setupDom();
    const page = new VncConsolePage(document, {
      workerId: "", hijackId: "", targetId: "", viewOnly: false, token: null,
    });
    expect(page.describeParams()).toContain("Missing worker_id");
    await page.connect();
    expect(page.statusState).toBe("error");
    expect((document.getElementById("vnc-connect") as HTMLButtonElement).disabled).toBe(false);
  });

  it("reports constructor failures and restores connection controls", async () => {
    setupDom();
    class FailingRfb {
      constructor() { throw new Error("RFB unavailable"); }
    }
    const page = new VncConsolePage(
      document,
      { workerId: "w", hijackId: "h", targetId: "t", viewOnly: true, token: null },
      FailingRfb as unknown as RfbConstructor,
    );
    await viWaitFor(() => page.statusState === "error");
    expect(document.getElementById("vnc-status")?.textContent).toContain("RFB unavailable");
    expect((document.getElementById("vnc-connect") as HTMLButtonElement).disabled).toBe(false);
    expect((document.getElementById("vnc-disconnect") as HTMLButtonElement).disabled).toBe(true);
  });

  it("maps disconnect and security events and refreshes canvas dimensions", async () => {
    vi.useFakeTimers();
    setupDom();
    const instances: EventRfb[] = [];
    class EventRfb {
      viewOnly = false;
      scaleViewport = false;
      clipViewport = true;
      resizeSession = true;
      background = "";
      qualityLevel = 0;
      compressionLevel = 0;
      listeners = new Map<string, Array<(event: Event) => void>>();
      constructor(target: HTMLElement) {
        const canvas = document.createElement("canvas");
        canvas.width = 1024;
        canvas.height = 768;
        target.appendChild(canvas);
        instances.push(this);
      }
      addEventListener(type: string, listener: (event: Event) => void) {
        this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
      }
      removeEventListener() {}
      disconnect() { this.emit("disconnect", { clean: true }); }
      emit(type: string, detail?: Record<string, unknown>) {
        for (const listener of this.listeners.get(type) ?? []) {
          listener(new CustomEvent(type, { detail }));
        }
      }
    }
    const page = new VncConsolePage(
      document,
      { workerId: "w", hijackId: "h", targetId: "desktop", viewOnly: true, token: null },
      EventRfb as unknown as RfbConstructor,
    );
    await Promise.resolve();
    const rfb = instances[0];
    expect(rfb).toMatchObject({ viewOnly: true, scaleViewport: true, clipViewport: false, resizeSession: false, qualityLevel: 9, compressionLevel: 2 });
    rfb?.emit("connect");
    vi.advanceTimersByTime(1_200);
    expect(document.getElementById("vnc-dims")?.textContent).toBe("1024×768 · desktop");
    rfb?.emit("securityfailure");
    expect(page.statusState).toBe("error");
    rfb?.emit("disconnect", { clean: false });
    expect(document.getElementById("vnc-status")?.textContent).toBe("Connection lost");
    await page.connect();
    instances.at(-1)?.emit("disconnect", { code: 1008 });
    expect(page.statusState).toBe("denied");
    page.disconnect();
    expect(page.statusState).toBe("disconnected");
    vi.useRealTimers();
  });
});

describe("attachVnc", () => {
  it("reports invalid params without constructing an RFB client", () => {
    const screen = document.createElement("div");
    const status = vi.fn();
    const RfbClass = vi.fn();
    const handle = attachVnc(
      screen,
      { workerId: "", hijackId: "h", targetId: "t", viewOnly: false, token: null },
      { RfbClass: RfbClass as unknown as RfbConstructor, onStatus: status },
    );
    expect(status).toHaveBeenCalledWith("error", expect.stringContaining("required"));
    expect(RfbClass).not.toHaveBeenCalled();
    expect(() => handle.disconnect()).not.toThrow();
  });

  it("configures an embedded RFB and routes status/dimensions including a clean disconnect", async () => {
    vi.useFakeTimers();
    const screen = document.createElement("div");
    const status = vi.fn();
    const dims = vi.fn();
    let instance: EmbeddedRfb | null = null;
    class EmbeddedRfb {
      viewOnly = false;
      scaleViewport = false;
      clipViewport = true;
      resizeSession = true;
      background = "";
      listeners = new Map<string, Array<(event: Event) => void>>();
      disconnect = vi.fn();
      constructor() { instance = this; }
      addEventListener(type: string, listener: (event: Event) => void) {
        this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
      }
      removeEventListener() {}
      emit(type: string, detail?: Record<string, unknown>) {
        for (const listener of this.listeners.get(type) ?? []) listener(new CustomEvent(type, { detail }));
      }
    }
    attachVnc(
      screen,
      { workerId: "w", hijackId: "h", targetId: "desk", viewOnly: true, token: null },
      { RfbClass: EmbeddedRfb as unknown as RfbConstructor, onStatus: status, onDims: dims },
    );
    await Promise.resolve();
    const rfb = instance as EmbeddedRfb | null;
    expect(status).toHaveBeenCalledWith("connecting", "Connecting…");
    expect(rfb?.viewOnly).toBe(true);
    const canvas = document.createElement("canvas");
    canvas.width = 640;
    canvas.height = 480;
    screen.appendChild(canvas);
    rfb?.emit("connect");
    vi.advanceTimersByTime(1_200);
    expect(dims).toHaveBeenCalledWith("640×480");
    rfb?.emit("securityfailure");
    expect(status).toHaveBeenCalledWith("error", "RFB security failure");
    rfb?.emit("disconnect", { clean: true });
    expect(status).toHaveBeenCalledWith("disconnected", "Disconnected");
    vi.useRealTimers();
  });

  it("disconnects the attached embedded RFB client during teardown", async () => {
    const screen = document.createElement("div");
    let instance: EmbeddedRfb | null = null;
    class EmbeddedRfb {
      disconnect = vi.fn();
      constructor() { instance = this; }
      addEventListener() {}
      removeEventListener() {}
    }
    const handle = attachVnc(
      screen,
      { workerId: "w", hijackId: "h", targetId: "desk", viewOnly: false, token: null },
      { RfbClass: EmbeddedRfb as unknown as RfbConstructor, onStatus: vi.fn() },
    );
    await Promise.resolve();
    const rfb = instance as EmbeddedRfb | null;

    handle.disconnect();

    expect(rfb?.disconnect).toHaveBeenCalledOnce();
    handle.disconnect();
    expect(rfb?.disconnect).toHaveBeenCalledOnce();
  });

  it("reports embedded constructor failures", async () => {
    const status = vi.fn();
    class Broken { constructor() { throw new Error("broken client"); } }
    attachVnc(
      document.createElement("div"),
      { workerId: "w", hijackId: "h", targetId: "desk", viewOnly: false, token: null },
      { RfbClass: Broken as unknown as RfbConstructor, onStatus: status },
    );
    await Promise.resolve();
    expect(status).toHaveBeenCalledWith("error", expect.stringContaining("broken client"));
  });
});

async function viWaitFor(pred: () => boolean, timeoutMs = 1000): Promise<void> {
  const start = Date.now();
  while (!pred()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error("timeout waiting for condition");
    }
    await new Promise((r) => setTimeout(r, 10));
  }
}
