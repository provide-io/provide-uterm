//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap, SessionSummary } from "../../api/types";
import { useDashboardStore } from "../../stores/dashboardStore";
import { DashboardPage } from "./DashboardPage";

const BOOTSTRAP: AppBootstrap = {
  page_kind: "dashboard",
  title: "Test",
  app_path: "/app",
  assets_path: "/assets",
};

function makeSession(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    sessionId: "s1",
    displayName: "Dev Session",
    connectorType: "pty",
    lifecycleState: "running",
    inputMode: "open",
    connected: true,
    autoStart: false,
    tags: [],
    recordingEnabled: false,
    recordingAvailable: false,
    owner: null,
    visibility: "public",
    lastError: null,
    ...overrides,
  };
}

const refreshMock = vi.fn().mockResolvedValue(undefined);
const setFilterMock = vi.fn();

function resetStore(sessions: SessionSummary[] = [], error: string | null = null) {
  useDashboardStore.setState({
    sessions,
    filter: "",
    loading: false,
    error,
    refresh: refreshMock,
    setFilter: setFilterMock,
  } as never);
}

beforeEach(() => {
  resetStore();
});

afterEach(() => {
  vi.restoreAllMocks();
  resetStore();
});

describe("DashboardPage", () => {
  it("calls refresh on mount", async () => {
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    await waitFor(() => {
      expect(refreshMock).toHaveBeenCalled();
    });
  });

  it("renders the title", () => {
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("Provide Terminal")).toBeInTheDocument();
  });

  it("renders Dashboard crumb via AppHeader", () => {
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
  });

  it("renders Quick connect link", () => {
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    const link = screen.getByText("Quick connect");
    expect(link).toHaveAttribute("href", "/app/connect");
  });

  it("shows healthy status when no errors", () => {
    resetStore([makeSession()]);
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("All systems healthy")).toBeInTheDocument();
  });

  it("shows error count when sessions have errors", () => {
    resetStore([makeSession({ lastError: "timeout" })]);
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("1 error(s)")).toBeInTheDocument();
  });

  it("renders metric cards", () => {
    resetStore([
      makeSession({ sessionId: "s1", connected: true, lifecycleState: "running" }),
      makeSession({ sessionId: "s2", connected: false, lifecycleState: "stopped", recordingEnabled: true }),
    ]);
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(screen.getAllByText("Sessions").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Live").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Errors").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Recording").length).toBeGreaterThanOrEqual(1);
  });

  it("displays error banner when error is set", () => {
    resetStore([], "Something went wrong");
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });

  it("does not display error banner when no error", () => {
    resetStore([]);
    const { container } = render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(container.querySelector("[class*='error']")?.textContent).toBeFalsy();
  });

  it("shows Refresh button", () => {
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("Refresh")).toBeInTheDocument();
  });

  it("shows Loading text when loading", () => {
    useDashboardStore.setState({
      sessions: [],
      filter: "",
      loading: true,
      error: null,
      refresh: refreshMock,
      setFilter: setFilterMock,
    } as never);
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("calls refresh when Refresh button is clicked", () => {
    refreshMock.mockClear();
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    fireEvent.click(screen.getByText("Refresh"));
    // Once on mount + once on click
    expect(refreshMock).toHaveBeenCalledTimes(2);
  });

  it("renders the filter input placeholder", () => {
    render(<DashboardPage bootstrap={BOOTSTRAP} />);
    expect(screen.getByPlaceholderText("Filter sessions...")).toBeInTheDocument();
  });
});
