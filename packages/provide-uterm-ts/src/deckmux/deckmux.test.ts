//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  encodeKeysDisplay,
  KEY_SYMBOLS,
  MAX_PRESENCE_DICT_BYTES,
  MAX_PRESENCE_DICT_KEYS,
  MSG_AUTO_TRANSFER_WARNING,
  MSG_CONTROL_REQUEST,
  MSG_CONTROL_TRANSFER,
  MSG_PRESENCE_LEAVE,
  MSG_PRESENCE_SYNC,
  MSG_PRESENCE_UPDATE,
  MSG_QUEUED_INPUT,
  makeControlTransfer,
  makePresenceLeave,
  makePresenceSync,
  makePresenceUpdate,
  PresenceStore,
  presenceToWire,
  VALIDATED_PRESENCE_FIELDS,
} from "./index.ts";

interface DeckmuxGolden {
  key_symbols: Record<string, string>;
  keys: Array<{ name: string; raw: string; display: string }>;
  validation: Array<{
    name: string;
    field: string;
    value: unknown;
    error: string | null;
    stored_is_unchanged: boolean;
  }>;
  partial_update: { error: string; scroll_line_after: number; pin_after: unknown };
  store: Record<string, unknown>;
  idle: { idle_states: Record<string, boolean>; pruned: string[]; remaining: string[] };
  messages: Record<string, unknown>;
  max_dict_bytes: number;
  max_dict_keys: number;
  validated_fields: string[];
  default_user: Record<string, unknown>;
}

const golden = loadGolden<DeckmuxGolden>("deckmux_golden.json");
const NOW = 1000;

/** A store on a fixed clock, holding one user. */
function storeWith(userId = "u1") {
  const store = new PresenceStore({ now: () => NOW });
  store.add(userId, "Alice", "#ff0000", "operator", "AL");
  return store;
}

/** Run and return the refusal, or nothing. */
function refused(call: () => void): string | null {
  try {
    call();
  } catch (error) {
    return (error as Error).message;
  }
  return null;
}

describe("showing what somebody typed", () => {
  it.each(golden.keys)("$name", (record) => {
    expect(encodeKeysDisplay(record.raw)).toBe(record.display);
  });

  it("matches a three-character escape before its first character", () => {
    // Otherwise an arrow key renders as an escape symbol followed by two
    // stray letters, which is what everybody else would believe was typed.
    const arrow = golden.keys.find((entry) => entry.name === "an arrow key");
    expect(arrow?.display).toBe("↑");
    expect(arrow?.display).not.toContain("⎋");
  });

  it("falls back to the escape symbol when the sequence is not one", () => {
    expect(golden.keys.find((entry) => entry.name === "an escape that is not a sequence")?.display).toBe("⎋Z");
    expect(golden.keys.find((entry) => entry.name === "a truncated escape at the end")?.display).toBe("ab⎋[");
  });

  it("drops a control character it has no symbol for", () => {
    // It has no visible form; drawing the raw byte would corrupt the display.
    expect(golden.keys.find((entry) => entry.name === "a control character with no symbol")?.display).toBe("");
  });

  it("keeps a space, which is printable", () => {
    expect(golden.keys.find((entry) => entry.name === "a space")?.display).toBe(" ");
  });

  it("keeps text unchanged", () => {
    for (const name of ["plain text", "unicode"]) {
      const record = golden.keys.find((entry) => entry.name === name);
      expect(record?.display).toBe(record?.raw);
    }
  });

  it("uses the recorded symbols", () => {
    expect(KEY_SYMBOLS).toStrictEqual(golden.key_symbols);
  });
});

