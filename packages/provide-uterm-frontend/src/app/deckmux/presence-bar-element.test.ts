//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "./presence-bar-element.js";
import type { PresenceBar, PresenceUser } from "./presence-bar-element.js";

describe("PresenceBar (uterm-presence-bar)", () => {
  let el: PresenceBar;

  beforeEach(() => {
    el = document.createElement("uterm-presence-bar") as PresenceBar;
    document.body.appendChild(el);
  });

  afterEach(() => {
    el.remove();
  });

  function getToggle(name: "Names" | "Cursors" | "Dims"): HTMLButtonElement {
    // Buttons are in Shadow DOM
    const buttons = el.shadowRoot!.querySelectorAll<HTMLButtonElement>(".dm-toggle-btn");
    for (const btn of buttons) {
      if (btn.textContent === name) return btn;
    }
    throw new Error(`toggle ${name} not found`);
  }

  it("renders a toolbar landmark for the toggles", () => {
    const row = el.shadowRoot!.querySelector(".dm-toggles");
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

  it("Names toggle flips aria-pressed when clicked and dispatches event", async () => {
    const spy = vi.fn();
    el.addEventListener("presence:toggle-names", spy);

    const btn = getToggle("Names");
    btn.click();
    
    // Wait for Lit element update
    await el.updateComplete;
    
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0][0].detail).toBe(true);
  });

  it("Cursors toggle flips aria-pressed when clicked and dispatches event", async () => {
    const spy = vi.fn();
    el.addEventListener("presence:toggle-cursors", spy);

    const btn = getToggle("Cursors");
    btn.click();
    
    await el.updateComplete;
    
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0][0].detail).toBe(false);
  });

  it("Dims toggle flips aria-pressed when clicked and dispatches event", async () => {
    const spy = vi.fn();
    el.addEventListener("presence:toggle-ghost-box", spy);

    const btn = getToggle("Dims");
    btn.click();
    
    await el.updateComplete;
    
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    expect(spy).toHaveBeenCalledOnce();
    expect(spy.mock.calls[0][0].detail).toBe(false);
  });

  it("renders avatars based on users property", async () => {
    const users: PresenceUser[] = [
      {
        userId: "u1",
        name: "Alice",
        color: "#ff0000",
        initials: "AL",
        role: "admin",
        typing: false,
        rows: 24,
        cols: 80
      },
      {
        userId: "u2",
        name: "Bob",
        color: "#00ff00",
        initials: "BO",
        role: "viewer",
        typing: true,
        rows: 0,
        cols: 0,
        idle: true
      }
    ];

    el.users = users;
    el.ownerId = "u1";
    await el.updateComplete;

    const avatars = el.shadowRoot!.querySelectorAll(".dm-avatar-wrap");
    expect(avatars.length).toBe(2);

    // Check Alice (owner, not idle, has dims)
    const u1 = avatars[0] as HTMLElement;
    expect(u1.dataset.userId).toBe("u1");
    expect(u1.classList.contains("dm-avatar-wrap--owner")).toBe(true);
    expect(u1.classList.contains("dm-avatar-wrap--typing")).toBe(false);
    expect(u1.classList.contains("dm-avatar--idle")).toBe(false);
    
    const u1Name = u1.querySelector(".dm-avatar-name") as HTMLElement;
    expect(u1Name.textContent).toBe("Alice");
    expect(u1Name.style.display).toBe("none"); // Because names are hidden by default

    const u1Dims = u1.querySelector(".dm-avatar-dims") as HTMLElement;
    expect(u1Dims.textContent).toBe("24×80");
    expect(u1Dims.style.display).not.toBe("none");

    // Check Bob (not owner, typing, idle, no dims)
    const u2 = avatars[1] as HTMLElement;
    expect(u2.dataset.userId).toBe("u2");
    expect(u2.classList.contains("dm-avatar-wrap--owner")).toBe(false);
    expect(u2.classList.contains("dm-avatar-wrap--typing")).toBe(true);
    expect(u2.classList.contains("dm-avatar--idle")).toBe(true);

    const u2Dims = u2.querySelector(".dm-avatar-dims") as HTMLElement;
    expect(u2Dims.style.display).toBe("none");
  });

  it("dispatches avatar events on interaction", async () => {
    const user: PresenceUser = {
      userId: "user_x",
      name: "Charlie",
      color: "#000",
      initials: "CH",
      role: "operator",
      typing: false,
      rows: 24,
      cols: 80
    };

    el.users = [user];
    await el.updateComplete;

    const wrap = el.shadowRoot!.querySelector(".dm-avatar-wrap") as HTMLElement;

    const clickSpy = vi.fn();
    const hoverSpy = vi.fn();
    const hoverOutSpy = vi.fn();

    el.addEventListener("presence:click-avatar", clickSpy);
    el.addEventListener("presence:hover-avatar", hoverSpy);
    el.addEventListener("presence:hover-out-avatar", hoverOutSpy);

    wrap.click();
    expect(clickSpy).toHaveBeenCalledOnce();
    expect(clickSpy.mock.calls[0][0].detail).toBe("user_x");

    wrap.dispatchEvent(new MouseEvent("mouseenter"));
    expect(hoverSpy).toHaveBeenCalledOnce();
    expect(hoverSpy.mock.calls[0][0].detail).toBe("user_x");

    wrap.dispatchEvent(new MouseEvent("mouseleave"));
    expect(hoverOutSpy).toHaveBeenCalledOnce();
    expect(hoverOutSpy.mock.calls[0][0].detail).toBe("user_x");
  });
});
