//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { DeckMuxPresence, type DeckMuxPresenceOptions } from "./index.ts";

interface Broadcast {
  worker_id: string;
  msg: Record<string, unknown>;
}

interface ServiceGolden {
  joining: {
    first_sync: Record<string, unknown>;
    broadcast_after_first: Broadcast[];
    second_sync: Record<string, unknown>;
    broadcast_after_second: Broadcast[];
    broadcast_after_leave: Broadcast[];
    broadcast_after_leaving_twice: Broadcast[];
  };
  identity: {
    anonymous: Record<string, unknown>;
    named: Record<string, unknown>;
    unnamed_principal: Record<string, unknown>;
    blank_display_name: Record<string, unknown>;
  };
  stable_identity: { first: Record<string, unknown>; again: Record<string, unknown> };
  guards: {
    broadcast_on_claimed_ownership: Broadcast[];
    owner_after_claim: string | null;
    broadcast_on_claimed_identity: Broadcast[];
    broadcast_on_claimed_queue: Broadcast[];
    broadcast_on_selection: Broadcast[];
  };
  colors: { colors: string[]; all_distinct: boolean };
  prune: { ghost_id: string; users_after_join: string[]; ghost_survived: boolean };
  updates: {
    broadcast_on_update: Broadcast[];
    broadcast_on_oversized_pin: Broadcast[];
    state_after_oversized_pin: Record<string, unknown>;
    broadcast_on_unknown_field: Broadcast[];
    broadcast_from_a_stranger: Broadcast[];
    broadcast_on_unknown_type: Broadcast[];
  };
  queue: {
    broadcast_on_queue: Broadcast[];
    broadcast_on_arrow: Broadcast[];
    broadcast_on_no_keys: Broadcast[];
    broadcast_from_a_stranger: Broadcast[];
  };
  control: {
    broadcast_on_grant: Broadcast[];
    owner_after_grant: string | null;
    broadcast_on_refusal: Broadcast[];
    owner_after_refusal: string | null;
    broadcast_on_release: Broadcast[];
    owner_after_release: string | null;
    broadcast_on_grant_with_a_queue: Broadcast[];
  };
  containers: {
    separate_stores: boolean;
    same_store_when_asked_twice: boolean;
    default_idle_s: number;
    default_queue_mode: string;
    configured_idle_s: number;
    configured_queue_mode: string;
    config_ignored_on_second_call: number;
    cleanup_replaces_the_store: boolean;
  };
}

const golden = loadGolden<ServiceGolden>("deckmux_service_golden.json");

/** A hub that records what it was asked to broadcast. */
class RecordingHub {
  readonly sent: Broadcast[] = [];

  async broadcast(workerId: string, message: Record<string, unknown>): Promise<void> {
    this.sent.push({ worker_id: workerId, msg: message });
  }

  /** What has been sent since the last drain. */
  drain(): Broadcast[] {
    return this.sent.splice(0, this.sent.length);
  }
}

/** The same counter the corpus generator ran under. */
function counter(): () => string {
  let next = 0;
  return () => {
    next += 1;
    return next.toString(16).padStart(32, "0");
  };
}

/** A service and its hub, sharing one deterministic identity source. */
function build(options: DeckMuxPresenceOptions = {}): { hub: RecordingHub; service: DeckMuxPresence } {
  const hub = new RecordingHub();
  return { hub, service: new DeckMuxPresence(hub, { newUserId: counter(), ...options }) };
}

/** A browser connection. */
function socket(): object {
  return {};
}

