//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap, SessionSummary } from "../../api/types";
import { useDashboardStore } from "../../stores/dashboardStore";
import { SessionRow } from "./SessionRow";

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

const restartMock = vi.fn();

beforeEach(() => {
  useDashboardStore.setState({ restart: restartMock } as never);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SessionRow", () => {
  it("renders display name", () => {
    render(<SessionRow session={makeSession()} bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("Dev Session")).toBeInTheDocument();
  });

  it("shows Live badge when connected", () => {
    render(<SessionRow session={makeSession({ connected: true })} bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("shows Stopped badge when disconnected with no error", () => {
    render(
      <SessionRow
        session={makeSession({ connected: false, lastError: null })}
        bootstrap={BOOTSTRAP}
      />,
    );
    expect(screen.getByText("Stopped")).toBeInTheDocument();
  });

  it("shows Error badge when lastError is set", () => {
    render(
      <SessionRow
        session={makeSession({ connected: false, lastError: "timeout" })}
        bootstrap={BOOTSTRAP}
      />,
    );
    expect(screen.getByText("Error")).toBeInTheDocument();
  });

  it("shows connector type badge", () => {
    render(<SessionRow session={makeSession({ connectorType: "ssh" })} bootstrap={BOOTSTRAP} />);
    expect(screen.getByText("ssh")).toBeInTheDocument();
  });

  it("shows rec badge when recording is enabled", () => {
    render(
      <SessionRow session={makeSession({ recordingEnabled: true })} bootstrap={BOOTSTRAP} />,
    );
    expect(screen.getByText("rec")).toBeInTheDocument();
  });

  it("does not show rec badge when recording is disabled", () => {
    render(
      <SessionRow session={makeSession({ recordingEnabled: false })} bootstrap={BOOTSTRAP} />,
    );
    expect(screen.queryByText("rec")).toBeNull();
  });

  it("shows visibility badge for non-public sessions", () => {
    render(
      <SessionRow session={makeSession({ visibility: "private" })} bootstrap={BOOTSTRAP} />,
    );
    expect(screen.getByText("private")).toBeInTheDocument();
  });

  it("does not show visibility badge for public sessions", () => {
    render(
      <SessionRow session={makeSession({ visibility: "public" })} bootstrap={BOOTSTRAP} />,
    );
    expect(screen.queryByText("public")).toBeNull();
  });

  it("shows Shared mode for open input", () => {
    render(
      <SessionRow session={makeSession({ inputMode: "open" })} bootstrap={BOOTSTRAP} />,
    );
    expect(screen.getByText(/Shared mode/)).toBeInTheDocument();
  });

  it("shows Exclusive mode for hijack input", () => {
    render(
      <SessionRow session={makeSession({ inputMode: "hijack" })} bootstrap={BOOTSTRAP} />,
    );
    expect(screen.getByText(/Exclusive mode/)).toBeInTheDocument();
  });

  it("shows error text in meta when lastError is set", () => {
    render(
      <SessionRow
        session={makeSession({ lastError: "connection reset" })}
        bootstrap={BOOTSTRAP}
      />,
    );
    expect(screen.getByText(/connection reset/)).toBeInTheDocument();
  });

  it("renders Operate link with correct href", () => {
    render(<SessionRow session={makeSession()} bootstrap={BOOTSTRAP} />);
    const link = screen.getByText("Operate");
    expect(link).toHaveAttribute("href", "/app/operator/s1");
  });

  it("renders View link when connected", () => {
    render(
      <SessionRow session={makeSession({ connected: true })} bootstrap={BOOTSTRAP} />,
    );
    const link = screen.getByText("View");
    expect(link).toHaveAttribute("href", "/app/session/s1");
  });

  it("does not render View link when disconnected", () => {
    render(
      <SessionRow session={makeSession({ connected: false })} bootstrap={BOOTSTRAP} />,
    );
    expect(screen.queryByText("View")).toBeNull();
  });

  it("renders Replay link when recording is available", () => {
    render(
      <SessionRow
        session={makeSession({ recordingAvailable: true })}
        bootstrap={BOOTSTRAP}
      />,
    );
    const link = screen.getByText("Replay");
    expect(link).toHaveAttribute("href", "/app/replay/s1");
  });

  it("does not render Replay link when recording is not available", () => {
    render(
      <SessionRow
        session={makeSession({ recordingAvailable: false })}
        bootstrap={BOOTSTRAP}
      />,
    );
    expect(screen.queryByText("Replay")).toBeNull();
  });

  it("calls restart when Restart button is clicked", () => {
    render(<SessionRow session={makeSession()} bootstrap={BOOTSTRAP} />);
    fireEvent.click(screen.getByText("Restart"));
    expect(restartMock).toHaveBeenCalledWith("s1");
  });

  it("encodes session id in action hrefs", () => {
    render(
      <SessionRow
        session={makeSession({ sessionId: "has spaces" })}
        bootstrap={BOOTSTRAP}
      />,
    );
    const link = screen.getByText("Operate");
    expect(link).toHaveAttribute("href", "/app/operator/has%20spaces");
  });
});
