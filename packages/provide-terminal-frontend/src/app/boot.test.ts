//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./router.js", () => ({
  routeApp: vi.fn().mockResolvedValue(undefined),
}));

import { bootApp } from "./boot.js";
import * as routerModule from "./router.js";

function setupBootstrapScript(data: Record<string, unknown>): HTMLScriptElement {
  const script = document.createElement("script");
  script.type = "application/json";
  script.id = "app-bootstrap";
  script.textContent = JSON.stringify(data);
  document.body.appendChild(script);
  return script;
}

function setupAppRoot(): HTMLElement {
  const root = document.createElement("div");
  root.id = "app-root";
  document.body.appendChild(root);
  return root;
}

afterEach(() => {
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("bootApp", () => {
  it("throws when #app-root is missing", async () => {
    setupBootstrapScript({
      page_kind: "dashboard",
      title: "T",
      app_path: "/",
      assets_path: "/a",
    });
    await expect(bootApp()).rejects.toThrow("Missing #app-root");
  });

  it("throws when #app-bootstrap script is missing", async () => {
    setupAppRoot();
    await expect(bootApp()).rejects.toThrow("Missing #app-bootstrap payload");
  });

  it("throws when app-bootstrap is not a script element", async () => {
    setupAppRoot();
    const div = document.createElement("div");
    div.id = "app-bootstrap";
    document.body.appendChild(div);
    await expect(bootApp()).rejects.toThrow("Missing #app-bootstrap payload");
  });

  it("throws on invalid page_kind", async () => {
    setupAppRoot();
    setupBootstrapScript({
      page_kind: "unknown",
      title: "T",
      app_path: "/",
      assets_path: "/a",
    });
    await expect(bootApp()).rejects.toThrow("Invalid page bootstrap");
  });

  it("throws when title is missing", async () => {
    setupAppRoot();
    setupBootstrapScript({
      page_kind: "dashboard",
      app_path: "/",
      assets_path: "/a",
    });
    await expect(bootApp()).rejects.toThrow("Incomplete page bootstrap");
  });

  it("throws when app_path is missing", async () => {
    setupAppRoot();
    setupBootstrapScript({
      page_kind: "dashboard",
      title: "T",
      assets_path: "/a",
    });
    await expect(bootApp()).rejects.toThrow("Incomplete page bootstrap");
  });

  it("throws when assets_path is missing", async () => {
    setupAppRoot();
    setupBootstrapScript({
      page_kind: "dashboard",
      title: "T",
      app_path: "/",
    });
    await expect(bootApp()).rejects.toThrow("Incomplete page bootstrap");
  });

  it("calls routeApp with root and bootstrap for dashboard", async () => {
    const root = setupAppRoot();
    setupBootstrapScript({
      page_kind: "dashboard",
      title: "My App",
      app_path: "/app",
      assets_path: "/assets",
    });
    await bootApp();
    expect(routerModule.routeApp).toHaveBeenCalledWith(root, {
      page_kind: "dashboard",
      title: "My App",
      app_path: "/app",
      assets_path: "/assets",
    });
  });

  it("accepts all valid page_kinds", async () => {
    const validKinds = ["dashboard", "session", "operator", "replay", "connect"] as const;
    for (const kind of validKinds) {
      document.body.innerHTML = "";
      vi.clearAllMocks();
      setupAppRoot();
      setupBootstrapScript({
        page_kind: kind,
        title: "T",
        app_path: "/",
        assets_path: "/a",
      });
      await bootApp();
      expect(routerModule.routeApp).toHaveBeenCalled();
    }
  });

  it("handles empty textContent as empty object gracefully (throws invalid bootstrap)", async () => {
    setupAppRoot();
    const script = document.createElement("script");
    script.type = "application/json";
    script.id = "app-bootstrap";
    script.textContent = "";
    document.body.appendChild(script);
    await expect(bootApp()).rejects.toThrow("Invalid page bootstrap");
  });
});