describe("an untrusted presence value", () => {
  it.each(golden.validation)("$name", (record) => {
    const store = storeWith();
    // The corpus cannot carry a value JSON has no form for, so that one case
    // is rebuilt here: a function stands in for the reference's bare object,
    // and both are measured by their string form.
    const value = record.value === "<unserialisable>" ? { when: () => undefined } : record.value;
    expect(refused(() => store.update("u1", { [record.field]: value }))).toBe(record.error);
  });

  it("accepts a real selection and a real pin", () => {
    for (const name of ["a real selection", "a real pin", "clearing it"]) {
      expect(golden.validation.find((entry) => entry.name === name)?.error).toBeNull();
    }
  });

  it("refuses anything that is not an object", () => {
    // It is stored and re-broadcast verbatim; a string where a shape was
    // expected reaches every joiner.
    for (const name of ["not a dict at all", "a list", "a number"]) {
      expect(golden.validation.find((entry) => entry.name === name)?.error).toContain("must be a dict or None");
    }
  });

  it("bounds the number of keys", () => {
    expect(golden.validation.find((entry) => entry.name === "exactly the key limit")?.error).toBeNull();
    expect(golden.validation.find((entry) => entry.name === "one key too many")?.error).toContain("too many keys");
  });

  it("bounds the encoded size", () => {
    // Unbounded, this is memory amplification with an audience.
    expect(golden.validation.find((entry) => entry.name === "just under the byte limit")?.error).toBeNull();
    expect(golden.validation.find((entry) => entry.name === "over the byte limit")?.error).toContain("too large");
  });

  it("is exact at the boundary", () => {
    // Either side of it, so an off-by-anything is caught rather than only an
    // off-by-a-lot.
    expect(golden.validation.find((entry) => entry.name === "exactly at the byte limit")?.error).toBeNull();
    expect(golden.validation.find((entry) => entry.name === "one byte over the limit")?.error).toContain(
      `2049 > ${MAX_PRESENCE_DICT_BYTES}`,
    );
  });

  it("measures the size the way the reference measures it", () => {
    // The bound is on CPython's spaced encoding; a compact one is shorter and
    // would let a value through that the reference refuses.
    const record = golden.validation.find((entry) => entry.name === "over the byte limit");
    expect(record?.error).toContain("4009");
  });

  it("uses the recorded bounds", () => {
    expect(MAX_PRESENCE_DICT_BYTES).toBe(golden.max_dict_bytes);
    expect(MAX_PRESENCE_DICT_KEYS).toBe(golden.max_dict_keys);
    expect([...VALIDATED_PRESENCE_FIELDS].sort()).toStrictEqual(golden.validated_fields);
  });

  it("leaves nothing behind when it refuses", () => {
    // A rejected value alongside a valid one must not leave the user
    // half-updated.
    const store = storeWith();
    expect(refused(() => store.update("u1", { scroll_line: 42, pin: { a: "x".repeat(4000) } }))).toBe(
      golden.partial_update.error,
    );
    const user = store.get("u1");
    expect(user?.scrollLine).toBe(golden.partial_update.scroll_line_after);
    expect(user?.pin ?? null).toBe(golden.partial_update.pin_after);
  });

  it("accepts the nested shapes a real selection has", () => {
    // A selection is a pair of coordinates, so lists and objects inside it
    // are ordinary — they just count towards the size like anything else.
    for (const name of ["a nested list", "a nested object", "a nested list of objects"]) {
      expect(golden.validation.find((entry) => entry.name === name)?.error).toBeNull();
    }
  });

  it("does not check a field that is not untrusted", () => {
    // `queued_keys` is a string this server produced, not a browser payload.
    const store = storeWith();
    expect(refused(() => store.update("u1", { queued_keys: "x".repeat(5000) }))).toBeNull();
  });
});

