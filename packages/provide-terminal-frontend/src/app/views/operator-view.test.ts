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
  clearRuntime: vi.fn(),
  loadOperatorWorkspaceState: vi.fn(),
  requestAnalysis: vi.fn(),
  switchSessionMode: vi.fn(),
}));
vi.mock("../widgets/hijack-widget-host.js", () => ({
  mountHijackWidget: vi.fn(),
}));

import * as apiModule from "../api.js";
import * as stateModule from "../state.js";
import * as widgetModule from "../widgets/hijack-widget-host.js";
import { renderOperator } from "./operator-view.js";

function makeBootstrap(overrides: Partial<AppBootstrap> = {}): AppBootstrap {
  return {
    page_kind: "operator",
    title: "Operator",
    app_path: "/app",
    assets_path: "/assets",
    session_id: "sess-1",
    ...overrides,
  };
}

function makeSummary(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    sessionId: "sess-1",
    displayName: "Test Session",
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

function makeWorkspaceState(summaryOverrides: Partial<SessionSummary> = {}) {
  const summary = makeSummary(summaryOverrides);
  return {
    session: { summary, snapshotPromptId: null, analysis: null },
    status: { tone: "ok" as const, text: "Test Session is live in hijack mode." },
    modeCommand: { pending: false, lastError: null },
    utilityCommand: { pending: false, lastError: null },
    widget: { mounted: false, error: null },
  };
}

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
  vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: true, error: null });
  vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState());
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("renderOperator", () => {
  it("throws when session_id is missing", async () => {
    await expect(renderOperator(root, makeBootstrap({ session_id: undefined }))).rejects.toThrow(
      "operator bootstrap missing session_id",
    );
  });

  it("renders operator layout with sidebar", async () => {
    await renderOperator(root, makeBootstrap());
    expect(root.querySelector(".layout")).toBeTruthy();
    expect(root.querySelector("#widget")).toBeTruthy();
  });

  it("renders sidebar with session name", async () => {
    await renderOperator(root, makeBootstrap());
    expect(root.innerHTML).toContain("Test Session");
  });

  it("shows status chip", async () => {
    await renderOperator(root, makeBootstrap());
    expect(root.querySelector("#operator-status")).toBeTruthy();
  });

  it("shows error when loadOperatorWorkspaceState throws", async () => {
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockRejectedValue(new Error("load error"));
    await renderOperator(root, makeBootstrap());
    const status = root.querySelector<HTMLElement>("#operator-status")!;
    expect(status.textContent).toContain("Operator workspace failed to load");
  });

  it("shows error when widget mount fails", async () => {
    vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: false, error: "Widget unavailable" });
    await renderOperator(root, makeBootstrap());
    const status = root.querySelector<HTMLElement>("#operator-status")!;
    expect(status.textContent).toBe("Widget unavailable");
  });

  it("shows fallback error when widget fails with null error", async () => {
    vi.mocked(widgetModule.mountHijackWidget).mockReturnValue({ mounted: false, error: null });
    await renderOperator(root, makeBootstrap());
    const status = root.querySelector<HTMLElement>("#operator-status")!;
    expect(status.textContent).toBe("Widget mount failed");
  });

  it("calls switchSessionMode to open on btn-open click", async () => {
    vi.mocked(stateModule.switchSessionMode).mockResolvedValue({
      summary: makeSummary({ inputMode: "open" }),
      snapshotPromptId: null,
      analysis: null,
    });
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState({ inputMode: "open" }));
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-open")?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(stateModule.switchSessionMode).toHaveBeenCalledWith("sess-1", "open");
  });

  it("calls switchSessionMode to hijack on btn-hijack click", async () => {
    vi.mocked(stateModule.switchSessionMode).mockResolvedValue({
      summary: makeSummary({ inputMode: "hijack" }),
      snapshotPromptId: null,
      analysis: null,
    });
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState({ inputMode: "hijack" }));
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-hijack")?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(stateModule.switchSessionMode).toHaveBeenCalledWith("sess-1", "hijack");
  });

  it("calls clearRuntime when btn-clear clicked and confirmed", async () => {
    vi.stubGlobal("confirm", () => true);
    vi.mocked(stateModule.clearRuntime).mockResolvedValue({
      summary: makeSummary(),
      snapshotPromptId: null,
      analysis: null,
    });
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState());
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-clear")?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(stateModule.clearRuntime).toHaveBeenCalledWith("sess-1");
  });

  it("does not call clearRuntime when btn-clear cancelled", async () => {
    vi.stubGlobal("confirm", () => false);
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-clear")?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(stateModule.clearRuntime).not.toHaveBeenCalled();
  });

  it("shows error status when clearRuntime fails", async () => {
    vi.stubGlobal("confirm", () => true);
    vi.mocked(stateModule.clearRuntime).mockRejectedValue(new Error("clear failed"));
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-clear")?.click();
    await new Promise((r) => setTimeout(r, 20));
    const status = root.querySelector<HTMLElement>("#operator-status")!;
    expect(status.textContent).toContain("Clear failed");
  });

  it("calls requestAnalysis and shows result on btn-analyze click", async () => {
    vi.mocked(stateModule.requestAnalysis).mockResolvedValue("AI analysis result");
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-analyze")?.click();
    await new Promise((r) => setTimeout(r, 20));
    const analysisEl = root.querySelector<HTMLElement>("#analysis-result")!;
    expect(analysisEl.textContent).toBe("AI analysis result");
    expect(analysisEl.style.display).toBe("block");
  });

  it("shows error when requestAnalysis fails", async () => {
    vi.mocked(stateModule.requestAnalysis).mockRejectedValue(new Error("analyze failed"));
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-analyze")?.click();
    await new Promise((r) => setTimeout(r, 20));
    const status = root.querySelector<HTMLElement>("#operator-status")!;
    expect(status.textContent).toContain("Analyze failed");
  });

  it("calls restartSession when btn-restart clicked and confirmed", async () => {
    vi.stubGlobal("confirm", () => true);
    vi.mocked(apiModule.restartSession).mockResolvedValue(makeSummary());
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState());
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-restart")?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(apiModule.restartSession).toHaveBeenCalledWith("sess-1");
  });

  it("does not call restartSession when cancelled", async () => {
    vi.stubGlobal("confirm", () => false);
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-restart")?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(apiModule.restartSession).not.toHaveBeenCalled();
  });

  it("shows error when restartSession fails", async () => {
    vi.stubGlobal("confirm", () => true);
    vi.mocked(apiModule.restartSession).mockRejectedValue(new Error("restart error"));
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-restart")?.click();
    await new Promise((r) => setTimeout(r, 20));
    const status = root.querySelector<HTMLElement>("#operator-status")!;
    expect(status.textContent).toContain("Restart failed");
  });

  it("calls deleteSession when btn-delete clicked and confirmed", async () => {
    vi.stubGlobal("confirm", () => true);
    vi.mocked(apiModule.deleteSession).mockResolvedValue(undefined);
    Object.defineProperty(window, "location", {
      value: { href: "" },
      writable: true,
    });
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-delete")?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(apiModule.deleteSession).toHaveBeenCalledWith("sess-1");
  });

  it("does not call deleteSession when cancelled", async () => {
    vi.stubGlobal("confirm", () => false);
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-delete")?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(apiModule.deleteSession).not.toHaveBeenCalled();
  });

  it("shows error when deleteSession fails", async () => {
    vi.stubGlobal("confirm", () => true);
    vi.mocked(apiModule.deleteSession).mockRejectedValue(new Error("delete error"));
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-delete")?.click();
    await new Promise((r) => setTimeout(r, 20));
    const status = root.querySelector<HTMLElement>("#operator-status")!;
    expect(status.textContent).toContain("Delete failed");
  });

  it("renders 'open' mode buttons correctly (isOpen=true)", async () => {
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState({ inputMode: "open" }));
    await renderOperator(root, makeBootstrap());
    // Re-render happens on refresh — sidebar should show open mode state
    const openBtn = root.querySelector<HTMLButtonElement>("#btn-open")!;
    // After refresh with open mode, btn-open should be primary
    expect(openBtn.className).toContain("primary");
  });

  it("renders sidebar with connected=false (offline badge)", async () => {
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState({ connected: false }));
    await renderOperator(root, makeBootstrap());
    expect(root.innerHTML).toContain("Offline");
  });

  it("renders sidebar with connected=true (live badge)", async () => {
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState({ connected: true }));
    await renderOperator(root, makeBootstrap());
    expect(root.innerHTML).toContain("Live");
  });

  it("renders tags in sidebar", async () => {
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState({ tags: ["game", "prod"] }));
    await renderOperator(root, makeBootstrap());
    expect(root.innerHTML).toContain("game");
    expect(root.innerHTML).toContain("prod");
  });

  it("renders 'none' for empty tags", async () => {
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState({ tags: [] }));
    await renderOperator(root, makeBootstrap());
    expect(root.innerHTML).toContain("none");
  });

  it("renders auto-start as 'yes' in session info (line 90 true branch)", async () => {
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockResolvedValue(makeWorkspaceState({ autoStart: true }));
    await renderOperator(root, makeBootstrap());
    // The sidebar should show "yes" for auto-start
    expect(root.innerHTML).toContain("yes");
  });

  it("throws when #widget element is missing from DOM (line 116)", async () => {
    // Make querySelector return null for #widget so the guard fires
    const origQuerySelector = root.querySelector.bind(root);
    const spy = vi.spyOn(root, "querySelector").mockImplementation((sel: string) => {
      if (sel === "#widget") return null;
      return origQuerySelector(sel);
    });
    await expect(renderOperator(root, makeBootstrap())).rejects.toThrow("operator shell is incomplete");
    spy.mockRestore();
  });

  it("does not error when #analysis-result is missing when analyze runs (line 136 false branch)", async () => {
    vi.mocked(stateModule.requestAnalysis).mockResolvedValue("AI result");
    await renderOperator(root, makeBootstrap());
    // Remove the analysis-result element from DOM so the if(el) branch is false
    const analysisEl = root.querySelector<HTMLElement>("#analysis-result");
    if (analysisEl) analysisEl.remove();
    root.querySelector<HTMLButtonElement>("#btn-analyze")?.click();
    await new Promise((r) => setTimeout(r, 20));
    // No error thrown, requestAnalysis was still called
    expect(stateModule.requestAnalysis).toHaveBeenCalledWith("sess-1");
  });

  it("setStatus does nothing when #operator-status element is missing (line 161 false branch)", async () => {
    await renderOperator(root, makeBootstrap());
    // Remove the status element so setStatus's if(el) is false
    const statusEl = root.querySelector<HTMLElement>("#operator-status");
    if (statusEl) statusEl.remove();
    // Trigger an action that calls setStatus — clicking btn-analyze with an error
    vi.mocked(stateModule.requestAnalysis).mockRejectedValue(new Error("analyze fail"));
    root.querySelector<HTMLButtonElement>("#btn-analyze")?.click();
    await new Promise((r) => setTimeout(r, 20));
    // Should not throw — setStatus silently does nothing when el is null
    expect(root.querySelector("#operator-status")).toBeNull();
  });

  it("refresh skips sidebar replacement when sidebar element is missing (line 172 false branch)", async () => {
    // Override querySelector so that `.layout > section:first-child` returns null during the second refresh
    // but other selectors still work (so the initial render completes normally).
    let callCount = 0;
    const origQuerySelector = root.querySelector.bind(root);
    let sidebarSpy: ReturnType<typeof vi.spyOn> | null = null;
    vi.mocked(stateModule.switchSessionMode).mockResolvedValue({
      summary: makeSummary({ inputMode: "open" }),
      snapshotPromptId: null,
      analysis: null,
    });
    // On first call to loadOperatorWorkspaceState (initial refresh), allow sidebar replacement.
    // On second call (after btn-open triggers switchSessionMode → refresh), block sidebar lookup.
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockImplementation(() => {
      callCount++;
      if (callCount >= 2 && sidebarSpy === null) {
        // After the first refresh, intercept sidebar lookups to simulate it being gone
        sidebarSpy = vi.spyOn(root, "querySelector").mockImplementation((sel: string) => {
          if (sel === ".layout > section:first-child") return null;
          // For all other selectors, call original without restoring spy (so it stays active)
          return origQuerySelector(sel);
        });
      }
      return Promise.resolve(makeWorkspaceState({ inputMode: "open" }));
    });
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-open")?.click();
    await new Promise((r) => setTimeout(r, 30));
    if (sidebarSpy) (sidebarSpy as ReturnType<typeof vi.spyOn>).mockRestore();
    // No error — the false branch of if(sidebar) was silently skipped
    expect(stateModule.switchSessionMode).toHaveBeenCalledWith("sess-1", "open");
  });

  it("refresh skips sidebar replaceWith when tmp querySelector returns null (line 176 false branch)", async () => {
    // Intercept document.createElement so that when "div" is created inside refresh,
    // its querySelector always returns null — simulating no <section> in tmp.
    let callCount = 0;
    vi.mocked(stateModule.switchSessionMode).mockResolvedValue({
      summary: makeSummary({ inputMode: "open" }),
      snapshotPromptId: null,
      analysis: null,
    });
    vi.mocked(stateModule.loadOperatorWorkspaceState).mockImplementation(() => {
      callCount++;
      if (callCount >= 2) {
        // Override createElement for the next "div" call so its querySelector returns null
        const origCreateElement = document.createElement.bind(document);
        const createSpy = vi.spyOn(document, "createElement");
        createSpy.mockImplementationOnce((tag: string) => {
          if (tag === "div") {
            const fakeDiv = origCreateElement("div");
            // Make querySelector return null for "section"
            vi.spyOn(fakeDiv, "querySelector").mockReturnValue(null);
            createSpy.mockRestore();
            return fakeDiv;
          }
          createSpy.mockRestore();
          return origCreateElement(tag);
        });
      }
      return Promise.resolve(makeWorkspaceState({ inputMode: "open" }));
    });
    await renderOperator(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-open")?.click();
    await new Promise((r) => setTimeout(r, 30));
    // No error — if(next) false branch was silently skipped
    expect(stateModule.switchSessionMode).toHaveBeenCalledWith("sess-1", "open");
  });
});
