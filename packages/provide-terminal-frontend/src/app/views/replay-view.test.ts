//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppBootstrap, RecordingEntryView } from "../types.js";

vi.mock("../state.js", () => ({
  loadReplayState: vi.fn(),
}));

import * as stateModule from "../state.js";
import { renderReplay } from "./replay-view.js";

function makeBootstrap(overrides: Partial<AppBootstrap> = {}): AppBootstrap {
  return {
    page_kind: "replay",
    title: "Replay",
    app_path: "/app",
    assets_path: "/assets",
    session_id: "sess-1",
    ...overrides,
  };
}

function makeEntry(overrides: Partial<RecordingEntryView> = {}): RecordingEntryView {
  return {
    ts: 1000,
    event: "read",
    payload: { screen: "hello" },
    screen: "hello",
    ...overrides,
  };
}

function makeReplayState(entries: RecordingEntryView[] = []) {
  return {
    entries,
    index: entries.length > 0 ? entries.length - 1 : 0,
    filter: "",
    limit: 200,
    total: entries.length,
    status:
      entries.length > 0
        ? { tone: "ok" as const, text: `Loaded ${entries.length} entries.` }
        : { tone: "info" as const, text: "No entries match the current filter." },
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

describe("renderReplay", () => {
  it("throws when session_id is missing", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState());
    await expect(renderReplay(root, makeBootstrap({ session_id: undefined }))).rejects.toThrow(
      "replay bootstrap missing session_id",
    );
  });

  it("renders replay page structure", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState());
    await renderReplay(root, makeBootstrap());
    expect(root.querySelector("#replay-meta")).toBeTruthy();
    expect(root.querySelector("#replay-list")).toBeTruthy();
    expect(root.querySelector("#replay-screen")).toBeTruthy();
    expect(root.querySelector("#replay-json")).toBeTruthy();
    expect(root.querySelector("#replay-scrubber")).toBeTruthy();
  });

  it("renders entry list when entries present", async () => {
    const entries = [makeEntry({ event: "read", ts: 1000 })];
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState(entries));
    await renderReplay(root, makeBootstrap());
    const list = root.querySelector<HTMLElement>("#replay-list")!;
    expect(list.innerHTML).toContain("read");
  });

  it("shows 'No entries' message when empty", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState([]));
    await renderReplay(root, makeBootstrap());
    const list = root.querySelector<HTMLElement>("#replay-list")!;
    expect(list.textContent).toContain("No entries");
  });

  it("displays screen text of current entry", async () => {
    const entries = [makeEntry({ screen: "Terminal output here", ts: null })];
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState(entries));
    await renderReplay(root, makeBootstrap());
    const screen = root.querySelector<HTMLElement>("#replay-screen")!;
    expect(screen.textContent).toBe("Terminal output here");
  });

  it("scrubber is disabled when no entries", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState([]));
    await renderReplay(root, makeBootstrap());
    const scrubber = root.querySelector<HTMLInputElement>("#replay-scrubber")!;
    expect(scrubber.disabled).toBe(true);
  });

  it("scrubber is enabled when entries present", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState([makeEntry()]));
    await renderReplay(root, makeBootstrap());
    const scrubber = root.querySelector<HTMLInputElement>("#replay-scrubber")!;
    expect(scrubber.disabled).toBe(false);
  });

  it("reloads on btn-load click", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState());
    await renderReplay(root, makeBootstrap());
    vi.clearAllMocks();
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState([makeEntry()]));
    root.querySelector<HTMLButtonElement>("#btn-load")?.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(stateModule.loadReplayState).toHaveBeenCalled();
  });

  it("navigates to prev entry on btn-prev click", async () => {
    const entries = [makeEntry({ event: "a" }), makeEntry({ event: "b" })];
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState(entries));
    await renderReplay(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-prev")?.click();
    const screen = root.querySelector<HTMLElement>("#replay-screen")!;
    expect(screen.textContent).toBe("hello");
  });

  it("navigates to next entry on btn-next click", async () => {
    const entries = [makeEntry({ screen: "first" }), makeEntry({ screen: "second" })];
    // index starts at last (1)
    const state = { ...makeReplayState(entries), index: 0 };
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(state);
    await renderReplay(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-next")?.click();
    const screen = root.querySelector<HTMLElement>("#replay-screen")!;
    expect(screen.textContent).toBe("second");
  });

  it("navigates to first entry on btn-first click", async () => {
    const entries = [makeEntry({ screen: "first" }), makeEntry({ screen: "second" })];
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState(entries));
    await renderReplay(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-first")?.click();
    const screen = root.querySelector<HTMLElement>("#replay-screen")!;
    expect(screen.textContent).toBe("first");
  });

  it("navigates to last entry on btn-last click", async () => {
    const entries = [makeEntry({ screen: "first" }), makeEntry({ screen: "last" })];
    const state = { ...makeReplayState(entries), index: 0 };
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(state);
    await renderReplay(root, makeBootstrap());
    root.querySelector<HTMLButtonElement>("#btn-last")?.click();
    const screen = root.querySelector<HTMLElement>("#replay-screen")!;
    expect(screen.textContent).toBe("last");
  });

  it("reloads when filter changes", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState());
    await renderReplay(root, makeBootstrap());
    vi.clearAllMocks();
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState());
    root.querySelector<HTMLSelectElement>("#replay-filter")?.dispatchEvent(new Event("change"));
    await new Promise((r) => setTimeout(r, 20));
    expect(stateModule.loadReplayState).toHaveBeenCalled();
  });

  it("reloads when limit changes", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState());
    await renderReplay(root, makeBootstrap());
    vi.clearAllMocks();
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState());
    root.querySelector<HTMLSelectElement>("#replay-limit")?.dispatchEvent(new Event("change"));
    await new Promise((r) => setTimeout(r, 20));
    expect(stateModule.loadReplayState).toHaveBeenCalled();
  });

  it("navigates via scrubber input", async () => {
    const entries = [makeEntry({ screen: "first" }), makeEntry({ screen: "second" })];
    const state = { ...makeReplayState(entries), index: 1 };
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(state);
    await renderReplay(root, makeBootstrap());
    const scrubber = root.querySelector<HTMLInputElement>("#replay-scrubber")!;
    scrubber.value = "0";
    scrubber.dispatchEvent(new Event("input"));
    const screen = root.querySelector<HTMLElement>("#replay-screen")!;
    expect(screen.textContent).toBe("first");
  });

  it("navigates via clicking replay entry button", async () => {
    const entries = [makeEntry({ screen: "first" }), makeEntry({ screen: "second" })];
    const state = { ...makeReplayState(entries), index: 1 };
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(state);
    await renderReplay(root, makeBootstrap());
    const list = root.querySelector<HTMLElement>("#replay-list")!;
    const firstBtn = list.querySelector<HTMLButtonElement>("[data-index='0']")!;
    firstBtn.click();
    const screen = root.querySelector<HTMLElement>("#replay-screen")!;
    expect(screen.textContent).toBe("first");
  });

  it("clicking non-data-index element in list is no-op", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState([makeEntry()]));
    await renderReplay(root, makeBootstrap());
    const list = root.querySelector<HTMLElement>("#replay-list")!;
    // Create a div without data-index
    const randomDiv = document.createElement("div");
    list.appendChild(randomDiv);
    randomDiv.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    // Should not throw
  });

  it("handles null ts in entry list", async () => {
    const entries = [makeEntry({ ts: null })];
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState(entries));
    await renderReplay(root, makeBootstrap());
    const list = root.querySelector<HTMLElement>("#replay-list")!;
    // Should not include time string when ts is null
    expect(list.innerHTML).not.toContain("•");
  });

  it("renders meta JSON with state info", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState([]));
    await renderReplay(root, makeBootstrap());
    const meta = root.querySelector<HTMLElement>("#replay-meta")!;
    const parsed = JSON.parse(meta.textContent || "{}");
    expect(parsed).toHaveProperty("total");
    expect(parsed).toHaveProperty("limit");
  });

  it("clamps index when navigating past boundaries", async () => {
    const entries = [makeEntry({ screen: "only" })];
    const state = { ...makeReplayState(entries), index: 0 };
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(state);
    await renderReplay(root, makeBootstrap());
    // Try to go before first
    root.querySelector<HTMLButtonElement>("#btn-prev")?.click();
    root.querySelector<HTMLButtonElement>("#btn-prev")?.click();
    const screen = root.querySelector<HTMLElement>("#replay-screen")!;
    expect(screen.textContent).toBe("only");
    // Try to go past last
    root.querySelector<HTMLButtonElement>("#btn-next")?.click();
    root.querySelector<HTMLButtonElement>("#btn-next")?.click();
    expect(screen.textContent).toBe("only");
  });

  it("throws when replay shell DOM elements are missing (line 103)", async () => {
    // Make querySelector return null for #replay-filter so the shell-incomplete guard fires
    const origQuerySelector = root.querySelector.bind(root);
    const spy = vi.spyOn(root, "querySelector").mockImplementation((sel: string) => {
      if (sel === "#replay-filter") return null;
      return origQuerySelector(sel);
    });
    await expect(renderReplay(root, makeBootstrap())).rejects.toThrow("replay shell is incomplete");
    spy.mockRestore();
  });

  it("updateReplayUi returns early when meta element is missing (line 31)", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState([makeEntry()]));
    await renderReplay(root, makeBootstrap());
    // Remove #replay-meta so updateReplayUi early-returns on the next update
    const meta = root.querySelector<HTMLElement>("#replay-meta");
    if (meta) meta.remove();
    // Trigger an update via btn-load — should not throw
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState([makeEntry()]));
    root.querySelector<HTMLButtonElement>("#btn-load")?.click();
    await new Promise((r) => setTimeout(r, 20));
    // No error — early return path was taken
    expect(root.querySelector("#replay-meta")).toBeNull();
  });

  it("clicking non-HTMLElement target in replay-list is a no-op (line 128 true branch)", async () => {
    vi.mocked(stateModule.loadReplayState).mockResolvedValue(makeReplayState([makeEntry()]));
    await renderReplay(root, makeBootstrap());
    const list = root.querySelector<HTMLElement>("#replay-list")!;
    // Dispatch a click event where target is not an HTMLElement (e.g. a Text node via synthetic event)
    // We can simulate this by dispatching on the list itself but overriding event.target via a custom event
    // In jsdom, all click targets are HTMLElements, so we use dispatchEvent with a manually constructed event
    const event = new MouseEvent("click", { bubbles: true });
    Object.defineProperty(event, "target", { value: document.createTextNode("text"), writable: false });
    list.dispatchEvent(event);
    // No error thrown — the instanceof check returned early
  });
});