describe("the store", () => {
  it("refuses a field the record does not have", () => {
    // A browser that invented one would otherwise have it stored and
    // re-broadcast.
    const store = storeWith();
    expect(refused(() => store.update("u1", { nonsense: 1 }))).toBe(golden.store.unknown_field);
  });

  it("says nothing about a user it does not have", () => {
    const store = storeWith();
    expect(store.update("nobody", { typing: true })).toBe(golden.store.update_a_user_that_is_not_there ?? undefined);
    expect(store.get("nobody")).toBeUndefined();
    expect(store.remove("nobody")).toBeUndefined();
  });

  it("hands back the user it removed", () => {
    const store = storeWith();
    store.add("u2", "Bob", "#00ff00", "viewer");
    expect(store.remove("u2")?.userId).toBe(golden.store.remove_returns_the_user);
    expect(store.count).toBe(golden.store.count_after_remove);
  });

  it("keeps one owner at a time", () => {
    // Two people both believing they hold control is the failure this
    // prevents.
    const store = storeWith();
    store.add("u2", "Bob", "#00ff00", "viewer");
    store.setOwner("u1");
    expect(store.getOwner()?.userId).toBe(golden.store.first_owner);
    store.setOwner("u2");
    expect(store.getOwner()?.userId).toBe(golden.store.second_owner);
    expect(
      store
        .getAll()
        .filter((user) => user.isOwner)
        .map((user) => user.userId),
    ).toStrictEqual(golden.store.owners_after_set);
  });

  it("can leave nobody in control", () => {
    const store = storeWith();
    store.setOwner("u1");
    store.clearOwner();
    expect(store.getOwner()).toBe(golden.store.owner_after_clear ?? undefined);
  });

  it("reports the colours in use", () => {
    // A joiner needs one nobody else has, or two cursors look like one.
    const store = storeWith();
    store.add("u2", "Bob", "#00ff00", "viewer");
    expect([...store.takenColors()].sort()).toStrictEqual(golden.store.colors);
    expect(store.count).toBe(golden.store.count);
  });

  it("reads the real clock when given none", () => {
    // Every other case injects one; a store that never advanced its own clock
    // would never find anybody idle.
    const store = new PresenceStore();
    const before = Date.now() / 1000;
    const user = store.add("u1", "Alice", "#f00", "viewer");
    expect(user.lastActivityAt).toBeGreaterThanOrEqual(before - 1);
    expect(store.isIdle(user, 3600)).toBe(false);
  });

  it("stamps activity on every update", () => {
    let clock = NOW;
    const store = new PresenceStore({ now: () => clock });
    store.add("u1", "Alice", "#ff0000", "operator");
    clock += 30;
    store.update("u1", { typing: true });
    expect(store.get("u1")?.lastActivityAt).toBe(NOW + 30);
  });
});

describe("idleness", () => {
  /** A store with a fresh user, a stale one, and one exactly at the line. */
  function aged() {
    const store = new PresenceStore({ now: () => NOW });
    store.add("fresh", "A", "#f00", "viewer");
    const stale = store.add("stale", "B", "#0f0", "viewer");
    stale.lastActivityAt = NOW - 100;
    const exact = store.add("exact", "C", "#00f", "viewer");
    exact.lastActivityAt = NOW - 60;
    return store;
  }

  it("counts somebody quiet for longer than the threshold", () => {
    const store = aged();
    expect(store.isIdle(store.get("fresh") as never, 60)).toBe(golden.idle.idle_states.fresh);
    expect(store.isIdle(store.get("stale") as never, 60)).toBe(golden.idle.idle_states.stale);
  });

  it("does not count somebody exactly at it", () => {
    // Strictly longer: at the threshold they have not passed it, and control
    // should not move out from under somebody who just paused.
    const store = aged();
    expect(store.isIdle(store.get("exact") as never, 60)).toBe(golden.idle.idle_states.exactly_at_the_threshold);
  });

  it("prunes exactly those it counted", () => {
    const store = aged();
    expect(store.pruneIdle(60).sort()).toStrictEqual(golden.idle.pruned);
    expect(
      store
        .getAll()
        .map((user) => user.userId)
        .sort(),
    ).toStrictEqual(golden.idle.remaining);
  });

  it("prunes nobody from an empty store", () => {
    expect(new PresenceStore().pruneIdle(60)).toStrictEqual([]);
  });
});

