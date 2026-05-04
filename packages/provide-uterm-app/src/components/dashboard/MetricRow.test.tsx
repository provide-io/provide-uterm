//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MetricRow } from "./MetricRow";

describe("MetricRow", () => {
  it("renders Sessions metric", () => {
    render(<MetricRow total={10} live={3} errors={1} recording={5} />);
    expect(screen.getByText("Sessions")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
  });

  it("renders Live metric", () => {
    render(<MetricRow total={10} live={3} errors={1} recording={5} />);
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("renders Errors metric", () => {
    render(<MetricRow total={10} live={3} errors={1} recording={5} />);
    expect(screen.getByText("Errors")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("renders Recording metric", () => {
    render(<MetricRow total={10} live={3} errors={1} recording={5} />);
    expect(screen.getByText("Recording")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  it("renders zero counts", () => {
    render(<MetricRow total={0} live={0} errors={0} recording={0} />);
    const zeroes = screen.getAllByText("0");
    expect(zeroes).toHaveLength(4);
  });

  it("wraps in metric-grid div", () => {
    const { container } = render(
      <MetricRow total={1} live={1} errors={0} recording={0} />,
    );
    expect(container.querySelector(".metric-grid")).toBeTruthy();
  });
});
