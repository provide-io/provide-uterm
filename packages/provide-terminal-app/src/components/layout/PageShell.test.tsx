//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PageShell } from "./PageShell";

describe("PageShell", () => {
  it("renders children", () => {
    render(
      <PageShell>
        <div>Content inside shell</div>
      </PageShell>,
    );
    expect(screen.getByText("Content inside shell")).toBeInTheDocument();
  });

  it("wraps children in a div with page-shell class", () => {
    render(
      <PageShell>
        <span>Inner</span>
      </PageShell>,
    );
    const wrapper = screen.getByText("Inner").parentElement;
    expect(wrapper).toHaveClass("page-shell");
  });

  it("renders multiple children", () => {
    render(
      <PageShell>
        <div>First</div>
        <div>Second</div>
      </PageShell>,
    );
    expect(screen.getByText("First")).toBeInTheDocument();
    expect(screen.getByText("Second")).toBeInTheDocument();
  });
});