describe("the messages", () => {
  it("use the recorded type names", () => {
    expect({
      presence_update: MSG_PRESENCE_UPDATE,
      presence_sync: MSG_PRESENCE_SYNC,
      presence_leave: MSG_PRESENCE_LEAVE,
      control_transfer: MSG_CONTROL_TRANSFER,
      queued_input: MSG_QUEUED_INPUT,
      control_request: MSG_CONTROL_REQUEST,
      auto_transfer_warning: MSG_AUTO_TRANSFER_WARNING,
    }).toStrictEqual(golden.messages.types);
  });

  it("carries only the identity when nothing else changed", () => {
    expect(makePresenceUpdate("u1", "Alice", "#ff0000", "operator")).toStrictEqual(golden.messages.bare_update);
  });

  it("carries every optional field that was given", () => {
    expect(
      makePresenceUpdate("u1", "Alice", "#ff0000", "operator", {
        scroll_line: 10,
        scroll_range: [0, 24],
        total_lines: 100,
        selection: { start: 1 },
        pin: { row: 2 },
        typing: true,
        queued_keys: "ls",
        is_owner: true,
      }),
    ).toStrictEqual(golden.messages.full_update);
  });

  it("drops a field it does not know", () => {
    // A browser that invented one would otherwise have it re-broadcast to
    // everybody else verbatim.
    expect(makePresenceUpdate("u1", "Alice", "#ff0000", "operator", { nonsense: 1, cols: 80 })).toStrictEqual(
      golden.messages.update_ignores_unknown_fields,
    );
  });

  it("builds a leave", () => {
    expect(makePresenceLeave("u1")).toStrictEqual(golden.messages.leave);
  });

  it("builds a transfer", () => {
    expect(makeControlTransfer("u1", "u2", "handover", "ls")).toStrictEqual(golden.messages.transfer);
  });

  it("gives a transfer an empty queue when there is none", () => {
    // Always present, so a consumer need not tell absent from empty.
    expect(makeControlTransfer("u1", "u2", "auto_idle")).toStrictEqual(golden.messages.transfer_without_queue);
  });

  it("builds the sync a joiner is sent", () => {
    const store = storeWith();
    expect(store.getSyncPayload({ idle_threshold_s: 60 })).toStrictEqual(golden.messages.sync);
  });

  it("builds an empty sync", () => {
    expect(makePresenceSync([], {})).toStrictEqual(golden.messages.empty_sync);
  });
});

describe("the wire form of a presence", () => {
  it("matches the reference for a fresh user", () => {
    const store = storeWith("u");
    const user = store.get("u") as never;
    expect(
      presenceToWire({ ...(user as object), name: "n", color: "c", role: "r", initials: "" } as never),
    ).toStrictEqual(golden.default_user);
  });

  it("hands out a copy of the range", () => {
    // The wire object is broadcast; a consumer that adjusted its own copy
    // would otherwise be adjusting the stored presence for everybody.
    const store = storeWith();
    const user = store.get("u1") as never;
    const wire = presenceToWire(user);
    (wire.scroll_range as number[])[0] = 99;
    expect((store.get("u1") as { scrollRange: number[] }).scrollRange[0]).toBe(0);
  });

  it("sends a range as a list", () => {
    // A tuple has no JSON form; a client reading index zero needs an array.
    expect(golden.default_user.scroll_range).toStrictEqual([0, 0]);
  });

  it("sends an absent selection as null", () => {
    // Always present, so a consumer need not tell absent from unset.
    expect(golden.default_user.selection).toBeNull();
    expect(golden.default_user.pin).toBeNull();
  });

  it("does not send the activity timestamp", () => {
    // It is this server's bookkeeping, not something a participant needs.
    expect(Object.keys(golden.default_user)).not.toContain("last_activity_at");
  });
});
