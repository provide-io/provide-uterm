//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { DeckMuxPresenceBar } from "./presence-bar.js";
import type { DeckMuxConfig } from "./types.js";

const CONFIG: DeckMuxConfig = { autoTransferIdleS: 0, keystrokeQueue: "display" };

let container: HTMLElement;
let bar: DeckMuxPresenceBar;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  bar = new DeckMuxPresenceBar(container, CONFIG);
});

afterEach(() => {
  bar.destroy();
  container.remove();
});

function getToggle(name: "Names" | "Cursors" | "Dims"): HTMLButtonElement {
  const pb = container.querySelector("uterm-presence-bar");
  if (!pb || !pb.shadowRoot) throw new Error("pb not found");
  const buttons = pb.shadowRoot.querySelectorAll<HTMLButtonElement>(".dm-toggle-btn");
  for (const btn of buttons) {
    if (btn.textContent === name) return btn;
  }
  throw new Error(`toggle ${name} not found`);
}

describe("DeckMuxPresenceBar accessibility", () => {
  it("renders a toolbar landmark for the toggles", () => {
    const pb = container.querySelector("uterm-presence-bar");
    const row = pb?.shadowRoot?.querySelector(".dm-toggles");
    expect(row?.getAttribute("role")).toBe("toolbar");
    expect(row?.getAttribute("aria-label")).toBe("Presence display options");
  });

  it("Names toggle has aria-label and aria-pressed", () => {
    const btn = getToggle("Names");
    expect(btn.getAttribute("aria-label")).toBe("Toggle participant names");
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    expect(btn.type).toBe("button");
  });

  it("Cursors toggle has aria-label and aria-pressed", () => {
    const btn = getToggle("Cursors");
    expect(btn.getAttribute("aria-label")).toBe("Toggle participant cursors");
    expect(btn.getAttribute("aria-pressed")).toBe("true");
  });

  it("Dims toggle has aria-label and aria-pressed", () => {
    const btn = getToggle("Dims");
    expect(btn.getAttribute("aria-label")).toBe("Toggle participant viewport dimensions");
    expect(btn.getAttribute("aria-pressed")).toBe("true");
  });

  it("Names toggle flips aria-pressed when clicked", async () => {
    const btn = getToggle("Names");
    btn.click();
    const pb = container.querySelector("uterm-presence-bar") as any;
    await pb.updateComplete;
    expect(btn.getAttribute("aria-pressed")).toBe("true");
  });

  it("Cursors toggle flips aria-pressed when clicked", async () => {
    const btn = getToggle("Cursors");
    btn.click();
    const pb = container.querySelector("uterm-presence-bar") as any;
    await pb.updateComplete;
    expect(btn.getAttribute("aria-pressed")).toBe("false");
  });

  it("Dims toggle flips aria-pressed when clicked", async () => {
    const btn = getToggle("Dims");
    btn.click();
    const pb = container.querySelector("uterm-presence-bar") as any;
    await pb.updateComplete;
    expect(btn.getAttribute("aria-pressed")).toBe("false");
  });
});
