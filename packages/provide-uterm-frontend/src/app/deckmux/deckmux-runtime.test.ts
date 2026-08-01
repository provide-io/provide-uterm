//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ContextMenu } from "./context-menu.js";
import { DeckMuxControlPanel } from "./control-panel.js";
import type { CursorOverlayElement } from "./cursor-overlay-element.js";
import { DeckMuxCursorOverlay } from "./cursor-overlay.js";
import type { EdgeIndicatorsElement } from "./edge-indicators-element.js";
import { DeckMuxEdgeIndicators } from "./edge-indicators.js";
import type { UtermGhostOverlayElement } from "./ghost-overlay-element.js";
import { DeckMuxGhostOverlay } from "./ghost-overlay.js";
import type { PresenceBar } from "./presence-bar-element.js";
import { DeckMuxPresenceBar } from "./presence-bar.js";
import type { DeckMuxConfig, DeckMuxUser } from "./types.js";

const config: DeckMuxConfig = { autoTransferIdleS: 30, keystrokeQueue: "display", ghostBox: true };
const user: DeckMuxUser = {
  userId: "u1", name: "Alice Smith", color: "#f00", role: "operator", initials: "AS",
  scrollLine: 0, scrollRange: [0, 10], totalLines: 100, cols: 80, rows: 24, joinTime: 1,
  selection: null, pin: null, typing: false, queuedKeys: "", isOwner: false,
};

let container: HTMLElement;
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  container.remove();
});

describe("DeckMuxControlPanel", () => {
  it("dispatches request-toast choices and auto-dismisses transfers", () => {
    vi.useFakeTimers();
    const panel = new DeckMuxControlPanel(container);
    panel.hideContextMenu();
    panel.hideToasts();
    panel.hideKeystrokeQueue("missing");
    const accept = vi.fn();
    const deny = vi.fn();
    panel.showRequestToast("Alice", "#f00", accept, deny);
    expect(container.textContent).toContain("Alice wants control");
    (container.querySelector(".dm-toast-btn--accept") as HTMLButtonElement).click();
    expect(accept).toHaveBeenCalledOnce();

    panel.showRequestToast("Alice", "#f00", accept, deny);
    (container.querySelector(".dm-toast-btn--deny") as HTMLButtonElement).click();
    expect(deny).toHaveBeenCalledOnce();
    panel.showTransferToast("Bob", "#0f0");
    expect(container.textContent).toContain("Control transferred to Bob");
    vi.advanceTimersByTime(6_000);
    expect(container.querySelector(".dm-toast")).toBeNull();
    panel.destroy();
  });

  it("updates countdown and queued-keystroke UI and cleans it up", () => {
    vi.useFakeTimers();
    const panel = new DeckMuxControlPanel(container);
    panel.showAutoTransferWarning("Bob", 1);
    vi.advanceTimersByTime(500);
    expect(parseFloat((container.querySelector(".dm-countdown-fill") as HTMLElement).style.width)).toBeCloseTo(50);
    panel.showAutoTransferComplete("Bob");
    expect(container.textContent).toContain("Control transferred to Bob");

    panel.showKeystrokeQueue("u1", "ls", { x: 10, y: 20 });
    panel.showKeystrokeQueue("u1", "ls -la", { x: 30, y: 40 });
    const queue = container.querySelector(".dm-keystroke-queue") as HTMLElement;
    expect(queue.textContent).toBe("ls -la");
    expect(queue.style.left).toBe("30px");
    vi.advanceTimersByTime(2_000);
    expect(container.querySelector(".dm-keystroke-queue")).toBeNull();
    panel.destroy();
  });

  it("positions context menus, wraps actions, and closes on outside click", async () => {
    vi.useFakeTimers();
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => frames.push(callback));
    const panel = new DeckMuxControlPanel(container);
    const action = vi.fn();
    panel.showContextMenu("u1", user, { x: 20, y: 30 }, [{ icon: "x", label: "Action", onClick: action }]);
    const menu = container.querySelector("uterm-context-menu") as ContextMenu;
    vi.spyOn(menu, "getBoundingClientRect").mockReturnValue({
      x: 20, y: 30, left: 20, top: 30, right: 500, bottom: 500, width: 100, height: 80, toJSON: () => ({}),
    });
    vi.stubGlobal("innerWidth", 100);
    vi.stubGlobal("innerHeight", 100);
    frames.shift()?.(0);
    expect(menu.style.left).toBe("-80px");
    expect(menu.style.top).toBe("-50px");
    expect(menu.actions).toHaveLength(1);
    menu.actions[0]?.onClick();
    expect(action).toHaveBeenCalledOnce();
    expect(menu.isConnected).toBe(false);

    panel.showContextMenu("u1", user, { x: 1, y: 2 }, []);
    vi.runOnlyPendingTimers();
    document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(container.querySelector("uterm-context-menu")).toBeNull();
    panel.destroy();
  });
});

