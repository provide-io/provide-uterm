//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("renders children text", () => {
    render(<StatusBadge tone="ok">Connected</StatusBadge>);
    expect(screen.getByText("Connected")).toBeInTheDocument();
  });

  it("renders as a span element", () => {
    render(<StatusBadge tone="info">Status</StatusBadge>);
    const element = screen.getByText("Status");
    expect(element.tagName).toBe("SPAN");
  });

  it("applies ok tone colors", () => {
    render(<StatusBadge tone="ok">OK</StatusBadge>);
    const el = screen.getByText("OK");
    expect(el.style.background).toBe("var(--bg-success)");
    expect(el.style.color).toBe("var(--text-success)");
  });

  it("applies error tone colors", () => {
    render(<StatusBadge tone="error">Error</StatusBadge>);
    const el = screen.getByText("Error");
    expect(el.style.background).toBe("var(--bg-danger)");
    expect(el.style.color).toBe("var(--text-danger)");
  });

  it("applies info tone colors", () => {
    render(<StatusBadge tone="info">Info</StatusBadge>);
    const el = screen.getByText("Info");
    expect(el.style.background).toBe("var(--bg-info)");
    expect(el.style.color).toBe("var(--text-info)");
  });

  it("applies warning tone colors", () => {
    render(<StatusBadge tone="warning">Warn</StatusBadge>);
    const el = screen.getByText("Warn");
    expect(el.style.background).toBe("var(--bg-warning)");
    expect(el.style.color).toBe("var(--text-warning)");
  });

  it("applies neutral tone with transparent bg", () => {
    render(<StatusBadge tone="neutral">N/A</StatusBadge>);
    const el = screen.getByText("N/A");
    expect(el.style.background).toBe("transparent");
    expect(el.style.color).toBe("var(--text-tertiary)");
  });

  it("applies border on neutral tone", () => {
    render(<StatusBadge tone="neutral">Border</StatusBadge>);
    const el = screen.getByText("Border");
    expect(el.style.border).toContain("var(--border-primary)");
  });

  it("has no border on non-neutral tones", () => {
    render(<StatusBadge tone="ok">NoBorder</StatusBadge>);
    const el = screen.getByText("NoBorder");
    expect(el.style.border).not.toContain("var(--border-primary)");
  });

  it("applies glow box-shadow when glow is true", () => {
    render(<StatusBadge tone="ok" glow>Glow</StatusBadge>);
    const el = screen.getByText("Glow");
    expect(el.style.boxShadow).toContain("var(--success)");
  });

  it("no box-shadow when glow is false", () => {
    render(<StatusBadge tone="ok">NoGlow</StatusBadge>);
    const el = screen.getByText("NoGlow");
    expect(el.style.boxShadow).toBe("");
  });

  it("no box-shadow on neutral even with glow", () => {
    render(<StatusBadge tone="neutral" glow>NeutralGlow</StatusBadge>);
    const el = screen.getByText("NeutralGlow");
    // Neutral has no glowColor, so boxShadow should be empty
    expect(el.style.boxShadow).toBe("");
  });
});
