//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  _resetHijackCssState,
  buildHijackToolbarHtml,
  escapeHijackHtml,
  injectHijackCss,
  MOBILE_KEYS,
} from "./hijack-ui.js";

beforeEach(() => {
  _resetHijackCssState();
  document.head.innerHTML = "";
});

afterEach(() => {
  _resetHijackCssState();
  document.head.innerHTML = "";
});

describe("escapeHijackHtml", () => {
  it("escapes HTML special characters", () => {
    expect(escapeHijackHtml("<script>alert(1)</script>")).toBe("&lt;script&gt;alert(1)&lt;/script&gt;");
  });

  it("coerces non-strings via String()", () => {
    expect(escapeHijackHtml(42)).toBe("42");
    expect(escapeHijackHtml(null)).toBe("null");
  });
});

describe("injectHijackCss", () => {
  it("appends a stylesheet link the first time", () => {
    injectHijackCss("https://cdn.example.test/assets/");
    const links = document.head.querySelectorAll("link[rel=stylesheet]");
    expect(links).toHaveLength(1);
    expect((links[0] as HTMLLinkElement).href).toBe("https://cdn.example.test/assets/hijack.css");
  });

  it("is idempotent", () => {
    injectHijackCss("https://cdn.example.test/assets/");
    injectHijackCss("https://cdn.example.test/assets/");
    expect(document.head.querySelectorAll("link").length).toBe(1);
  });
});

describe("buildHijackToolbarHtml", () => {
  it("includes title and uid-scoped IDs", () => {
    const html = buildHijackToolbarHtml({ uid: 7, title: "demo", showAnalysis: false });
    expect(html).toContain(">demo<");
    expect(html).toContain('id="h-7-hijack"');
    expect(html).toContain('id="h-7-mobilekeys"');
  });

  it("escapes the title", () => {
    const html = buildHijackToolbarHtml({ uid: 1, title: "<b>x</b>", showAnalysis: false });
    expect(html).toContain("&lt;b&gt;x&lt;/b&gt;");
  });

  it("includes the analysis section when showAnalysis is true", () => {
    const html = buildHijackToolbarHtml({ uid: 9, title: "t", showAnalysis: true });
    expect(html).toContain('id="h-9-analysis"');
    expect(html).toContain('id="h-9-analysistext"');
  });

  it("omits the analysis section when showAnalysis is false", () => {
    const html = buildHijackToolbarHtml({ uid: 9, title: "t", showAnalysis: false });
    expect(html).not.toContain("analysistext");
  });
});

describe("MOBILE_KEYS", () => {
  it("starts with ESC", () => {
    expect(MOBILE_KEYS[0]).toEqual({ label: "ESC", data: "\x1b" });
  });

  it("contains arrow keys", () => {
    const labels = MOBILE_KEYS.map((k) => k.label);
    expect(labels).toEqual(expect.arrayContaining(["↑", "↓", "→", "←"]));
  });
});