describe("DeckMux overlays", () => {
  it("synchronizes cursor pins, selections, ownership, and visibility", () => {
    const overlay = new DeckMuxCursorOverlay(container);
    const element = container.querySelector("uterm-cursor-overlay") as CursorOverlayElement;
    overlay.setPin("u1", 10, "Alice", "#f00", true);
    overlay.setPin("u1", 11, "Alice", "#f00", false);
    overlay.setSelection("u1", 4, 8, "#0f0");
    expect(element.ownerId).toBeNull();
    expect(element.users[0]).toMatchObject({ userId: "u1", pin: { line: 11 }, selection: { startLine: 4, endLine: 8 } });
    overlay.setVisible(false);
    expect(element.visible).toBe(false);
    overlay.removePin("u1");
    overlay.removeSelection("u1");
    overlay.removePin("missing");
    overlay.removeSelection("missing");
    expect(element.users).toEqual([]);
    expect(element.ownerId).toBeNull();
    overlay.destroy();
    expect(element.isConnected).toBe(false);
  });

  it("flashes, hides, removes, and disables ghost dimensions", () => {
    vi.useFakeTimers();
    const overlay = new DeckMuxGhostOverlay(container);
    const element = container.querySelector("uterm-ghost-overlay") as UtermGhostOverlayElement;
    overlay.setOwnDimensions(120, 40);
    overlay.showUser("invalid", "#fff", 0, 1);
    overlay.flashUser("invalid", "#fff", 1, 0);
    overlay.showUser("u1", "#f00", 80, 24);
    expect(element).toMatchObject({ ownCols: 120, ownRows: 40 });
    expect(element.entries[0]).toMatchObject({ userId: "u1", hidden: false });
    overlay.flashUser("u1", "#0f0", 100, 30);
    overlay.flashUser("u1", "#00f", 101, 31);
    expect(element.entries[0]?.flash).toBe(true);
    overlay.hideUser("u1");
    expect(element.entries[0]?.hidden).toBe(false);
    vi.advanceTimersByTime(1_800);
    expect(element.entries[0]).toMatchObject({ flash: false, hidden: true });
    overlay.setVisible(false);
    expect(element.visible).toBe(false);
    overlay.showUser("ignored", "#fff", 1, 1);
    expect(element.entries).toHaveLength(1);
    overlay.removeUser("u1");
    overlay.removeUser("missing");
    expect(element.entries).toEqual([]);
    overlay.destroy();
  });

  it("allocates stable edge slots up to capacity and reuses released slots", () => {
    const edges = new DeckMuxEdgeIndicators(container);
    const element = container.querySelector("uterm-edge-indicators") as EdgeIndicatorsElement;
    for (let i = 0; i < 8; i += 1) edges.setUser(`u${i}`, "#fff", { top: 0, height: 1 }, { name: `U${i}` });
    expect(element.users).toHaveLength(7);
    expect(element.users.map((entry) => entry.slot)).toEqual([0, 1, 2, 3, 4, 5, 6]);
    edges.setUser("u2", "#f00", { top: 0.5, height: 0.2 }, { isOwner: true });
    expect(element.users.find((entry) => entry.userId === "u2")).toMatchObject({ color: "#f00", slot: 2 });
    edges.removeUser("u2");
    edges.removeUser("missing");
    edges.setUser("u8", "#0f0", { top: 0, height: 1 });
    expect(element.users.find((entry) => entry.userId === "u8")?.slot).toBe(2);
    edges.setNamesVisible(true);
    expect(element.namesVisible).toBe(true);
    edges.destroy();
  });
});

describe("DeckMuxPresenceBar", () => {
  it("routes UI events through callbacks and maintains idle/owner state", () => {
    vi.useFakeTimers();
    const bar = new DeckMuxPresenceBar(container, config);
    const element = container.querySelector("uterm-presence-bar") as PresenceBar;
    const callbacks = {
      names: vi.fn(), cursors: vi.fn(), ghost: vi.fn(), click: vi.fn(), hover: vi.fn(), out: vi.fn(),
    };
    bar.onToggleNames = callbacks.names;
    bar.onToggleCursors = callbacks.cursors;
    bar.onToggleGhostBox = callbacks.ghost;
    bar.onAvatarClick = callbacks.click;
    bar.onAvatarHover = callbacks.hover;
    bar.onAvatarHoverOut = callbacks.out;
    element.dispatchEvent(new CustomEvent("presence:toggle-names", { detail: true }));
    element.dispatchEvent(new CustomEvent("presence:toggle-cursors", { detail: false }));
    element.dispatchEvent(new CustomEvent("presence:toggle-ghost-box", { detail: true }));
    element.dispatchEvent(new CustomEvent("presence:click-avatar", { detail: "u1" }));
    element.dispatchEvent(new CustomEvent("presence:hover-avatar", { detail: "u1" }));
    element.dispatchEvent(new CustomEvent("presence:hover-out-avatar", { detail: "u1" }));
    expect(Object.values(callbacks).every((callback) => callback.mock.calls.length === 1)).toBe(true);

    bar.addUser(user);
    bar.addUser({ ...user, name: "Updated" });
    bar.setOwner("u1");
    bar.setUserTyping("u1", true);
    bar.setUserRequesting("u1", true);
    bar.setUserIdle("u1", false);
    bar.updateUser("missing", { name: "Ignored" });
    bar.setUserIdle("missing", true);
    bar.setUserRequesting("missing", true);
    expect(bar.getAvatarElement("missing")).toBeNull();
    expect(element.users[0]).toMatchObject({ name: "Updated", typing: true, requesting: true });
    expect(element.ownerId).toBe("u1");
    vi.advanceTimersByTime(30_000);
    expect(element.users[0]?.idle).toBe(true);
    bar.clearOwner();
    bar.removeUser("u1");
    bar.removeUser("missing");
    expect(element.users).toEqual([]);
    bar.destroy();
  });
});
