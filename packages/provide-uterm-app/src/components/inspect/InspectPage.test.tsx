//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap, HttpRequestEntry } from "../../api/types";
import { useInspectStore } from "../../stores/inspectStore";

const sendJson = vi.hoisted(() => vi.fn());
vi.mock("./useInspectWs", () => ({ useInspectWs: () => ({ sendJson }) }));
import { InspectPage } from "./InspectPage";

const bootstrap: AppBootstrap = {
  page_kind: "inspect", title: "Inspect", app_path: "/app", assets_path: "/assets", session_id: "session-1",
};
const request = (id: string, method: string, url: string, intercepted = false): HttpRequestEntry => ({
  type: "http_req", id, ts: 1, method, url, headers: {}, body_size: 0, intercepted,
});

beforeEach(() => {
  sendJson.mockClear();
  useInspectStore.getState().clear();
  useInspectStore.getState().addRequest(request("one", "GET", "https://example.test/users"));
  useInspectStore.getState().addRequest(request("two", "POST", "https://example.test/upload", true));
});

describe("InspectPage", () => {
  it("requires a session id", () => {
    expect(() => render(<InspectPage bootstrap={{ ...bootstrap, session_id: undefined }} />)).toThrow(
      "inspect bootstrap missing session_id",
    );
  });

  it("filters exchanges, selects details, and resolves an intercepted action", () => {
    render(<InspectPage bootstrap={bootstrap} />);
    expect(screen.getByText("2 requests")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "POST" } });
    fireEvent.change(screen.getByPlaceholderText("Filter URL..."), { target: { value: "UPLOAD" } });
    expect(screen.getByText("1 request")).toBeInTheDocument();
    fireEvent.click(screen.getByText("https://example.test/upload"));
    fireEvent.click(screen.getByRole("button", { name: "Forward" }));
    expect(sendJson).toHaveBeenCalledWith({ type: "http_action", id: "two", action: "forward" });
    expect(useInspectStore.getState().exchanges[1]).toMatchObject({ interceptResolved: true, interceptAction: "forward" });
  });

  it("turns interception off whenever inspection is disabled", () => {
    useInspectStore.setState({ inspectEnabled: true, interceptEnabled: true });
    render(<InspectPage bootstrap={bootstrap} />);
    fireEvent.click(screen.getByRole("button", { name: "Inspect: ON" }));
    expect(useInspectStore.getState()).toMatchObject({ inspectEnabled: false, interceptEnabled: false });
    expect(sendJson).toHaveBeenNthCalledWith(1, { type: "http_inspect_toggle", enabled: false });
    expect(sendJson).toHaveBeenNthCalledWith(2, { type: "http_intercept_toggle", enabled: false });
  });

  it("enables interception independently while inspection remains active", () => {
    render(<InspectPage bootstrap={bootstrap} />);
    fireEvent.click(screen.getByRole("button", { name: "Intercept: OFF" }));
    expect(useInspectStore.getState().interceptEnabled).toBe(true);
    expect(sendJson).toHaveBeenCalledWith({ type: "http_intercept_toggle", enabled: true });
  });

  it("clears captured exchanges on unmount", () => {
    const view = render(<InspectPage bootstrap={bootstrap} />);
    view.unmount();
    expect(useInspectStore.getState().exchanges).toEqual([]);
  });
});
