//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { DeckMux } from "./deckmux.js";
import type { DeckMuxConfig } from "./types.js";

const CONFIG: DeckMuxConfig = { autoTransferIdleS: 0, keystrokeQueue: "display" };

let parent: HTMLElement;
let terminalContainer: HTMLElement;
let dm: DeckMux;

beforeEach(() => {
  parent = document.createElement("div");
  terminalContainer = document.createElement("div");
  parent.appendChild(terminalContainer);
  document.body.appendChild(parent);
  dm = new DeckMux(terminalContainer, null);
  dm.enable(CONFIG);
});

afterEach(() => {
  dm.destroy();
  parent.remove();
});

function clickAvatar(userId: string): void {
  // Avatars live in the presence bar host inserted before the terminal.
  // Match on dataset (not a CSS selector) so the test harness itself does
  // not depend on CSS.escape — we are exercising production code's lookup.
  const wraps = parent.querySelectorAll<HTMLElement>(".dm-avatar-wrap");
  let wrap: HTMLElement | null = null;
  for (const w of wraps) {
    if (w.dataset.userId === userId) wrap = w;
  }
  if (!wrap) throw new Error(`avatar for ${userId} not found`);
  wrap.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

describe("DeckMux avatar lookup", () => {
  it("avatar lookup survives a userId with CSS-special chars", () => {
    const userId = 'a"]b';
    dm.handleMessage({ type: "dm_join", user_id: userId, name: "x", color: "#fff" });

    expect(() => clickAvatar(userId)).not.toThrow();
  });
});