describe("browsers joining and leaving", () => {
  it("replays the recorded arrival and departure", async () => {
    const { hub, service } = build();
    const first = socket();
    const second = socket();

    expect(await service.onBrowserConnect("w1", first, "operator")).toStrictEqual(golden.joining.first_sync);
    expect(hub.drain()).toStrictEqual(golden.joining.broadcast_after_first);

    expect(await service.onBrowserConnect("w1", second, "viewer")).toStrictEqual(golden.joining.second_sync);
    expect(hub.drain()).toStrictEqual(golden.joining.broadcast_after_second);

    await service.onBrowserDisconnect("w1", second);
    expect(hub.drain()).toStrictEqual(golden.joining.broadcast_after_leave);

    await service.onBrowserDisconnect("w1", second);
    expect(hub.drain()).toStrictEqual(golden.joining.broadcast_after_leaving_twice);
  });

  it("tells the first browser about itself and nobody else", () => {
    // There is nobody else to tell, and a broadcast to an empty room is a
    // wasted round trip on every single connect.
    expect(golden.joining.broadcast_after_first).toStrictEqual([]);
    expect((golden.joining.first_sync.users as unknown[]).length).toBe(1);
  });

  it("tells everybody once there is somebody to tell", () => {
    // The joiner has to appear in the others' participant lists, so the whole
    // sync goes out rather than just the new user.
    expect(golden.joining.broadcast_after_second).toHaveLength(1);
    expect((golden.joining.broadcast_after_second[0]?.msg.users as unknown[]).length).toBe(2);
    expect(golden.joining.broadcast_after_second[0]?.msg.type).toBe("presence_sync");
  });

  it("announces a departure so the others drop them", () => {
    // Otherwise a disconnected browser stays in everybody's participant list
    // as a cursor that never moves.
    expect(golden.joining.broadcast_after_leave).toHaveLength(1);
    expect(golden.joining.broadcast_after_leave[0]?.msg.type).toBe("presence_leave");
  });

  it("says nothing when there was nobody to remove", () => {
    // A second disconnect for one connection must not tell everybody that
    // somebody who is already gone has left again.
    expect(golden.joining.broadcast_after_leaving_twice).toStrictEqual([]);
  });
});

describe("naming a participant", () => {
  it("replays the recorded identities", async () => {
    const { service } = build();
    expect(await service.onBrowserConnect("w1", socket(), "viewer")).toStrictEqual(golden.identity.anonymous);
    expect(
      await service.onBrowserConnect("w2", socket(), "viewer", { subjectId: "sre:alice", displayName: "Alice" }),
    ).toStrictEqual(golden.identity.named);
    expect(await service.onBrowserConnect("w3", socket(), "viewer", { subjectId: "sre:bob" })).toStrictEqual(
      golden.identity.unnamed_principal,
    );
    expect(
      await service.onBrowserConnect("w4", socket(), "viewer", { subjectId: "sre:carol", displayName: "" }),
    ).toStrictEqual(golden.identity.blank_display_name);
  });

  it("generates a name for an anonymous browser", () => {
    const user = (golden.identity.anonymous.users as Array<Record<string, unknown>>)[0];
    expect(user?.name).toBeTruthy();
    expect(user?.user_id).not.toBe(user?.name);
  });

  it("keys an authenticated browser on its subject", () => {
    // So the same person across two tabs is one participant, not two.
    const user = (golden.identity.named.users as Array<Record<string, unknown>>)[0];
    expect(user?.user_id).toBe("sre:alice");
    expect(user?.name).toBe("Alice");
  });

  it("falls back to the subject when there is no display name", () => {
    for (const key of ["unnamed_principal", "blank_display_name"] as const) {
      const users = golden.identity[key].users as Array<Record<string, unknown>>;
      expect(users[0]?.name).toBe(users[0]?.user_id);
    }
  });

  it("keeps one connection's identity stable across calls", async () => {
    // A minted identity that changed between connect and update would leave
    // the update unable to find the participant the connect had added.
    const { service } = build();
    const stable = socket();
    const first = await service.onBrowserConnect("w1", stable, "viewer");
    await service.onBrowserDisconnect("w1", stable);
    const again = await service.onBrowserConnect("w1", stable, "viewer");
    const firstId = (first.users as Array<Record<string, unknown>>)[0]?.user_id;
    const againId = (again.users as Array<Record<string, unknown>>)[0]?.user_id;
    expect(againId).toBe(firstId);
    expect(first).toStrictEqual(golden.stable_identity.first);
    expect(again).toStrictEqual(golden.stable_identity.again);
  });

  it("mints a real identity when none is injected", async () => {
    // The default source is what production runs on, so it has to produce
    // something that looks like an identity rather than an empty string.
    const service = new DeckMuxPresence(new RecordingHub());
    const sync = await service.onBrowserConnect("w1", socket(), "viewer");
    const userId = (sync.users as Array<Record<string, unknown>>)[0]?.user_id as string;
    expect(userId).toMatch(/^[0-9a-f]{32}$/);
  });

  it("gives two connections different identities", async () => {
    // The reference is explicit that an address is not an identity: CPython
    // reuses one after collection, so a browser connecting now could inherit
    // the presence and the ownership of one that just left.
    const { service } = build();
    const one = await service.onBrowserConnect("w1", socket(), "viewer");
    const two = await service.onBrowserConnect("w1", socket(), "viewer");
    const ids = (two.users as Array<Record<string, unknown>>).map((user) => user.user_id);
    expect(new Set(ids).size).toBe(2);
    expect((one.users as unknown[]).length).toBe(1);
  });
});

