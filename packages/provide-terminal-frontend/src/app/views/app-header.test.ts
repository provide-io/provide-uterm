//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import type { AppBootstrap } from "../types.js";
import { renderAppHeader } from "./app-header.js";

function makeBootstrap(overrides: Partial<AppBootstrap> = {}): AppBootstrap {
  return {
    page_kind: "dashboard",
    title: "Test App",
    app_path: "/app",
    assets_path: "/assets",
    ...overrides,
  };
}

describe("renderAppHeader", () => {
  it("renders header with nav links", () => {
    const html = renderAppHeader(makeBootstrap(), "dashboard");
    expect(html).toContain('<header class="app-header card">');
    expect(html).toContain("Dashboard");
    expect(html).toContain("Quick Connect");
  });

  it("marks dashboard link as active when active=dashboard", () => {
    const html = renderAppHeader(makeBootstrap(), "dashboard");
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, "text/html");
    const dashLink = doc.querySelector('a[href="/app/"]');
    expect(dashLink?.classList.contains("active")).toBe(true);
    const connectLink = doc.querySelector('a[href="/app/connect"]');
    expect(connectLink?.classList.contains("active")).toBe(false);
  });

  it("marks connect link as active when active=connect", () => {
    const html = renderAppHeader(makeBootstrap(), "connect");
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, "text/html");
    const connectLink = doc.querySelector('a[href="/app/connect"]');
    expect(connectLink?.classList.contains("active")).toBe(true);
    const dashLink = doc.querySelector('a[href="/app/"]');
    expect(dashLink?.classList.contains("active")).toBe(false);
  });

  it("does not mark any as active for session tab", () => {
    const html = renderAppHeader(makeBootstrap(), "session");
    expect(html).not.toContain("app-nav-link active");
  });

  it("escapes HTML special chars in app_path", () => {
    const bootstrap = makeBootstrap({ app_path: '/app?a=1&b=<x>"y' });
    const html = renderAppHeader(bootstrap, "dashboard");
    expect(html).not.toContain("<x>");
    expect(html).toContain("&lt;x&gt;");
    expect(html).toContain("&amp;");
    expect(html).toContain("&quot;");
  });

  it("uses app_path in nav link hrefs", () => {
    const html = renderAppHeader(makeBootstrap({ app_path: "/myapp" }), "dashboard");
    expect(html).toContain('href="/myapp/"');
    expect(html).toContain('href="/myapp/connect"');
  });
});
