//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { describe, expect, it } from "vitest";
import {
  approvalElementClass,
  buildApprovalModalHtml,
  buildApprovalStatusBarHtml,
  computeRemainingSeconds,
} from "./hijack-approval.js";

describe("approvalElementClass", () => {
  it("maps modal to hijack-approval-modal", () => {
    expect(approvalElementClass("modal")).toBe("hijack-approval-modal");
  });

  it("maps statusbar to hijack-approval-statusbar", () => {
    expect(approvalElementClass("statusbar")).toBe("hijack-approval-statusbar");
  });
});

describe("buildApprovalModalHtml", () => {
  it("escapes the command", () => {
    const html = buildApprovalModalHtml({ uid: 1, command: "<script>x</script>", isAdmin: false });
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
  });

  it("includes admin actions when isAdmin", () => {
    const html = buildApprovalModalHtml({ uid: 3, command: "ls", isAdmin: true });
    expect(html).toContain('id="h-3-approve"');
    expect(html).toContain('id="h-3-reject"');
  });

  it("omits admin actions when not admin", () => {
    const html = buildApprovalModalHtml({ uid: 3, command: "ls", isAdmin: false });
    expect(html).not.toContain('id="h-3-approve"');
  });
});

describe("buildApprovalStatusBarHtml", () => {
  it("includes the uid-scoped timer ID", () => {
    const html = buildApprovalStatusBarHtml({ uid: 5 });
    expect(html).toContain('id="h-5-approval-timer"');
  });
});

describe("computeRemainingSeconds", () => {
  it("rounds to the nearest second", () => {
    expect(computeRemainingSeconds(100, 99_400)).toBe(1);
    expect(computeRemainingSeconds(100, 90_000)).toBe(10);
    expect(computeRemainingSeconds(100, 99_600)).toBe(0);
  });

  it("clamps at zero", () => {
    expect(computeRemainingSeconds(5, 999_999_999)).toBe(0);
  });

  it("defaults nowMs to Date.now()", () => {
    const future = Date.now() / 1000 + 1000;
    const r = computeRemainingSeconds(future);
    expect(r).toBeGreaterThan(0);
  });
});
