//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ContextMenu } from "./context-menu.js";
import type { CursorOverlayElement } from "./cursor-overlay-element.js";
import type { EdgeIndicatorsElement } from "./edge-indicators-element.js";
import { DeckMux } from "./deckmux.js";
import type { PresenceBar } from "./presence-bar-element.js";
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
  vi.useRealTimers();
  dm.destroy();
  parent.remove();
});

function clickAvatar(userId: string): void {
  // Avatars live in the presence bar host inserted before the terminal.
  const presenceBar = parent.querySelector("uterm-presence-bar");
  if (!presenceBar?.shadowRoot) throw new Error("presence bar not found");

  const wraps = presenceBar.shadowRoot.querySelectorAll<HTMLElement>(".dm-avatar-wrap");
  let wrap: HTMLElement | null = null;
  for (const w of wraps) {
    if (w.dataset.userId === userId) wrap = w;
  }
  if (!wrap) throw new Error(`avatar for ${userId} not found`);
  wrap.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

function addPinnedUser(userId: string, name: string, color: string): void {
  dm.handleMessage({ type: "dm_join", user_id: userId, name, color });
  dm.handleMessage({ type: "dm_presence", user_id: userId, pin: { line: userId === "alice" ? 4 : 8 } });
}

async function expectProjectedOwner(ownerId: string | null): Promise<void> {
  const edges = terminalContainer.querySelector("uterm-edge-indicators") as EdgeIndicatorsElement;
  const cursors = terminalContainer.querySelector("uterm-cursor-overlay") as CursorOverlayElement;
  await Promise.all([edges.updateComplete, cursors.updateComplete]);

  expect(edges.users.filter((user) => user.options.isOwner).map((user) => user.userId)).toEqual(
    ownerId === null ? [] : [ownerId],
  );
  const ownerBars = Array.from(edges.shadowRoot?.querySelectorAll<HTMLElement>(".dm-edge-bar--owner") ?? []);
  expect(ownerBars.map((bar) => bar.dataset.userId)).toEqual(ownerId === null ? [] : [ownerId]);
  expect(cursors.ownerId).toBe(ownerId);
  for (const pin of Array.from(cursors.shadowRoot?.querySelectorAll<HTMLElement>(".dm-pin") ?? [])) {
    const expectedOwner = pin.dataset.userId === ownerId;
    expect(pin.classList.contains("dm-pin--owner")).toBe(expectedOwner);
    expect(pin.textContent).toContain(expectedOwner ? "⌨️" : "📌");
  }
}

describe("DeckMux avatar lookup", () => {
  it("avatar lookup survives a userId with CSS-special chars", async () => {
    const userId = 'a"]b';
    dm.handleMessage({ type: "dm_join", user_id: userId, name: "x", color: "#fff" });

    const pb = parent.querySelector("uterm-presence-bar") as any;
    if (pb) await pb.updateComplete;

    expect(() => clickAvatar(userId)).not.toThrow();
  });

  it("coordinates join, presence, ownership, queued keys, and leave", () => {
    dm.handleMessage({ type: "dm_hello", user_id: "me" });
    dm.handleMessage({
      type: "dm_join", user_id: "me", name: "Me User", color: "#f00", cols: 80, rows: 24,
      scroll_range: [0, 20], total_lines: 100, is_owner: true,
    });
    dm.handleMessage({
      type: "dm_presence", user_id: "me", name: "Myself", typing: true, cols: 100, rows: 30,
      pin: { line: 8 }, selection: { start: { line: 2, col: 0 }, end: { line: 4, col: 5 } },
      queued_keys: "ls -la",
    });

    const presence = parent.querySelector("uterm-presence-bar") as PresenceBar;
    const cursors = terminalContainer.querySelector("uterm-cursor-overlay") as CursorOverlayElement;
    const edges = terminalContainer.querySelector("uterm-edge-indicators") as EdgeIndicatorsElement;
    expect(presence.users[0]).toMatchObject({ userId: "me", name: "Myself", typing: true, cols: 100, rows: 30 });
    expect(cursors.users[0]).toMatchObject({ userId: "me", pin: { line: 8 } });
    expect(edges.users[0]?.options.selection).toBeDefined();
    expect(terminalContainer.textContent).toContain("ls -la");

    dm.handleMessage({ type: "dm_owner_change", user_id: "me" });
    expect(presence.ownerId).toBe("me");
    dm.handleMessage({ type: "dm_presence", user_id: "me", pin: null, selection: null, typing: false });
    expect(cursors.users).toEqual([]);
    dm.handleMessage({ type: "dm_leave", user_id: "me" });
    expect(presence.users).toEqual([]);
    expect(edges.users).toEqual([]);
  });

  it("turns owner control decisions into host events and transfer toasts", () => {
    dm.handleMessage({ type: "dm_hello", user_id: "owner" });
    dm.handleMessage({ type: "dm_join", user_id: "owner", name: "Owner", color: "#f00", is_owner: true });
    dm.handleMessage({ type: "dm_join", user_id: "guest", name: "Guest", color: "#0f0" });
    const response = vi.fn();
    terminalContainer.addEventListener("deckmux:control_response", response);

    dm.handleMessage({ type: "dm_control_request", from_user_id: "guest", from_name: "Guest", from_color: "#0f0" });
    (terminalContainer.querySelector(".dm-toast-btn--accept") as HTMLButtonElement).click();
    expect(response).toHaveBeenCalledOnce();
    expect((response.mock.calls[0]?.[0] as CustomEvent).detail).toEqual({ toUserId: "guest", response: "accept" });

    dm.handleMessage({ type: "dm_control_transfer", to_user_id: "guest", to_name: "Guest", to_color: "#0f0" });
    expect(terminalContainer.textContent).toContain("Control transferred to Guest");
    dm.handleMessage({ type: "dm_auto_transfer_warning", to_name: "Guest", seconds_remaining: 5 });
    expect(terminalContainer.textContent).toContain("Auto-transferring control");
    dm.handleMessage({ type: "dm_auto_transfer_warning", to_name: "Guest", complete: true });
    expect(terminalContainer.textContent).toContain("Control transferred to Guest");
  });

  it("reconciles snapshots and exposes context actions for control roles", async () => {
    dm.handleMessage({ type: "dm_hello", user_id: "owner" });
    dm.handleMessage({
      type: "dm_snapshot",
      owner_id: "owner",
      users: [
        { user_id: "owner", name: "Owner User", color: "#f00", is_owner: true },
        { user_id: "guest", name: "Guest User", color: "#0f0" },
        null,
      ],
    });
    const presence = parent.querySelector("uterm-presence-bar") as PresenceBar;
    await presence.updateComplete;
    clickAvatar("guest");
    const menu = terminalContainer.querySelector("uterm-context-menu") as ContextMenu;
    const labels = menu.actions.map((action) => action.label);
    expect(labels).toEqual(["Jump to view", "Hand off control", "Kick user"]);

    const events = { jump: vi.fn(), handoff: vi.fn(), kick: vi.fn() };
    terminalContainer.addEventListener("deckmux:jump_to_view", events.jump);
    terminalContainer.addEventListener("deckmux:hand_off", events.handoff);
    terminalContainer.addEventListener("deckmux:kick_user", events.kick);
    for (const action of menu.actions) action.onClick();
    expect(Object.values(events).every((listener) => listener.mock.calls.length === 1)).toBe(true);
  });

  it("supports disabling, idempotent enabling, and ghost visibility toggles", () => {
    dm.enable(CONFIG);
    expect(parent.querySelectorAll(".dm-bar-host")).toHaveLength(1);
    dm.setOwnDimensions(120, 40);
    dm.handleMessage({ type: "unknown" });
    dm.handleMessage({});
    dm.disable();
    expect(parent.querySelector("uterm-presence-bar")).toBeNull();
    dm.handleMessage({ type: "dm_join", user_id: "ignored", name: "Ignored", color: "#fff" });
    dm.disable();
  });

  it("rejects malformed and out-of-order collaboration messages safely", () => {
    dm.handleMessage({ type: "dm_hello", user_id: 1 });
    dm.handleMessage({ type: "dm_join", user_id: 1, name: "Bad", color: "#fff" });
    dm.handleMessage({ type: "dm_join", user_id: "bad", name: 1, color: "#fff" });
    dm.handleMessage({ type: "dm_join", user_id: "bad", name: "Bad", color: 1 });
    dm.handleMessage({ type: "dm_leave", user_id: 1 });
    dm.handleMessage({ type: "dm_presence", user_id: 1 });
    dm.handleMessage({ type: "dm_presence", user_id: "missing" });
    dm.handleMessage({ type: "dm_owner_change", user_id: null });
    dm.handleMessage({ type: "dm_control_request", from_user_id: 1, from_name: "x", from_color: "#fff" });
    dm.handleMessage({ type: "dm_control_request", from_user_id: "x", from_name: 1, from_color: "#fff" });
    dm.handleMessage({ type: "dm_control_request", from_user_id: "x", from_name: "x", from_color: 1 });
    dm.handleMessage({ type: "dm_control_request", from_user_id: "x", from_name: "x", from_color: "#fff" });
    dm.handleMessage({ type: "dm_control_transfer", to_user_id: 1, to_name: "x", to_color: "#fff" });
    dm.handleMessage({ type: "dm_control_transfer", to_user_id: "x", to_name: 1, to_color: "#fff" });
    dm.handleMessage({ type: "dm_control_transfer", to_user_id: "x", to_name: "x", to_color: 1 });
    dm.handleMessage({ type: "dm_auto_transfer_warning", to_name: 1, seconds_remaining: "soon" });
    dm.handleMessage({ type: "dm_snapshot", users: "bad" });
    dm.handleMessage({ type: "dm_snapshot", users: [null, 1, {}, { user_id: "bad", name: "Bad", color: 1 }] });
    expect(terminalContainer.querySelector(".dm-toast")).toBeNull();
  });

  it("updates existing snapshots and handles request/claim context actions", async () => {
    dm.handleMessage({ type: "dm_hello", user_id: "viewer" });
    dm.handleMessage({ type: "dm_join", user_id: "viewer", name: "V", color: "#00f" });
    dm.handleMessage({ type: "dm_join", user_id: "owner", name: "Owner", color: "#f00", is_owner: true });
    dm.handleMessage({
      type: "dm_snapshot",
      owner_id: "owner",
      users: [{ user_id: "viewer", name: "Viewer Updated", color: "#0ff", role: "admin" }],
    });
    const presence = parent.querySelector("uterm-presence-bar") as PresenceBar;
    await presence.updateComplete;
    clickAvatar("owner");
    let menu = terminalContainer.querySelector("uterm-context-menu") as ContextMenu;
    expect(menu.actions.map((action) => action.label)).toEqual(["Jump to view", "Request control"]);
    menu.remove();

    dm.handleMessage({ type: "dm_owner_change", user_id: null });
    await presence.updateComplete;
    clickAvatar("viewer");
    menu = terminalContainer.querySelector("uterm-context-menu") as ContextMenu;
    expect(menu.actions.map((action) => action.label)).toEqual(["Jump to view", "Claim control"]);
  });

  it("routes bar toggles and hover only for users with terminal dimensions", async () => {
    dm.handleMessage({ type: "dm_join", user_id: "zero", name: "Zero", color: "#fff" });
    const presence = parent.querySelector("uterm-presence-bar") as PresenceBar;
    await presence.updateComplete;
    presence.dispatchEvent(new CustomEvent("presence:toggle-names", { detail: true }));
    presence.dispatchEvent(new CustomEvent("presence:toggle-cursors", { detail: false }));
    presence.dispatchEvent(new CustomEvent("presence:toggle-ghost-box", { detail: false }));
    presence.dispatchEvent(new CustomEvent("presence:hover-avatar", { detail: "missing" }));
    presence.dispatchEvent(new CustomEvent("presence:hover-avatar", { detail: "zero" }));
    dm.handleMessage({ type: "dm_presence", user_id: "zero", cols: 80, rows: 24 });
    presence.dispatchEvent(new CustomEvent("presence:hover-avatar", { detail: "zero" }));
    presence.dispatchEvent(new CustomEvent("presence:hover-out-avatar", { detail: "zero" }));
    const edges = terminalContainer.querySelector("uterm-edge-indicators") as EdgeIndicatorsElement;
    const cursors = terminalContainer.querySelector("uterm-cursor-overlay") as CursorOverlayElement;
    expect(edges.namesVisible).toBe(true);
    expect(cursors.visible).toBe(false);
  });

  it("normalizes complete and sparse user updates across wire variants", () => {
    dm.handleMessage({
      type: "dm_join", user_id: "full", name: "", color: "#123", role: "admin",
      scroll_line: 5, scroll_range: [5, 15], total_lines: 50, cols: 120, rows: 40,
      selection: { start: {}, end: {} }, pin: { line: "bad" }, typing: true, queued_keys: "x", is_owner: true,
    });
    dm.handleMessage({
      type: "dm_presence", user_id: "full", name: "Full User", color: "#456", role: "viewer",
      scroll_line: 10, scroll_range: [10, 20], total_lines: 100, cols: 100, rows: 30,
      selection: { start: { line: 1 }, end: { line: 2 } }, pin: { line: 3 }, typing: false,
      queued_keys: "keys", is_owner: false,
    });
    const presence = parent.querySelector("uterm-presence-bar") as PresenceBar;
    expect(presence.users[0]).toMatchObject({
      name: "Full User", role: "viewer", scrollLine: 10, totalLines: 100, cols: 100, rows: 30,
    });

    const internals = dm as unknown as {
      _handleAvatarClick: (id: string) => void;
      _presenceBar: { getAvatarElement: (id: string) => HTMLElement | null };
      _barContainer: HTMLElement | null;
    };
    internals._handleAvatarClick("missing");
    internals._presenceBar.getAvatarElement = () => null;
    internals._handleAvatarClick("full");
    expect((terminalContainer.querySelector("uterm-context-menu") as ContextMenu).style.left).toBe("0px");
  });

  it("projects control transfers into presence, edge, and pinned-cursor ownership", async () => {
    addPinnedUser("alice", "Alice", "#f00");
    addPinnedUser("bob", "Bob", "#0f0");
    dm.handleMessage({ type: "dm_owner_change", user_id: "alice" });
    await expectProjectedOwner("alice");

    dm.handleMessage({ type: "dm_control_transfer", to_user_id: "bob", to_name: "Bob", to_color: "#0f0" });
    await expectProjectedOwner("bob");
  });

  it("clears owner projection from edge bars and pinned cursors", async () => {
    addPinnedUser("alice", "Alice", "#f00");
    dm.handleMessage({ type: "dm_owner_change", user_id: "alice" });
    await expectProjectedOwner("alice");

    dm.handleMessage({ type: "dm_owner_change", user_id: null });
    await expectProjectedOwner(null);
  });

  it("reconciles snapshot ownership across every visual projection", async () => {
    addPinnedUser("alice", "Alice", "#f00");
    addPinnedUser("bob", "Bob", "#0f0");
    dm.handleMessage({ type: "dm_owner_change", user_id: "alice" });
    await expectProjectedOwner("alice");

    dm.handleMessage({
      type: "dm_snapshot",
      owner_id: "bob",
      users: [
        { user_id: "alice", name: "Alice", color: "#f00", pin: { line: 4 } },
        { user_id: "bob", name: "Bob", color: "#0f0", pin: { line: 8 } },
      ],
    });
    await expectProjectedOwner("bob");
  });
});
