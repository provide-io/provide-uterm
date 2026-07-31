//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import { fanOutGroup, fanOutResult, InMemoryFanOutStore } from "./index.ts";

interface FanOutGolden {
  group_defaults: {
    mode: string;
    stop_on_first_error: boolean;
    error_pattern: string | null;
    quiesce_ms: number;
    max_response_ms: number;
    divergence_threshold: number;
    grants: string[];
  };
  result_defaults: { error: string | null; approval_required: boolean; approval_id: string | null };
  store: {
    missing_is_none: boolean;
    alice: string[];
    bob: string[];
    carol: string[];
    dave: string[];
    stranger: string[];
    replaced_grants: string[];
    bob_after_delete: string[];
  };
}

const golden = loadGolden<FanOutGolden>("fanout_golden.json");
const NOW = 1000;

/** A group owned by `createdBy`, optionally shared. */
function group(groupId: string, createdBy: string, grants: string[] = []) {
  return fanOutGroup({
    groupId,
    name: `group ${groupId}`,
    workerIds: ["w1", "w2"],
    createdBy,
    createdAt: NOW,
    grants,
  });
}

describe("fanOutGroup defaults", () => {
  it("matches the reference policy defaults", () => {
    // These are policy, not conveniences: they decide how long the hub waits
    // for a session to go quiet, how far outputs may drift before counting
    // as divergent, and whether one failure stops the rest.
    const defaults = golden.group_defaults;
    const built = fanOutGroup({
      groupId: "g1",
      name: "fleet",
      workerIds: ["w1"],
      createdBy: "alice",
      createdAt: NOW,
    });
    expect(built.mode).toBe(defaults.mode);
    expect(built.stopOnFirstError).toBe(defaults.stop_on_first_error);
    expect(built.errorPattern).toBe(defaults.error_pattern ?? undefined);
    expect(built.quiesceMs).toBe(defaults.quiesce_ms);
    expect(built.maxResponseMs).toBe(defaults.max_response_ms);
    expect(built.divergenceThreshold).toBe(defaults.divergence_threshold);
    expect(built.grants).toStrictEqual(defaults.grants);
  });

  it("keeps overrides", () => {
    const built = fanOutGroup({
      groupId: "g1",
      name: "fleet",
      workerIds: ["w1"],
      createdBy: "alice",
      createdAt: NOW,
      mode: "serial",
      stopOnFirstError: true,
      errorPattern: "ERROR",
      quiesceMs: 50,
      maxResponseMs: 100,
      divergenceThreshold: 0.5,
    });
    expect(built.mode).toBe("serial");
    expect(built.stopOnFirstError).toBe(true);
    expect(built.errorPattern).toBe("ERROR");
    expect(built.quiesceMs).toBe(50);
    expect(built.divergenceThreshold).toBe(0.5);
  });

  it("copies the grants it is handed", () => {
    // Granting access to one group must not silently grant it on another, or
    // on whatever list the caller kept a reference to. Building both groups
    // from one array is exactly how that happens.
    const shared = ["alice"];
    const first = fanOutGroup({
      groupId: "a",
      name: "a",
      workerIds: [],
      createdBy: "x",
      createdAt: NOW,
      grants: shared,
    });
    const second = fanOutGroup({
      groupId: "b",
      name: "b",
      workerIds: [],
      createdBy: "x",
      createdAt: NOW,
      grants: shared,
    });
    first.grants.push("intruder");
    expect(second.grants).toStrictEqual(["alice"]);
    expect(shared).toStrictEqual(["alice"]);
  });
});

describe("fanOutResult defaults", () => {
  it("matches the reference", () => {
    const built = fanOutResult({
      groupId: "g1",
      sendId: "s1",
      command: "uptime",
      sentAt: NOW,
      results: [],
      divergentSessions: [],
      failedSessions: [],
    });
    expect(built.error).toBe(golden.result_defaults.error ?? null);
    expect(built.approvalRequired).toBe(golden.result_defaults.approval_required);
    expect(built.approvalId).toBe(golden.result_defaults.approval_id ?? null);
  });
});

