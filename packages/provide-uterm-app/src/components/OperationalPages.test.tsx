//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap, RecordingEntryView, SessionSummary } from "../api/types";
import { useReplayStore } from "../stores/replayStore";
import { useSessionStore } from "../stores/sessionStore";
import { useTerminalStore } from "../stores/terminalStore";

vi.mock("./widgets/HijackHost", () => ({
  HijackHost: ({ sessionId, surface }: { sessionId: string; surface?: string }) => (
    <div data-testid="hijack-host">{sessionId}:{surface ?? "user"}</div>
  ),
}));
vi.mock("./replay/TimelineCanvas", () => ({ TimelineCanvas: () => <canvas data-testid="timeline" /> }));

import { OperatorPage } from "./operator/OperatorPage";
import { ReplayPage } from "./replay/ReplayPage";
import { SessionPage } from "./session/SessionPage";

const summary: SessionSummary = {
  sessionId: "session-1", displayName: "Primary", connectorType: "ssh", lifecycleState: "running",
  inputMode: "open", connected: true, autoStart: false, tags: [], recordingEnabled: true,
  recordingAvailable: true, owner: null, visibility: "private", lastError: null,
};
const base: AppBootstrap = {
  page_kind: "session", title: "Terminal", app_path: "/app", assets_path: "/assets", session_id: "session-1",
};
const entry: RecordingEntryView = { ts: 1, event: "read", payload: { data: "hello" }, screen: "hello" };

beforeEach(() => {
  useSessionStore.setState({ summary, loading: false, error: null, load: vi.fn() });
  useTerminalStore.setState({ mounted: true, cols: 120, rows: 40, error: null });
  useReplayStore.setState({ entries: [entry], index: 0, loading: false, error: null, load: vi.fn() });
});

describe("operational pages", () => {
  it("loads and renders a user session with connectivity metadata", () => {
    render(<SessionPage bootstrap={base} />);
    expect(useSessionStore.getState().load).toHaveBeenCalledWith("session-1");
    expect(screen.getByTestId("hijack-host")).toHaveTextContent("session-1:user");
    expect(screen.getByText("40×120")).toBeInTheDocument();
    expect(screen.getByText("Shared")).toBeInTheDocument();
  });

  it("loads and renders the operator surface with recording state", () => {
    render(<OperatorPage bootstrap={{ ...base, page_kind: "operator" }} />);
    expect(useSessionStore.getState().load).toHaveBeenCalledWith("session-1");
    expect(screen.getByTestId("hijack-host")).toHaveTextContent("session-1:operator");
    expect(screen.getByText("recording")).toBeInTheDocument();
    expect(screen.getAllByText("Connected").length).toBeGreaterThan(0);
  });

  it("loads and renders replay details and event count", () => {
    render(<ReplayPage bootstrap={{ ...base, page_kind: "replay" }} />);
    expect(useReplayStore.getState().load).toHaveBeenCalledWith("session-1");
    expect(screen.getByText("1 events")).toBeInTheDocument();
    expect(screen.getAllByText("hello", { exact: false })).toHaveLength(2);
    expect(screen.getByTestId("timeline")).toBeInTheDocument();
  });

  it("rejects operational pages without a session id", () => {
    const invalid = { ...base, session_id: undefined };
    expect(() => render(<SessionPage bootstrap={invalid} />)).toThrow("session bootstrap missing session_id");
    expect(() => render(<OperatorPage bootstrap={{ ...invalid, page_kind: "operator" }} />)).toThrow(
      "operator bootstrap missing session_id",
    );
    expect(() => render(<ReplayPage bootstrap={{ ...invalid, page_kind: "replay" }} />)).toThrow(
      "replay bootstrap missing session_id",
    );
  });
});
