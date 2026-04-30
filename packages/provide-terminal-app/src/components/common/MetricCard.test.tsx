//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MetricCard } from "./MetricCard";

describe("MetricCard", () => {
  it("renders the label", () => {
    render(<MetricCard label="Active" value={5} />);
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("renders the numeric value", () => {
    render(<MetricCard label="Count" value={42} />);
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("renders value 0", () => {
    render(<MetricCard label="Empty" value={0} />);
    expect(screen.getByText("0")).toBeInTheDocument();
  });

  it("applies default color to value", () => {
    render(<MetricCard label="Test" value={1} />);
    const value = screen.getByText("1");
    expect(value.style.color).toBe("var(--text-primary)");
  });

  it("applies custom color to value", () => {
    render(<MetricCard label="Errors" value={3} color="red" />);
    const value = screen.getByText("3");
    expect(value.style.color).toBe("red");
  });

  it("has card className on wrapper", () => {
    const { container } = render(<MetricCard label="Test" value={1} />);
    expect(container.querySelector(".card")).toBeInTheDocument();
  });

  it("has card-label className on label", () => {
    const { container } = render(<MetricCard label="Sessions" value={10} />);
    expect(container.querySelector(".card-label")).toBeInTheDocument();
  });

  it("has card-value className on value", () => {
    const { container } = render(<MetricCard label="Sessions" value={10} />);
    expect(container.querySelector(".card-value")).toBeInTheDocument();
  });
});
