//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap } from "./api/types";
import { App } from "./App";

function makeBootstrap(page_kind: string): AppBootstrap {
  // Cast through unknown to allow testing with invalid page_kind values
  // (the real AppBootstrap type only allows known kinds).
  return {
    page_kind: page_kind,
    title: "Test App",
    app_path: "/app",
    assets_path: "/assets",
  } as unknown as AppBootstrap;
}

beforeEach(() => {
  // Suppress React's console.error for expected errors thrown by the switch default
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("App routing", () => {
  it("renders ErrorBoundary fallback for an unknown page_kind", () => {
    render(<App bootstrap={makeBootstrap("not_a_real_page")} />);
    // The ErrorBoundary catches the thrown Error and shows its fallback UI
    expect(screen.getByText("Application Error")).toBeInTheDocument();
  });

  it("shows the unknown page_kind in the error message", () => {
    render(<App bootstrap={makeBootstrap("some_future_kind")} />);
    expect(screen.getByText(/some_future_kind/)).toBeInTheDocument();
  });
});
