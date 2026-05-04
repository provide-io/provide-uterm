//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FilterInput } from "./FilterInput";

describe("FilterInput", () => {
  it("renders an input element", () => {
    render(<FilterInput value="" onChange={() => {}} />);
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("displays the provided value", () => {
    render(<FilterInput value="ssh" onChange={() => {}} />);
    expect(screen.getByDisplayValue("ssh")).toBeInTheDocument();
  });

  it("uses default placeholder", () => {
    render(<FilterInput value="" onChange={() => {}} />);
    expect(screen.getByPlaceholderText("Filter...")).toBeInTheDocument();
  });

  it("uses custom placeholder", () => {
    render(
      <FilterInput value="" onChange={() => {}} placeholder="Search sessions" />,
    );
    expect(screen.getByPlaceholderText("Search sessions")).toBeInTheDocument();
  });

  it("calls onChange when value changes", () => {
    const onChange = vi.fn();
    render(<FilterInput value="" onChange={onChange} />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "ssh" } });
    expect(onChange).toHaveBeenCalledWith("ssh");
  });

  it("has type=text", () => {
    render(<FilterInput value="" onChange={() => {}} />);
    const input = screen.getByRole("textbox");
    expect(input).toHaveAttribute("type", "text");
  });

  it("has filter-input className", () => {
    render(<FilterInput value="" onChange={() => {}} />);
    const input = screen.getByRole("textbox");
    expect(input).toHaveClass("filter-input");
  });
});