describe("InMemoryFanOutStore", () => {
  /** A store holding the recorded fixture set. */
  async function seeded() {
    const store = new InMemoryFanOutStore();
    await store.save(group("g1", "alice"));
    await store.save(group("g2", "bob", ["alice"]));
    await store.save(group("g3", "bob"));
    await store.save(group("g4", "carol", ["dave", "alice"]));
    return store;
  }

  it("returns nothing for a group it does not hold", async () => {
    expect(golden.store.missing_is_none).toBe(true);
    expect(await new InMemoryFanOutStore().get("nope")).toBeUndefined();
  });

  it("returns what it was given", async () => {
    const store = await seeded();
    expect((await store.get("g1"))?.createdBy).toBe("alice");
  });

  it.each([
    ["alice", golden.store.alice],
    ["bob", golden.store.bob],
    ["carol", golden.store.carol],
    ["dave", golden.store.dave],
    ["eve", golden.store.stranger],
  ] as const)("lists what %s may see", async (principal, expected) => {
    // Creator or grantee, nobody else. Wrong either way is bad: it hides an
    // operator's own groups, or shows them someone else's fleet.
    const store = await seeded();
    const visible = (await store.listForPrincipal(principal)).map((entry) => entry.groupId).sort();
    expect(visible).toStrictEqual(expected);
  });

  it("replaces rather than duplicating on a repeated save", async () => {
    const store = await seeded();
    await store.save(group("g1", "alice", ["zoe"]));
    expect((await store.get("g1"))?.grants).toStrictEqual(golden.store.replaced_grants);
    expect((await store.listForPrincipal("alice")).filter((entry) => entry.groupId === "g1")).toHaveLength(1);
  });

  it("forgets a deleted group", async () => {
    const store = await seeded();
    await store.delete("g3");
    const visible = (await store.listForPrincipal("bob")).map((entry) => entry.groupId).sort();
    expect(visible).toStrictEqual(golden.store.bob_after_delete);
  });

  it("treats a second delete as a no-op", async () => {
    const store = await seeded();
    await store.delete("g3");
    await expect(store.delete("g3")).resolves.toBeUndefined();
  });

  it("hands back a listing a later save does not disturb", async () => {
    const store = await seeded();
    const listing = await store.listForPrincipal("alice");
    await store.save(group("g5", "alice"));
    expect(listing.map((entry) => entry.groupId).sort()).toStrictEqual(golden.store.alice);
  });

  it("deeply isolates saved groups and every read surface", async () => {
    const store = new InMemoryFanOutStore();
    const original = group("isolated", "alice", ["bob"]);
    original.workerIds = ["w1"];
    await store.save(original);

    original.workerIds.push("input-injected");
    original.createdBy = "mallory";
    original.grants.push("mallory");

    const fetched = await store.get("isolated");
    expect(fetched).toMatchObject({ workerIds: ["w1"], createdBy: "alice", grants: ["bob"] });
    fetched?.workerIds.push("get-injected");
    if (fetched) fetched.createdBy = "eve";
    fetched?.grants.splice(0);

    const listed = await store.listForPrincipal("alice");
    expect(listed).toHaveLength(1);
    listed[0]?.workerIds.push("list-injected");
    listed[0]?.grants.push("eve");

    expect(await store.get("isolated")).toMatchObject({
      workerIds: ["w1"],
      createdBy: "alice",
      grants: ["bob"],
    });
  });

  it("preserves concurrent grants atomically", async () => {
    const store = new InMemoryFanOutStore();
    await store.save(group("atomic", "alice"));

    await Promise.all([store.grantAccess("atomic", "bob", "alice"), store.grantAccess("atomic", "carol", "alice")]);

    expect((await store.get("atomic"))?.grants.sort()).toStrictEqual(["bob", "carol"]);
    await expect(store.grantAccess("atomic", "mallory", "not-alice")).resolves.toBe(false);
  });
});