describe("a presence update", () => {
  /** A session with one connected browser, drained. */
  async function seated() {
    const { hub, service } = build();
    const ws = socket();
    await service.onBrowserConnect("w1", ws, "operator");
    hub.drain();
    return { hub, service, ws };
  }

  it("replays the recorded broadcasts", async () => {
    const { hub, service, ws } = await seated();

    await service.handleMessage("w1", ws, { type: "presence_update", scroll_line: 5, typing: true });
    expect(hub.drain()).toStrictEqual(golden.updates.broadcast_on_update);

    await service.handleMessage("w1", ws, { type: "presence_update", scroll_line: 99, pin: { a: "x".repeat(4000) } });
    expect(hub.drain()).toStrictEqual(golden.updates.broadcast_on_oversized_pin);

    await service.handleMessage("w1", ws, { type: "presence_update", nonsense: 1 });
    expect(hub.drain()).toStrictEqual(golden.updates.broadcast_on_unknown_field);

    await service.handleMessage("w1", socket(), { type: "presence_update", scroll_line: 1 });
    expect(hub.drain()).toStrictEqual(golden.updates.broadcast_from_a_stranger);

    await service.handleMessage("w1", ws, { type: "nonsense" });
    expect(hub.drain()).toStrictEqual(golden.updates.broadcast_on_unknown_type);
  });

  it("carries the sender's whole record, not just what changed", () => {
    // A joiner that missed earlier updates would otherwise render a
    // participant with holes in them.
    const message = golden.updates.broadcast_on_update[0]?.msg;
    expect(message?.type).toBe("presence_update");
    expect(message?.scroll_line).toBe(5);
    expect(message?.color).toBeTruthy();
    expect(message?.name).toBeTruthy();
  });

  it("drops an oversized value without tearing the session down", async () => {
    // The store raises on it; swallowing that is what keeps a browser from
    // being able to end everybody else's session by sending one message.
    expect(golden.updates.broadcast_on_oversized_pin).toStrictEqual([]);
    const { hub, service, ws } = await seated();
    await expect(
      service.handleMessage("w1", ws, { type: "presence_update", pin: { a: "x".repeat(4000) } }),
    ).resolves.toBeUndefined();
    expect(hub.drain()).toStrictEqual([]);
  });

  it("leaves the rest of a rejected update unapplied", () => {
    // The scroll_line in that same message was 99 and did not land.
    expect(golden.updates.state_after_oversized_pin.scroll_line).toBe(5);
    expect(golden.updates.state_after_oversized_pin.pin).toBeNull();
  });

  it("ignores a field that is not in the allow-list", () => {
    // Filtered rather than refused, so an unknown field from a newer frontend
    // does not cost the whole update.
    expect(golden.updates.broadcast_on_unknown_field).toHaveLength(1);
    expect(golden.updates.broadcast_on_unknown_field[0]?.msg.nonsense).toBeUndefined();
  });

  it("says nothing for a browser it has never seen", () => {
    expect(golden.updates.broadcast_from_a_stranger).toStrictEqual([]);
  });

  it("ignores a message type it does not route", () => {
    expect(golden.updates.broadcast_on_unknown_type).toStrictEqual([]);
  });

  it("re-arms the transfer warning when the owner is typing", async () => {
    // The owner is at the keyboard, so the countdown to taking it from them
    // starts again.
    const { service, ws } = await seated();
    const store = service.getPresenceStore("w1");
    store.setOwner((store.getAll()[0] as { userId: string }).userId);
    const manager = service.getTransferManager("w1");
    manager.checkAutoTransfer(25, ["someone"]);
    expect(manager.checkAutoTransfer(25, ["someone"])).toStrictEqual({ shouldWarn: false, shouldTransfer: false });

    await service.handleMessage("w1", ws, { type: "presence_update", typing: true });
    expect(manager.checkAutoTransfer(25, ["someone"])).toStrictEqual({ shouldWarn: true, shouldTransfer: false });
  });

  it("does not re-arm it for somebody who is not the owner", async () => {
    const { service, ws } = await seated();
    const manager = service.getTransferManager("w1");
    manager.checkAutoTransfer(25, ["someone"]);
    await service.handleMessage("w1", ws, { type: "presence_update", typing: true });
    expect(manager.checkAutoTransfer(25, ["someone"])).toStrictEqual({ shouldWarn: false, shouldTransfer: false });
  });

  it("does not re-arm it when the owner is not typing", async () => {
    const { service, ws } = await seated();
    const store = service.getPresenceStore("w1");
    store.setOwner((store.getAll()[0] as { userId: string }).userId);
    const manager = service.getTransferManager("w1");
    manager.checkAutoTransfer(25, ["someone"]);
    await service.handleMessage("w1", ws, { type: "presence_update", scroll_line: 3 });
    expect(manager.checkAutoTransfer(25, ["someone"])).toStrictEqual({ shouldWarn: false, shouldTransfer: false });
  });
});

