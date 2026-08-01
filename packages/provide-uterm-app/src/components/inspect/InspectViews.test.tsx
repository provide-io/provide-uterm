//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { HttpExchangeEntry } from "../../api/types";
import { useInspectStore } from "../../stores/inspectStore";
import { InspectDetail } from "./InspectDetail";
import { InspectList } from "./InspectList";
import { InspectToolbar } from "./InspectToolbar";

const exchange: HttpExchangeEntry = {
  id: "req-1",
  request: {
    type: "http_req",
    id: "req-1",
    ts: 1,
    method: "POST",
    url: "https://example.test/upload",
    headers: { "content-type": "text/plain" },
    body_size: 5,
    body_b64: btoa("hello"),
    intercepted: true,
  },
  response: null,
  intercepted: true,
  interceptResolved: false,
  interceptAction: null,
};

beforeEach(() => useInspectStore.getState().clear());

describe("inspect operational views", () => {
  it("renders captured request state and selects a request", () => {
    const onSelect = vi.fn();
    const { rerender } = render(<InspectList exchanges={[]} selected={null} onSelect={onSelect} />);
    expect(screen.getByText("No requests captured yet.")).toBeInTheDocument();

    rerender(<InspectList exchanges={[exchange]} selected="req-1" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onSelect).toHaveBeenCalledWith("req-1");
    expect(screen.getByText("PAUSED")).toBeInTheDocument();
  });

  it("forwards, drops, and modifies intercepted requests", () => {
    const onAction = vi.fn();
    render(<InspectDetail exchange={exchange} onAction={onAction} />);
    fireEvent.click(screen.getByRole("button", { name: "Forward" }));
    fireEvent.click(screen.getByRole("button", { name: "Drop" }));
    fireEvent.click(screen.getByRole("button", { name: "Modify & Forward" }));
    const textboxes = screen.getAllByRole("textbox");
    fireEvent.change(textboxes[textboxes.length - 1] as HTMLTextAreaElement, { target: { value: "changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Send Modified" }));

    expect(onAction).toHaveBeenNthCalledWith(1, "req-1", "forward");
    expect(onAction).toHaveBeenNthCalledWith(2, "req-1", "drop");
    expect(onAction).toHaveBeenNthCalledWith(
      3,
      "req-1",
      "modify",
      { "content-type": "text/plain" },
      btoa("changed"),
    );
  });

  it("renders response metadata and binary/truncated bodies", () => {
    const completed: HttpExchangeEntry = {
      ...exchange,
      intercepted: false,
      response: {
        type: "http_res",
        id: "req-1",
        ts: 2,
        status: 503,
        status_text: "Unavailable",
        headers: {},
        body_size: 2048,
        body_truncated: true,
        duration_ms: 42.4,
      },
    };
    render(<InspectDetail exchange={completed} onAction={vi.fn()} />);
    expect(screen.getByText(/503 Unavailable/)).toBeInTheDocument();
    expect(screen.getByText("(truncated, 2.0KB)")).toBeInTheDocument();
    expect(screen.getByText("none")).toBeInTheDocument();
  });

  it("updates method and URL filters and reports websocket state", () => {
    useInspectStore.setState({ wsStatus: "connected", inspectEnabled: true, interceptEnabled: false });
    const toggleInspect = vi.fn();
    const toggleIntercept = vi.fn();
    render(
      <InspectToolbar onToggleInspect={toggleInspect} onToggleIntercept={toggleIntercept} filteredCount={1} />,
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "POST" } });
    fireEvent.change(screen.getByPlaceholderText("Filter URL..."), { target: { value: "upload" } });
    fireEvent.click(screen.getByRole("button", { name: "Inspect: ON" }));
    fireEvent.click(screen.getByRole("button", { name: "Intercept: OFF" }));

    expect(useInspectStore.getState()).toMatchObject({ methodFilter: "POST", urlFilter: "upload" });
    expect(toggleInspect).toHaveBeenCalledOnce();
    expect(toggleIntercept).toHaveBeenCalledOnce();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("1 request")).toBeInTheDocument();
  });
});
