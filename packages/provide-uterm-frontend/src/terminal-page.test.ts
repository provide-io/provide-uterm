//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// terminal-page.ts is a script that runs immediately on load and calls initTerminalPage().
// We need to set up all global state before importing it.
import { afterEach, describe, expect, it, vi } from "vitest";

// terminal-page.ts now calls widget.connect() after mounting; register a stub
// <uterm-terminal> so the generic element has the config/connect surface it uses.
class _StubTerminal extends HTMLElement {
  config: unknown = {};
  connect(): void {}
}
if (!customElements.get("uterm-terminal")) {
  customElements.define("uterm-terminal", _StubTerminal);
}

describe("terminal-page (module-level execution)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
    vi.resetModules();
  });

  it("throws when #app element is missing", async () => {
    // No #app element in DOM
    vi.stubGlobal("window", {
      ...window,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    await expect(import("./terminal-page.js")).rejects.toThrow("Missing #app container");
  });

  it("creates uterm-terminal instance with correct wsUrl for raw role", async () => {
    const app = document.createElement("div");
    app.id = "app";
    document.body.appendChild(app);
    vi.stubGlobal("window", {
      ...window,
      location: {
        search: "?worker_id=myworker",
        protocol: "http:",
        host: "localhost",
      },
    });
    await import("./terminal-page.js");
    const widget = document.querySelector("uterm-terminal") as any;
    expect(widget).toBeTruthy();
    expect(widget.config.wsUrl).toBe("/ws/raw/myworker/term");
  });

  it("uses 'demo' worker when worker_id is absent", async () => {
    const app = document.createElement("div");
    app.id = "app";
    document.body.appendChild(app);
    vi.stubGlobal("window", {
      ...window,
      location: { search: "", protocol: "http:", host: "localhost" },
    });
    await import("./terminal-page.js");
    const widget = document.querySelector("uterm-terminal") as any;
    expect(widget).toBeTruthy();
    expect(widget.config.wsUrl).toBe("/ws/raw/demo/term");
  });

  it("uses 'browser' role when role=browser param present", async () => {
    const app = document.createElement("div");
    app.id = "app";
    document.body.appendChild(app);
    vi.stubGlobal("window", {
      ...window,
      location: { search: "?worker_id=w1&role=browser", protocol: "http:", host: "localhost" },
    });
    await import("./terminal-page.js");
    const widget = document.querySelector("uterm-terminal") as any;
    expect(widget).toBeTruthy();
    expect(widget.config.wsUrl).toBe("/ws/browser/w1/term");
  });

  it("uses 'raw' role for non-browser role values", async () => {
    const app = document.createElement("div");
    app.id = "app";
    document.body.appendChild(app);
    vi.stubGlobal("window", {
      ...window,
      location: { search: "?worker_id=w1&role=admin", protocol: "http:", host: "localhost" },
    });
    await import("./terminal-page.js");
    const widget = document.querySelector("uterm-terminal") as any;
    expect(widget).toBeTruthy();
    expect(widget.config.wsUrl).toBe("/ws/raw/w1/term");
  });

  it("sanitizes invalid worker_id to 'demo'", async () => {
    const app = document.createElement("div");
    app.id = "app";
    document.body.appendChild(app);
    vi.stubGlobal("window", {
      ...window,
      location: { search: "?worker_id=invalid!chars!", protocol: "http:", host: "localhost" },
    });
    await import("./terminal-page.js");
    const widget = document.querySelector("uterm-terminal") as any;
    expect(widget).toBeTruthy();
    expect(widget.config.wsUrl).toBe("/ws/raw/demo/term");
  });
});