describe("keystrokes from somebody who cannot type yet", () => {
  it("replays the recorded broadcasts", async () => {
    const { hub, service } = build();
    const ws = socket();
    await service.onBrowserConnect("w1", ws, "viewer");
    hub.drain();

    await service.handleMessage("w1", ws, { type: "queued_input", keys: "ls" });
    expect(hub.drain()).toStrictEqual(golden.queue.broadcast_on_queue);

    await service.handleMessage("w1", ws, { type: "queued_input", keys: "[A" });
    expect(hub.drain()).toStrictEqual(golden.queue.broadcast_on_arrow);

    await service.handleMessage("w1", ws, { type: "queued_input" });
    expect(hub.drain()).toStrictEqual(golden.queue.broadcast_on_no_keys);

    await service.handleMessage("w1", socket(), { type: "queued_input", keys: "rm" });
    expect(hub.drain()).toStrictEqual(golden.queue.broadcast_from_a_stranger);
  });

  it("shows the others what is being typed at them", () => {
    // The queue is visible so the owner can see somebody waiting rather than
    // being surprised when control moves.
    expect(golden.queue.broadcast_on_queue[0]?.msg.queued_keys).toBe("ls");
    expect(golden.queue.broadcast_on_arrow[0]?.msg.queued_keys).toBe("ls↑");
  });

  it("treats a message with no keys as no keys", () => {
    // Rather than as the string "undefined" appearing in everybody's view.
    expect(golden.queue.broadcast_on_no_keys[0]?.msg.queued_keys).toBe("ls↑");
  });

  it("says nothing for a browser it has never seen", () => {
    expect(golden.queue.broadcast_from_a_stranger).toStrictEqual([]);
  });
});

describe("who gets the terminal", () => {
  /** A session with two connected browsers, drained. */
  async function twoUp() {
    const { hub, service } = build();
    const first = socket();
    const second = socket();
    await service.onBrowserConnect("w1", first, "operator");
    await service.onBrowserConnect("w1", second, "viewer");
    hub.drain();
    return { hub, service, first, second };
  }

  it("replays the recorded decisions", async () => {
    const { hub, service, first, second } = await twoUp();

    await service.handleMessage("w1", first, { type: "control_request" });
    expect(hub.drain()).toStrictEqual(golden.control.broadcast_on_grant);
    expect(service.getPresenceStore("w1").getOwner()?.userId).toBe(golden.control.owner_after_grant);

    await service.handleMessage("w1", second, { type: "control_request" });
    expect(hub.drain()).toStrictEqual(golden.control.broadcast_on_refusal);
    expect(service.getPresenceStore("w1").getOwner()?.userId).toBe(golden.control.owner_after_refusal);

    await service.handleMessage("w1", first, { type: "control_request" });
    expect(hub.drain()).toStrictEqual(golden.control.broadcast_on_release);
    expect(service.getPresenceStore("w1").getOwner()).toBeUndefined();

    await service.handleMessage("w1", second, { type: "queued_input", keys: "ls" });
    hub.drain();
    await service.handleMessage("w1", second, { type: "control_request" });
    expect(hub.drain()).toStrictEqual(golden.control.broadcast_on_grant_with_a_queue);
  });

  it("grants it when nobody holds it", () => {
    expect(golden.control.broadcast_on_grant).toHaveLength(1);
    expect(golden.control.broadcast_on_grant[0]?.msg.type).toBe("control_transfer");
    expect(golden.control.broadcast_on_grant[0]?.msg.from_user_id).toBe("");
  });

  it("ignores anybody else asking while it is held", () => {
    // Without this any viewer could take the terminal out from under whoever
    // is typing into it.
    expect(golden.control.broadcast_on_refusal).toStrictEqual([]);
    expect(golden.control.owner_after_refusal).toBe(golden.control.owner_after_grant);
  });

  it("releases it when the holder asks again", () => {
    // The same message both takes and gives up control, so a frontend needs
    // only one button.
    expect(golden.control.broadcast_on_release).toHaveLength(1);
    expect(golden.control.broadcast_on_release[0]?.msg.to_user_id).toBe("");
    expect(golden.control.owner_after_release).toBeNull();
  });

  it("hands the new owner what they typed while they waited", () => {
    expect(golden.control.broadcast_on_grant_with_a_queue[0]?.msg.queued_keys).toBe("ls");
  });
});

