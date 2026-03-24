//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap, SessionSummary } from "../types.js";

vi.mock("../api.js", () => ({
  deleteSession: vi.fn(),
  restartSession: vi.fn(),
}));
vi.mock("../state.js", () => ({
  loadDashboardState: vi.fn(),
  summarizeSessions: vi.fn(),
}));

import * as apiModule from "../api.js";
import * as stateModule from "../state.js";
import { renderDashboard } from "./dashboard-view.js";

function makeBootstrap(): AppBootstrap {
  return {
    page_kind: "dashboard",
    title: "Dashboard",
    app_path: "/app",
    assets_path: "/assets",
  };
}

function makeSummary(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    sessionId: "sess-1",
    displayName: "Test",
    connectorType: "shell",
    lifecycleState: "running",
    inputMode: "hijack",
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

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
  vi.mocked(stateModule.summarizeSessions).mockImplementation((sessions) => ({
    running: sessions.filter((s) => s.connected && s.lifecycleState === "running"),
    stopped: sessions.filter((s) => !s.connected && s.lifecycleState !== "error"),
    degraded: sessions.filter((s) => s.lifecycleState === "error" || s.lastError !== null),
  }));
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("renderDashboard", () => {
  it("renders dashboard shell with heading and status", async () => {
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([]);
    await renderDashboard(root, makeBootstrap());
    expect(root.querySelector("#dashboard-status")).toBeTruthy();
    expect(root.querySelector("#dashboard-content")).toBeTruthy();
    expect(root.querySelector("#dashboard-refresh")).toBeTruthy();
  });

  it("loads and displays sessions on init", async () => {
    const session = makeSummary();
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    await renderDashboard(root, makeBootstrap());
    const status = root.querySelector<HTMLElement>("#dashboard-status")!;
    expect(status.textContent).toContain("1 session(s) loaded");
  });

  it("shows error status when load fails", async () => {
    vi.mocked(stateModule.loadDashboardState).mockRejectedValue(new Error("network error"));
    await renderDashboard(root, makeBootstrap());
    const status = root.querySelector<HTMLElement>("#dashboard-status")!;
    expect(status.textContent).toContain("Dashboard failed to load");
  });

  it("reloads sessions on refresh button click", async () => {
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([]);
    await renderDashboard(root, makeBootstrap());
    vi.clearAllMocks();
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([makeSummary()]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [makeSummary()],
      stopped: [],
      degraded: [],
    });
    const refreshBtn = root.querySelector<HTMLButtonElement>("#dashboard-refresh")!;
    refreshBtn.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(stateModule.loadDashboardState).toHaveBeenCalled();
  });

  it("renders session cards with restart and delete buttons", async () => {
    const session = makeSummary({ sessionId: "my-session" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    await renderDashboard(root, makeBootstrap());
    expect(root.querySelector(".btn-restart")).toBeTruthy();
    expect(root.querySelector(".btn-delete")).toBeTruthy();
  });

  it("renders sessions with tags", async () => {
    const session = makeSummary({ tags: ["game", "prod"], sessionId: "s1" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    await renderDashboard(root, makeBootstrap());
    expect(root.querySelector(".tag-list")).toBeTruthy();
    expect(root.innerHTML).toContain("game");
  });

  it("shows recording badge when recording_enabled", async () => {
    const session = makeSummary({ recordingEnabled: true });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    await renderDashboard(root, makeBootstrap());
    expect(root.innerHTML).toContain("badge-rec");
  });

  it("shows recording-available badge when available but not enabled", async () => {
    const session = makeSummary({ recordingEnabled: false, recordingAvailable: true });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    await renderDashboard(root, makeBootstrap());
    expect(root.innerHTML).toContain("badge-rec-avail");
  });

  it("shows visibility badge for non-public sessions", async () => {
    const session = makeSummary({ visibility: "private" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    await renderDashboard(root, makeBootstrap());
    expect(root.innerHTML).toContain("badge-visibility");
  });

  it("calls restartSession when restart button clicked", async () => {
    const session = makeSummary({ sessionId: "sess-restart" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    vi.mocked(apiModule.restartSession).mockResolvedValue(session);
    await renderDashboard(root, makeBootstrap());
    const restartBtn = root.querySelector<HTMLButtonElement>(".btn-restart")!;
    restartBtn.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(apiModule.restartSession).toHaveBeenCalledWith("sess-restart");
  });

  it("shows error in status when restart fails", async () => {
    const session = makeSummary({ sessionId: "sess-fail" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    vi.mocked(apiModule.restartSession).mockRejectedValue(new Error("restart failed"));
    await renderDashboard(root, makeBootstrap());
    const restartBtn = root.querySelector<HTMLButtonElement>(".btn-restart")!;
    restartBtn.click();
    await new Promise((r) => setTimeout(r, 20));
    const status = root.querySelector<HTMLElement>("#dashboard-status")!;
    expect(status.textContent).toContain("Restart failed");
  });

  it("calls deleteSession when delete confirmed", async () => {
    const session = makeSummary({ sessionId: "sess-del" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    vi.mocked(apiModule.deleteSession).mockResolvedValue(undefined);
    vi.stubGlobal("confirm", () => true);
    await renderDashboard(root, makeBootstrap());
    const deleteBtn = root.querySelector<HTMLButtonElement>(".btn-delete")!;
    deleteBtn.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(apiModule.deleteSession).toHaveBeenCalledWith("sess-del");
    vi.unstubAllGlobals();
  });

  it("does not delete when confirm is cancelled", async () => {
    const session = makeSummary({ sessionId: "sess-keep" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    vi.stubGlobal("confirm", () => false);
    await renderDashboard(root, makeBootstrap());
    const deleteBtn = root.querySelector<HTMLButtonElement>(".btn-delete")!;
    deleteBtn.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(apiModule.deleteSession).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("shows error in status when delete fails", async () => {
    const session = makeSummary({ sessionId: "sess-del-fail" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [session],
      stopped: [],
      degraded: [],
    });
    vi.mocked(apiModule.deleteSession).mockRejectedValue(new Error("delete failed"));
    vi.stubGlobal("confirm", () => true);
    await renderDashboard(root, makeBootstrap());
    const deleteBtn = root.querySelector<HTMLButtonElement>(".btn-delete")!;
    deleteBtn.click();
    await new Promise((r) => setTimeout(r, 20));
    const status = root.querySelector<HTMLElement>("#dashboard-status")!;
    expect(status.textContent).toContain("Delete failed");
    vi.unstubAllGlobals();
  });

  it("clicking non-button in content is a no-op", async () => {
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([]);
    await renderDashboard(root, makeBootstrap());
    const content = root.querySelector<HTMLElement>("#dashboard-content")!;
    // Click a non-button element
    content.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    // Should not throw
  });

  it("restart button with no data-session-id is a no-op", async () => {
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([]);
    await renderDashboard(root, makeBootstrap());
    const content = root.querySelector<HTMLElement>("#dashboard-content")!;
    // Create a restart button without data-session-id
    const btn = document.createElement("button");
    btn.className = "btn-restart";
    content.appendChild(btn);
    btn.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(apiModule.restartSession).not.toHaveBeenCalled();
  });

  it("delete button with no data-session-id is a no-op", async () => {
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([]);
    await renderDashboard(root, makeBootstrap());
    const content = root.querySelector<HTMLElement>("#dashboard-content")!;
    // Create a delete button without data-session-id
    const btn = document.createElement("button");
    btn.className = "btn-delete";
    content.appendChild(btn);
    btn.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(apiModule.deleteSession).not.toHaveBeenCalled();
  });

  it("renders sessions with lastError (Error chip status)", async () => {
    const session = makeSummary({ connected: false, lastError: "something broke", lifecycleState: "error" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [],
      stopped: [],
      degraded: [session],
    });
    await renderDashboard(root, makeBootstrap());
    // Error chip should show "Error" text
    expect(root.innerHTML).toContain("Error");
  });

  it("renders sessions with connected=false and no lastError (Stopped chip)", async () => {
    const session = makeSummary({ connected: false, lastError: null, lifecycleState: "stopped" });
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([session]);
    vi.mocked(stateModule.summarizeSessions).mockReturnValue({
      running: [],
      stopped: [session],
      degraded: [],
    });
    await renderDashboard(root, makeBootstrap());
    expect(root.innerHTML).toContain("Stopped");
  });

  it("renders empty sections for each group", async () => {
    vi.mocked(stateModule.loadDashboardState).mockResolvedValue([]);
    await renderDashboard(root, makeBootstrap());
    const content = root.querySelector<HTMLElement>("#dashboard-content")!;
    expect(content.innerHTML).toContain("Active");
    expect(content.innerHTML).toContain("Idle");
    expect(content.innerHTML).toContain("Error");
  });

  it("throws when dashboard shell is incomplete (line 90)", async () => {
    // Make querySelector return null for #dashboard-status to trigger the guard
    const origQuerySelector = root.querySelector.bind(root);
    const spy = vi.spyOn(root, "querySelector").mockImplementation((sel: string) => {
      if (sel === "#dashboard-status") return null;
      return origQuerySelector(sel);
    });
    await expect(renderDashboard(root, makeBootstrap())).rejects.toThrow("dashboard shell is incomplete");
    spy.mockRestore();
  });
});
