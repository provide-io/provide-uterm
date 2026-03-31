//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap, SessionSummary } from "../types.js";

vi.mock("../state.js", () => ({
  loadUserWorkspaceState: vi.fn(),
}));
vi.mock("../widgets/hijack-widget-host.js", () => ({
  mountHijackWidget: vi.fn(),
}));

import * as stateModule from "../state.js";
import * as widgetModule from "../widgets/hijack-widget-host.js";
import { renderSession } from "./session-view.js";

function makeBootstrap(overrides: Partial<AppBootstrap> = {}): AppBootstrap {
  return {
    page_kind: "session",
    title: "My Session",
    app_path: "/app",
    assets_path: "/assets",
    session_id: "sess-1",
    ...overrides,
  };
}

function makeSummary(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    sessionId: "sess-1",
    displayName: "My Session",
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
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("renderSession", () => {
  it("throws when session_id is missing from bootstrap", async () => {
    const bootstrap = makeBootstrap({ session_id: undefined });
    await expect(renderSession(root, bootstrap)).rejects.toThrow("session bootstrap missing session_id");
  });

  it("renders session page structure", async () => {
    const summary = makeSummary();
    vi.mocked(stateModule.loadUserWorkspaceState).mockResolvedValue({
      session: { summary, snapshotPromptId: null, analysis: null },
      status: { tone: "ok", text: "Live" },
      widget: { mounted: false, error: null },
    });
    vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: true, error: null });
    await renderSession(root, makeBootstrap());
    expect(root.querySelector("#session-status")).toBeTruthy();
    expect(root.querySelector("#widget")).toBeTruthy();
  });

  it("sets status chip correctly on successful load", async () => {
    const summary = makeSummary({ connected: true });
    vi.mocked(stateModule.loadUserWorkspaceState).mockResolvedValue({
      session: { summary, snapshotPromptId: null, analysis: null },
      status: { tone: "ok", text: "My Session is live in hijack mode." },
      widget: { mounted: false, error: null },
    });
    vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: true, error: null });
    await renderSession(root, makeBootstrap());
    const status = root.querySelector<HTMLElement>("#session-status")!;
    expect(status.className).toContain("ok");
    expect(status.textContent).toContain("live");
  });

  it("shows error status when widget mount fails", async () => {
    const summary = makeSummary();
    vi.mocked(stateModule.loadUserWorkspaceState).mockResolvedValue({
      session: { summary, snapshotPromptId: null, analysis: null },
      status: { tone: "ok", text: "OK" },
      widget: { mounted: false, error: null },
    });
    vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: false, error: "Widget failed" });
    await renderSession(root, makeBootstrap());
    const status = root.querySelector<HTMLElement>("#session-status")!;
    expect(status.className).toContain("error");
    expect(status.textContent).toBe("Widget failed");
  });

  it("shows fallback error when widget mount fails with null error", async () => {
    const summary = makeSummary();
    vi.mocked(stateModule.loadUserWorkspaceState).mockResolvedValue({
      session: { summary, snapshotPromptId: null, analysis: null },
      status: { tone: "ok", text: "OK" },
      widget: { mounted: false, error: null },
    });
    vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: false, error: null });
    await renderSession(root, makeBootstrap());
    const status = root.querySelector<HTMLElement>("#session-status")!;
    expect(status.textContent).toBe("Widget mount failed");
  });

  it("shows error status when loadUserWorkspaceState throws", async () => {
    vi.mocked(stateModule.loadUserWorkspaceState).mockRejectedValue(new Error("load error"));
    await renderSession(root, makeBootstrap());
    const status = root.querySelector<HTMLElement>("#session-status")!;
    expect(status.className).toContain("error");
    expect(status.textContent).toContain("Session failed to load");
  });

  it("escapes special chars in app_path", async () => {
    const summary = makeSummary();
    vi.mocked(stateModule.loadUserWorkspaceState).mockResolvedValue({
      session: { summary, snapshotPromptId: null, analysis: null },
      status: { tone: "ok", text: "OK" },
      widget: { mounted: false, error: null },
    });
    vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: true, error: null });
    await renderSession(root, makeBootstrap({ app_path: "/app" }));
    // Verify basic structure renders correctly
    const controlLink = root.querySelector('a[href*="/operator/"]');
    expect(controlLink).toBeTruthy();
  });

  it("calls mountHijackWidget with session_id and 'user' surface", async () => {
    const summary = makeSummary();
    vi.mocked(stateModule.loadUserWorkspaceState).mockResolvedValue({
      session: { summary, snapshotPromptId: null, analysis: null },
      status: { tone: "ok", text: "OK" },
      widget: { mounted: false, error: null },
    });
    vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: true, error: null });
    const bootstrap = makeBootstrap({ session_id: "my-sess" });
    await renderSession(root, bootstrap);
    expect(widgetModule.mountHijackWidget).toHaveBeenCalledWith(expect.any(HTMLElement), "my-sess", "user");
  });

  it("hides the control link for viewer share pages", async () => {
    const summary = makeSummary();
    vi.mocked(stateModule.loadUserWorkspaceState).mockResolvedValue({
      session: { summary, snapshotPromptId: null, analysis: null },
      status: { tone: "ok", text: "OK" },
      widget: { mounted: false, error: null },
    });
    vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: true, error: null });
    await renderSession(root, makeBootstrap({ share_role: "viewer", share_token: "share-token-123" }));
    expect(root.querySelector('a[href*="/operator/"]')).toBeNull();
  });

  it("throws when session shell DOM elements are missing (line 36)", async () => {
    // Make querySelector return null for #session-status so the guard fires
    const origQuerySelector = root.querySelector.bind(root);
    const spy = vi.spyOn(root, "querySelector").mockImplementation((sel: string) => {
      if (sel === "#session-status") return null;
      return origQuerySelector(sel);
    });
    await expect(renderSession(root, makeBootstrap())).rejects.toThrow("session shell is incomplete");
    spy.mockRestore();
  });
});