describe("what a browser may not say about itself", () => {
  /** Two seated browsers, drained. */
  async function twoUp() {
    const { hub, service } = build();
    const first = socket();
    const second = socket();
    await service.onBrowserConnect("w1", first, "operator");
    await service.onBrowserConnect("w1", second, "viewer");
    hub.drain();
    return { hub, service, first, second };
  }

  it("replays the recorded refusals", async () => {
    const { hub, service, second } = await twoUp();

    await service.handleMessage("w1", second, { type: "presence_update", is_owner: true });
    expect(hub.drain()).toStrictEqual(golden.guards.broadcast_on_claimed_ownership);
    expect(service.getPresenceStore("w1").getOwner()).toBeUndefined();

    await service.handleMessage("w1", second, { type: "presence_update", color: "#000000", name: "Administrator" });
    expect(hub.drain()).toStrictEqual(golden.guards.broadcast_on_claimed_identity);

    await service.handleMessage("w1", second, { type: "presence_update", queued_keys: "rm -rf /" });
    expect(hub.drain()).toStrictEqual(golden.guards.broadcast_on_claimed_queue);

    const selection = { start: { row: 1, col: 2 }, end: { row: 3, col: 4 } };
    await service.handleMessage("w1", second, { type: "presence_update", selection });
    expect(hub.drain()).toStrictEqual(golden.guards.broadcast_on_selection);
  });

  it("will not let a browser make itself the owner", () => {
    // Ownership is the right to type into the terminal. If a browser could
    // set it, the whole control-request path would be decoration.
    expect(golden.guards.owner_after_claim).toBeNull();
    expect(golden.guards.broadcast_on_claimed_ownership[0]?.msg.is_owner).toBe(false);
  });

  it("will not let a browser rename or recolour itself", () => {
    // Both are how the others tell participants apart; a browser that could
    // set them could impersonate somebody already in the room.
    const message = golden.guards.broadcast_on_claimed_identity[0]?.msg;
    expect(message?.name).not.toBe("Administrator");
    expect(message?.color).not.toBe("#000000");
  });

  it("will not let a browser write its own queue display", () => {
    // The display is what the others are shown of somebody waiting to type.
    expect(golden.guards.broadcast_on_claimed_queue[0]?.msg.queued_keys).toBe("");
  });

  it("does carry a selection, which is theirs to set", () => {
    // The allow-list is not a wall around everything — a selection is what a
    // browser is for.
    expect(golden.guards.broadcast_on_selection[0]?.msg.selection).toStrictEqual({
      start: { row: 1, col: 2 },
      end: { row: 3, col: 4 },
    });
  });
});

describe("telling participants apart", () => {
  it("gives everybody in a session a different colour", async () => {
    // Two people rendered identically cannot be told apart in a terminal
    // they are both watching.
    // As many browsers as there are colours, so the walk has to run: in a
    // smaller room the natural picks may happen not to collide at all.
    const { service } = build();
    for (let index = 0; index < golden.colors.colors.length; index += 1) {
      await service.onBrowserConnect("w1", socket(), "viewer");
    }
    const colors = service
      .getPresenceStore("w1")
      .getAll()
      .map((user) => user.color);
    expect(colors).toStrictEqual(golden.colors.colors);
    expect(new Set(colors).size).toBe(colors.length);
    expect(golden.colors.all_distinct).toBe(true);
  });
});

