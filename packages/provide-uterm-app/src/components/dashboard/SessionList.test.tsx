//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { AppBootstrap, SessionSummary } from "../../api/types";
import { useDashboardStore } from "../../stores/dashboardStore";
import { SessionList } from "./SessionList";

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

function resetStore(sessions: SessionSummary[] = []) {
  useDashboardStore.setState({ sessions, filter: "", loading: false, error: null });
}

beforeEach(() => {
  resetStore();
});

afterEach(() => {
  resetStore();
});

describe("SessionList", () => {
  it("renders empty state when no sessions", () => {
    render(<SessionList bootstrap={BOOTSTRAP} filter="" />);
    expect(screen.getByText("No sessions found.")).toBeInTheDocument();
  });

  it("renders session rows", () => {
    resetStore([
      makeSession({ sessionId: "s1", displayName: "Alpha" }),
      makeSession({ sessionId: "s2", displayName: "Beta" }),
    ]);
    render(<SessionList bootstrap={BOOTSTRAP} filter="" />);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Beta")).toBeInTheDocument();
  });

  it("filters by displayName", () => {
    resetStore([
      makeSession({ sessionId: "s1", displayName: "Alpha" }),
      makeSession({ sessionId: "s2", displayName: "Beta" }),
    ]);
    render(<SessionList bootstrap={BOOTSTRAP} filter="alpha" />);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.queryByText("Beta")).toBeNull();
  });

  it("filters by sessionId", () => {
    resetStore([
      makeSession({ sessionId: "abc-123", displayName: "One" }),
      makeSession({ sessionId: "def-456", displayName: "Two" }),
    ]);
    render(<SessionList bootstrap={BOOTSTRAP} filter="abc" />);
    expect(screen.getByText("One")).toBeInTheDocument();
    expect(screen.queryByText("Two")).toBeNull();
  });

  it("filters by connectorType", () => {
    resetStore([
      makeSession({ sessionId: "s1", displayName: "SSH Session", connectorType: "ssh" }),
      makeSession({ sessionId: "s2", displayName: "PTY Session", connectorType: "pty" }),
    ]);
    render(<SessionList bootstrap={BOOTSTRAP} filter="ssh" />);
    expect(screen.getByText("SSH Session")).toBeInTheDocument();
    expect(screen.queryByText("PTY Session")).toBeNull();
  });

  it("filters by tags", () => {
    resetStore([
      makeSession({ sessionId: "s1", displayName: "Prod", tags: ["production"] }),
      makeSession({ sessionId: "s2", displayName: "Dev", tags: ["development"] }),
    ]);
    render(<SessionList bootstrap={BOOTSTRAP} filter="prod" />);
    expect(screen.getByText("Prod")).toBeInTheDocument();
    expect(screen.queryByText("Dev")).toBeNull();
  });

  it("filter is case-insensitive", () => {
    resetStore([makeSession({ sessionId: "s1", displayName: "Alpha" })]);
    render(<SessionList bootstrap={BOOTSTRAP} filter="ALPHA" />);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("shows empty state when filter matches nothing", () => {
    resetStore([makeSession({ sessionId: "s1", displayName: "Alpha" })]);
    render(<SessionList bootstrap={BOOTSTRAP} filter="zzz" />);
    expect(screen.getByText("No sessions found.")).toBeInTheDocument();
  });

  it("sorts errors first, then connected, then stopped", () => {
    resetStore([
      makeSession({ sessionId: "stopped", displayName: "Stopped", connected: false, lastError: null }),
      makeSession({ sessionId: "live", displayName: "Live", connected: true, lastError: null }),
      makeSession({ sessionId: "errored", displayName: "Errored", connected: false, lastError: "fail" }),
    ]);
    const { container } = render(<SessionList bootstrap={BOOTSTRAP} filter="" />);
    const rows = Array.from(container.querySelectorAll("[class*='name']:not([class*='nameRow'])"))
      .map((el) => el.textContent?.trim())
      .filter(Boolean);
    expect(rows[0]).toBe("Errored");
    expect(rows[1]).toBe("Live");
    expect(rows[2]).toBe("Stopped");
  });
});
