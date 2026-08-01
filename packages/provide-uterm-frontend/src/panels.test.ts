//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const vncHandles = vi.hoisted(() => [] as Array<{ disconnect: ReturnType<typeof vi.fn> }>);
const attachVnc = vi.hoisted(() => vi.fn((_screen, _params, options) => {
  const handle = { disconnect: vi.fn() };
  vncHandles.push(handle);
  options?.onStatus?.("connected", "Connected");
  return handle;
}));
vi.mock("./vnc-page.js", () => ({ attachVnc }));
vi.mock("./terminal-element.js", () => ({}));

import { PanelsPage } from "./panels.js";

class StubTerminal extends HTMLElement {
  config: Record<string, unknown> = {};
  connect = vi.fn();
}
if (!customElements.get("uterm-terminal")) customElements.define("uterm-terminal", StubTerminal);

beforeEach(() => {
  vi.useFakeTimers();
  attachVnc.mockClear();
  vncHandles.length = 0;
  document.body.innerHTML = `<span id="panels-count"></span><div id="panels-stage"></div>`;
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("PanelsPage", () => {
  it("mounts VNC sources, recursively splits, closes, and disposes removed panes", () => {
    const stage = document.getElementById("panels-stage") as HTMLElement;
    const page = new PanelsPage(stage, "?vnc=w1~h1~t1,w2~h2~t2");
    expect(page.leafCount).toBe(2);
    expect(attachVnc).toHaveBeenCalledTimes(2);
    expect(document.getElementById("panels-count")?.textContent).toBe("2");

    const firstLeaf = (stage.querySelector(".pane") as HTMLElement).dataset.leaf as string;
    page.splitPane(firstLeaf);
    expect(page.leafCount).toBe(3);
    expect(attachVnc).toHaveBeenCalledTimes(3);
    const liveLeaf = (stage.querySelector(".pane") as HTMLElement).dataset.leaf as string;
    page.closePane(liveLeaf);
    expect(page.leafCount).toBe(2);
    expect(vncHandles.some((handle) => handle.disconnect.mock.calls.length > 0)).toBe(true);

    page.nautilus();
    expect(page.leafCount).toBe(4);
    expect(stage.querySelector(".split-row, .split-col")).not.toBeNull();
  });

  it("mounts terminal pools and connects custom elements asynchronously", () => {
    const stage = document.getElementById("panels-stage") as HTMLElement;
    new PanelsPage(stage, "?term=alpha~browser,beta~raw");
    vi.runOnlyPendingTimers();
    const terminals = stage.querySelectorAll<StubTerminal>("uterm-terminal");
    expect(terminals).toHaveLength(1);
    expect(terminals[0]?.config).toMatchObject({ wsUrl: "/ws/browser/alpha/term", title: "alpha" });
    expect(terminals[0]?.connect).toHaveBeenCalledOnce();
  });

  it("renders explicit empty-source badges and refuses to close the last pane", () => {
    const stage = document.getElementById("panels-stage") as HTMLElement;
    const page = new PanelsPage(stage, "");
    expect(stage.textContent).toContain("no vnc source");
    expect(stage.textContent).toContain("no term source");
    const leaves = Array.from(stage.querySelectorAll<HTMLElement>(".pane"));
    page.closePane(leaves[0]?.dataset.leaf ?? "");
    page.closePane((stage.querySelector(".pane") as HTMLElement).dataset.leaf ?? "");
    expect(page.leafCount).toBe(1);
  });
});
