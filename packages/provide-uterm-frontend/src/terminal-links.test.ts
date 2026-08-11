//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, describe, expect, it, vi } from "vitest";

import { inertLinkHandler, isSafeTerminalLink, safeLinkHandler } from "./terminal-links.js";

const click = new MouseEvent("click");

afterEach(() => {
  vi.restoreAllMocks();
});

describe("isSafeTerminalLink", () => {
  it("accepts the schemes that can only load a document", () => {
    expect(isSafeTerminalLink("https://example.com/x")).toBe(true);
    expect(isSafeTerminalLink("http://example.com")).toBe(true);
    expect(isSafeTerminalLink("mailto:someone@example.com")).toBe(true);
  });

  it("refuses schemes that execute, read the disk, or carry a payload", () => {
    expect(isSafeTerminalLink("javascript:alert(1)")).toBe(false);
    expect(isSafeTerminalLink("JavaScript:alert(1)")).toBe(false);
    expect(isSafeTerminalLink("data:text/html,<script>alert(1)</script>")).toBe(false);
    expect(isSafeTerminalLink("vbscript:msgbox")).toBe(false);
    expect(isSafeTerminalLink("file:///etc/passwd")).toBe(false);
  });

  it("refuses anything without a scheme to vouch for", () => {
    expect(isSafeTerminalLink("/relative")).toBe(false);
    expect(isSafeTerminalLink("not a url")).toBe(false);
    expect(isSafeTerminalLink("")).toBe(false);
  });
});

describe("safeLinkHandler", () => {
  it("opens a confirmed document link with the opener severed", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    safeLinkHandler(() => true).activate(click, "https://example.com/x");
    expect(open).toHaveBeenCalledWith("https://example.com/x", "_blank", "noopener,noreferrer");
  });

  it("does not open a link the reader declined", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    safeLinkHandler(() => false).activate(click, "https://example.com/x");
    expect(open).not.toHaveBeenCalled();
  });

  it("never asks about an unsafe scheme, so a stray Enter cannot follow one", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const confirm = vi.fn(() => true);
    safeLinkHandler(confirm).activate(click, "javascript:alert(1)");
    expect(confirm).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
  });
});

describe("inertLinkHandler", () => {
  it("follows nothing at all, safe scheme or not", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    inertLinkHandler().activate(click, "https://example.com/x");
    inertLinkHandler().activate(click, "javascript:alert(1)");
    expect(open).not.toHaveBeenCalled();
  });
});
