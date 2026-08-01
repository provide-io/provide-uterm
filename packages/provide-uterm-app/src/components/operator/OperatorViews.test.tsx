//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { restartSession } from "../../api/sessions";
import type { AppBootstrap, SessionSummary } from "../../api/types";
import { useSessionStore } from "../../stores/sessionStore";
import { ModeToggle } from "./ModeToggle";
import { OperatorSidebar } from "./OperatorSidebar";
import { SessionMeta } from "./SessionMeta";

vi.mock("../../api/sessions", () => ({ restartSession: vi.fn() }));

const summary: SessionSummary = {
  sessionId: "s/1",
  displayName: "Production shell",
  connectorType: "ssh",
  lifecycleState: "running",
  inputMode: "hijack",
  connected: true,
  autoStart: true,
  tags: ["production", "oncall"],
  recordingEnabled: true,
  recordingAvailable: true,
  owner: "alice",
  visibility: "private",
  lastError: null,
};

const bootstrap: AppBootstrap = {
  page_kind: "operator",
  title: "Operator",
  app_path: "/app",
  assets_path: "/assets",
  session_id: "s/1",
};

beforeEach(() => {
  vi.clearAllMocks();
  useSessionStore.setState({
    summary,
    analysis: "screen is healthy",
    modePending: false,
    utilityPending: false,
    switchMode: vi.fn(),
    clear: vi.fn(),
    analyze: vi.fn(),
  });
});

describe("operator workflow", () => {
  it("exposes both input modes and honors the disabled state", () => {
    const onChange = vi.fn();
    const { rerender } = render(<ModeToggle mode="hijack" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Shared" }));
    expect(onChange).toHaveBeenCalledWith("open");

    rerender(<ModeToggle mode="open" disabled onChange={onChange} />);
    expect(screen.getByRole("button", { name: "Exclusive" })).toBeDisabled();
  });

  it("renders lifecycle metadata and tags", () => {
    render(<SessionMeta summary={summary} />);
    expect(screen.getByText("ssh")).toBeInTheDocument();
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText("production")).toBeInTheDocument();
    expect(screen.getByText("yes")).toHaveStyle({ color: "var(--text-success)" });
  });

  it("dispatches mode, analysis, clear, replay, and restart operations", async () => {
    vi.mocked(restartSession).mockReturnValue(new Promise(() => {}));
    render(<OperatorSidebar sessionId="s/1" bootstrap={bootstrap} />);

    fireEvent.click(screen.getByRole("button", { name: "Shared" }));
    fireEvent.click(screen.getByRole("button", { name: "Analyze screen" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear runtime" }));
    fireEvent.click(screen.getByRole("button", { name: "Restart session" }));

    expect(useSessionStore.getState().switchMode).toHaveBeenCalledWith("s/1", "open");
    expect(useSessionStore.getState().analyze).toHaveBeenCalledWith("s/1");
    expect(useSessionStore.getState().clear).toHaveBeenCalledWith("s/1");
    expect(screen.getByRole("link", { name: "View replay" })).toHaveAttribute("href", "/app/replay/s%2F1");
    expect(restartSession).toHaveBeenCalledWith("s/1");
  });
});