describe("a browser that dropped without saying so", () => {
  it("is cleared out by the next joiner", async () => {
    // Otherwise it holds a colour and a slot in everybody's participant list
    // forever, as a cursor that never moves.
    const { service } = build();
    await service.onBrowserConnect("w1", socket(), "viewer");
    const ghost = service.getPresenceStore("w1").getAll()[0] as { userId: string; lastActivityAt: number };
    ghost.lastActivityAt = Date.now() / 1000 - 100;

    const sync = await service.onBrowserConnect("w1", socket(), "viewer");
    const ids = (sync.users as Array<Record<string, unknown>>).map((user) => user.user_id);
    expect(ids).toStrictEqual(golden.prune.users_after_join);
    expect(ids).not.toContain(ghost.userId);
    expect(golden.prune.ghost_survived).toBe(false);
  });

  it("does not prune the joiner along with them", async () => {
    // Somebody added a moment ago is never idle by their own clock, so the
    // sweep's position either side of the add reaches the same room. It is
    // written the reference's way round because pruning first is what the
    // sweep is for — clearing the slot the joiner is about to take.
    const { service } = build({ now: () => 1_000 });
    const sync = await service.onBrowserConnect("w1", socket(), "viewer");
    expect((sync.users as unknown[]).length).toBe(1);
  });
});

describe("per-session containers", () => {
  it("keeps one session's participants out of another's", async () => {
    const { service } = build();
    const storeA = service.getPresenceStore("w1");
    expect(service.getPresenceStore("w2")).not.toBe(storeA);
    expect(service.getPresenceStore("w1")).toBe(storeA);
    expect(golden.containers.separate_stores).toBe(true);
    expect(golden.containers.same_store_when_asked_twice).toBe(true);
  });

  it("builds a transfer manager from the session's settings", () => {
    const { service } = build();
    expect(service.getTransferManager("w1").queueMode).toBe(golden.containers.default_queue_mode);
    const configured = service.getTransferManager("w2", { auto_transfer_idle_s: 5, keystroke_queue: "replay" });
    expect(configured.queueMode).toBe(golden.containers.configured_queue_mode);
    expect(configured.autoTransferEnabled).toBe(true);
  });

  it("uses the configured idle window rather than the default", () => {
    // A session told to hand over after five seconds must not sit on the
    // thirty-second default; the whole point of the setting is when control
    // moves.
    const { service } = build();
    const configured = service.getTransferManager("w1", { auto_transfer_idle_s: golden.containers.configured_idle_s });
    expect(configured.checkAutoTransfer(golden.containers.configured_idle_s, ["u2"])).toStrictEqual({
      shouldWarn: false,
      shouldTransfer: true,
    });
    const standard = service.getTransferManager("w2");
    expect(standard.checkAutoTransfer(golden.containers.configured_idle_s, ["u2"])).toStrictEqual({
      shouldWarn: false,
      shouldTransfer: false,
    });
    expect(golden.containers.default_idle_s).toBe(30);
  });

  it("ignores settings once the manager exists", () => {
    // The queue and the warning state live on it; rebuilding it mid-session
    // would drop what somebody had typed.
    const { service } = build();
    const first = service.getTransferManager("w1", { auto_transfer_idle_s: 5, keystroke_queue: "replay" });
    expect(service.getTransferManager("w1", { auto_transfer_idle_s: 999 })).toBe(first);
    expect(golden.containers.config_ignored_on_second_call).toBe(golden.containers.configured_idle_s);
  });

  it("forgets a session on cleanup", async () => {
    // Otherwise every session that ever ran stays in memory with its
    // participants.
    const { service } = build();
    const store = service.getPresenceStore("w1");
    const manager = service.getTransferManager("w1");
    service.cleanup("w1");
    expect(service.getPresenceStore("w1")).not.toBe(store);
    expect(service.getTransferManager("w1")).not.toBe(manager);
    expect(golden.containers.cleanup_replaces_the_store).toBe(true);
  });

  it("copes with cleaning up a session it does not have", () => {
    const { service } = build();
    expect(() => service.cleanup("nobody")).not.toThrow();
  });
});
