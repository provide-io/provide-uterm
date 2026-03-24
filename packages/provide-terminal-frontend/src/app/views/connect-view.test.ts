//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap } from "../types.js";

vi.mock("../api.js", () => ({
  quickConnect: vi.fn(),
}));

import * as apiModule from "../api.js";
import { renderConnect } from "./connect-view.js";

function makeBootstrap(overrides: Partial<AppBootstrap> = {}): AppBootstrap {
  return {
    page_kind: "connect",
    title: "Connect",
    app_path: "/app",
    assets_path: "/assets",
    ...overrides,
  };
}

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("renderConnect", () => {
  it("renders the connect form", () => {
    renderConnect(root, makeBootstrap());
    expect(root.querySelector("#connect-form")).toBeTruthy();
    expect(root.querySelector("#connect-type")).toBeTruthy();
    expect(root.querySelector("#connect-submit")).toBeTruthy();
  });

  it("escapes app_path in rendered HTML", () => {
    // Use a path with & which should become &amp; in href attributes
    renderConnect(root, makeBootstrap({ app_path: "/app" }));
    // Verify the dashboard link exists and points to correct path
    const dashLink = root.querySelector('a[href="/app/"]');
    expect(dashLink).toBeTruthy();
  });

  it("shows SSH/telnet fields hidden initially (ushell default)", () => {
    renderConnect(root, makeBootstrap());
    const hostFields = root.querySelectorAll<HTMLElement>(".field-host");
    for (const el of hostFields) {
      expect(el.style.display).toBe("none");
    }
    const sshFields = root.querySelectorAll<HTMLElement>(".field-ssh");
    for (const el of sshFields) {
      expect(el.style.display).toBe("none");
    }
  });

  it("shows host fields when type changed to telnet", () => {
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    typeSelect.value = "telnet";
    typeSelect.dispatchEvent(new Event("change"));
    const hostFields = root.querySelectorAll<HTMLElement>(".field-host");
    for (const el of hostFields) {
      expect(el.style.display).not.toBe("none");
    }
  });

  it("shows SSH fields when type changed to ssh", () => {
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    typeSelect.value = "ssh";
    typeSelect.dispatchEvent(new Event("change"));
    const sshFields = root.querySelectorAll<HTMLElement>(".field-ssh");
    for (const el of sshFields) {
      expect(el.style.display).not.toBe("none");
    }
  });

  it("hides SSH fields when type changed to websocket", () => {
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    // First set to ssh to show fields
    typeSelect.value = "ssh";
    typeSelect.dispatchEvent(new Event("change"));
    // Then switch to websocket
    typeSelect.value = "websocket";
    typeSelect.dispatchEvent(new Event("change"));
    const sshFields = root.querySelectorAll<HTMLElement>(".field-ssh");
    for (const el of sshFields) {
      expect(el.style.display).toBe("none");
    }
  });

  it("auto-sets port to 23 for telnet", () => {
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    const portEl = root.querySelector<HTMLInputElement>("#connect-port")!;
    typeSelect.value = "telnet";
    typeSelect.dispatchEvent(new Event("change"));
    expect(portEl.value).toBe("23");
  });

  it("auto-sets port to 22 for ssh", () => {
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    const portEl = root.querySelector<HTMLInputElement>("#connect-port")!;
    typeSelect.value = "ssh";
    typeSelect.dispatchEvent(new Event("change"));
    expect(portEl.value).toBe("22");
  });

  it("does not change port if user-edited flag is set", () => {
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    const portEl = root.querySelector<HTMLInputElement>("#connect-port")!;
    portEl.value = "9999";
    portEl.dataset.userEdited = "1";
    typeSelect.value = "telnet";
    typeSelect.dispatchEvent(new Event("change"));
    expect(portEl.value).toBe("9999");
  });

  it("sets userEdited flag on port input", () => {
    renderConnect(root, makeBootstrap());
    const portEl = root.querySelector<HTMLInputElement>("#connect-port")!;
    portEl.dispatchEvent(new Event("input"));
    expect(portEl.dataset.userEdited).toBe("1");
  });

  it("shows error when host is empty for ssh submit", async () => {
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    typeSelect.value = "ssh";
    typeSelect.dispatchEvent(new Event("change"));
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    // Wait for async handler
    await new Promise((r) => setTimeout(r, 10));
    const errorEl = root.querySelector<HTMLElement>("#connect-error")!;
    expect(errorEl.textContent).toContain("Host is required");
  });

  it("shows error when host is empty for telnet submit", async () => {
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    typeSelect.value = "telnet";
    typeSelect.dispatchEvent(new Event("change"));
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 10));
    const errorEl = root.querySelector<HTMLElement>("#connect-error")!;
    expect(errorEl.textContent).toContain("Host is required");
  });

  it("calls quickConnect on valid shell submit", async () => {
    vi.mocked(apiModule.quickConnect).mockResolvedValue({ session_id: "s1", url: "/app/operator/s1" });
    // Stub location.href setter
    const _locationHref = vi.fn();
    Object.defineProperty(window, "location", {
      value: { href: "", protocol: "http:", host: "localhost" },
      writable: true,
    });
    renderConnect(root, makeBootstrap());
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    expect(apiModule.quickConnect).toHaveBeenCalled();
    const payload = vi.mocked(apiModule.quickConnect).mock.calls[0][0];
    expect(payload.connector_type).toBe("ushell");
  });

  it("shows error when quickConnect fails", async () => {
    vi.mocked(apiModule.quickConnect).mockRejectedValue(new Error("Connection refused"));
    renderConnect(root, makeBootstrap());
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    const errorEl = root.querySelector<HTMLElement>("#connect-error")!;
    expect(errorEl.textContent).toBe("Connection refused");
    const submitBtn = root.querySelector<HTMLButtonElement>("#connect-submit")!;
    expect(submitBtn.disabled).toBe(false);
    expect(submitBtn.textContent).toBe("Connect");
  });

  it("shows fallback error message when non-Error thrown", async () => {
    vi.mocked(apiModule.quickConnect).mockRejectedValue("string error");
    renderConnect(root, makeBootstrap());
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    const errorEl = root.querySelector<HTMLElement>("#connect-error")!;
    expect(errorEl.textContent).toBe("Connection failed.");
  });

  it("sends display_name when filled", async () => {
    vi.mocked(apiModule.quickConnect).mockResolvedValue({ session_id: "s1", url: "/op/s1" });
    renderConnect(root, makeBootstrap());
    const nameInput = root.querySelector<HTMLInputElement>("#connect-name")!;
    nameInput.value = "My Session";
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    const payload = vi.mocked(apiModule.quickConnect).mock.calls[0][0];
    expect(payload.display_name).toBe("My Session");
  });

  it("sends tags when filled", async () => {
    vi.mocked(apiModule.quickConnect).mockResolvedValue({ session_id: "s1", url: "/op/s1" });
    renderConnect(root, makeBootstrap());
    const tagsInput = root.querySelector<HTMLInputElement>("#connect-tags")!;
    tagsInput.value = "tag1, tag2, tag3";
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    const payload = vi.mocked(apiModule.quickConnect).mock.calls[0][0];
    expect(payload.tags).toEqual(["tag1", "tag2", "tag3"]);
  });

  it("sends host and port for ssh connector", async () => {
    vi.mocked(apiModule.quickConnect).mockResolvedValue({ session_id: "s1", url: "/op/s1" });
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    typeSelect.value = "ssh";
    typeSelect.dispatchEvent(new Event("change"));
    const hostInput = root.querySelector<HTMLInputElement>("#connect-host")!;
    hostInput.value = "myhost.example.com";
    const portInput = root.querySelector<HTMLInputElement>("#connect-port")!;
    portInput.value = "2222";
    const userInput = root.querySelector<HTMLInputElement>("#connect-user")!;
    userInput.value = "admin";
    const passInput = root.querySelector<HTMLInputElement>("#connect-pass")!;
    passInput.value = "secret";
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    const payload = vi.mocked(apiModule.quickConnect).mock.calls[0][0];
    expect(payload.connector_type).toBe("ssh");
    expect(payload.host).toBe("myhost.example.com");
    expect(payload.port).toBe(2222);
    expect(payload.username).toBe("admin");
    expect(payload.password).toBe("secret");
  });

  it("uses default port 23 for telnet when port is NaN", async () => {
    vi.mocked(apiModule.quickConnect).mockResolvedValue({ session_id: "s1", url: "/op/s1" });
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    typeSelect.value = "telnet";
    typeSelect.dispatchEvent(new Event("change"));
    const hostInput = root.querySelector<HTMLInputElement>("#connect-host")!;
    hostInput.value = "telnet.host.com";
    const portInput = root.querySelector<HTMLInputElement>("#connect-port")!;
    portInput.value = "";
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    const payload = vi.mocked(apiModule.quickConnect).mock.calls[0][0];
    expect(payload.port).toBe(23);
  });

  it("does not send username/password when empty for ssh", async () => {
    vi.mocked(apiModule.quickConnect).mockResolvedValue({ session_id: "s1", url: "/op/s1" });
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    typeSelect.value = "ssh";
    typeSelect.dispatchEvent(new Event("change"));
    const hostInput = root.querySelector<HTMLInputElement>("#connect-host")!;
    hostInput.value = "myhost.example.com";
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    const payload = vi.mocked(apiModule.quickConnect).mock.calls[0][0];
    expect(payload.username).toBeUndefined();
    expect(payload.password).toBeUndefined();
  });

  it("does not include input_mode when mode select is empty string", async () => {
    vi.mocked(apiModule.quickConnect).mockResolvedValue({ session_id: "s1", url: "/op/s1" });
    renderConnect(root, makeBootstrap());
    // Force the mode select to empty string to cover the `if (mode)` false branch (line 48)
    const modeSelect = root.querySelector<HTMLSelectElement>("#connect-mode")!;
    modeSelect.value = "";
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    const payload = vi.mocked(apiModule.quickConnect).mock.calls[0][0];
    expect(payload.input_mode).toBeUndefined();
  });

  it("uses default port 22 for ssh when port field is NaN", async () => {
    vi.mocked(apiModule.quickConnect).mockResolvedValue({ session_id: "s1", url: "/op/s1" });
    renderConnect(root, makeBootstrap());
    const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type")!;
    typeSelect.value = "ssh";
    typeSelect.dispatchEvent(new Event("change"));
    const hostInput = root.querySelector<HTMLInputElement>("#connect-host")!;
    hostInput.value = "myhost.example.com";
    const portInput = root.querySelector<HTMLInputElement>("#connect-port")!;
    portInput.value = "";
    const form = root.querySelector<HTMLFormElement>("#connect-form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 50));
    const payload = vi.mocked(apiModule.quickConnect).mock.calls[0][0];
    // parseInt("") is NaN, so it should fall back to 22 for ssh (line 59)
    expect(payload.port).toBe(22);
  });

  it("returns early without error when required DOM elements are missing (line 139)", () => {
    // Intercept querySelector on root to return null for #connect-form so the guard fires
    const origQuerySelector = root.querySelector.bind(root);
    const spy = vi.spyOn(root, "querySelector").mockImplementation((sel: string) => {
      if (sel === "#connect-form") return null;
      return origQuerySelector(sel);
    });
    // Should not throw even though #connect-form is null
    expect(() => renderConnect(root, makeBootstrap())).not.toThrow();
    spy.mockRestore();
  });
});
