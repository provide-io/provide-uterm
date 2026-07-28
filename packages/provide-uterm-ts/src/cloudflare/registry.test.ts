//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  deleteKvSession,
  getKvSession,
  KV_REFRESH_S,
  listKvSessions,
  type SessionRegistryKV,
  type UpdateKvSessionOptions,
  updateKvSession,
} from "./index.ts";

interface RegistryGolden {
  existing: Record<string, unknown>;
  updates: Array<{
    name: string;
    initial: Record<string, unknown> | null;
    kwargs: Record<string, unknown>;
    stored: Record<string, unknown> | null;
    calls: string[];
  }>;
  degradation: {
    get_failure_still_writes: Record<string, unknown>;
    corrupt_entry_is_replaced: Record<string, unknown>;
    non_object_entry_is_replaced: Record<string, unknown>;
    put_failure_left_nothing: boolean;
    delete_failure_left_it: boolean;
  };
  reads: {
    listed: Array<Record<string, unknown>>;
    one: Record<string, unknown>;
    missing: null;
    after_delete: null;
    get_failure: null;
    list_failure: unknown[];
  };
  no_binding: { get: null; list: unknown[] };
  list_shapes: { keys_as_objects: Array<Record<string, unknown>>; python_rejects_a_dict_listing: string };
  kv_refresh_s: number;
}

const golden = loadGolden<RegistryGolden>("cfregistry_golden.json");

/** A key-value store that records what it was asked to do. */
class FakeKV implements SessionRegistryKV {
  readonly data: Map<string, string>;
  readonly calls: string[] = [];
  readonly fail: "get" | "put" | "delete" | "list" | undefined;
  #listing: ((prefix: string) => unknown) | undefined;

  constructor(initial: Record<string, string> = {}, fail?: "get" | "put" | "delete" | "list") {
    this.data = new Map(Object.entries(initial));
    this.fail = fail;
  }

  /** Answer listings with something other than the usual shape. */
  withListing(listing: (prefix: string) => unknown): this {
    this.#listing = listing;
    return this;
  }

  async get(key: string): Promise<string | null> {
    this.calls.push(`get:${key}`);
    if (this.fail === "get") {
      throw new Error("kv unreachable");
    }
    return this.data.get(key) ?? null;
  }

  async put(key: string, value: string): Promise<void> {
    this.calls.push(`put:${key}`);
    if (this.fail === "put") {
      throw new Error("kv unreachable");
    }
    this.data.set(key, value);
  }

  async delete(key: string): Promise<void> {
    this.calls.push(`delete:${key}`);
    if (this.fail === "delete") {
      throw new Error("kv unreachable");
    }
    this.data.delete(key);
  }

  async list(options: { prefix: string }): Promise<unknown> {
    this.calls.push(`list:${options.prefix}`);
    if (this.fail === "list") {
      throw new Error("kv unreachable");
    }
    if (this.#listing !== undefined) {
      return this.#listing(options.prefix);
    }
    return {
      keys: [...this.data.keys()]
        .filter((key) => key.startsWith(options.prefix))
        .sort()
        .map((key) => ({ name: key })),
    };
  }

  /** What is stored for one session, parsed. */
  stored(key = "session:w1"): Record<string, unknown> | null {
    const raw = this.data.get(key);
    return raw === undefined ? null : (JSON.parse(raw) as Record<string, unknown>);
  }
}

/** A store seeded with an entry, or empty. */
function kvWith(initial: Record<string, unknown> | null): FakeKV {
  return new FakeKV(initial === null ? {} : { "session:w1": JSON.stringify(initial) });
}

describe("writing a session's status", () => {
  it.each(golden.updates)("$name", async (record) => {
    const kv = kvWith(record.initial);
    await updateKvSession({ SESSION_REGISTRY: kv }, "w1", record.kwargs as unknown as UpdateKvSessionOptions);
    expect(kv.stored()).toStrictEqual(record.stored);
    expect(kv.calls).toStrictEqual(record.calls);
  });

  /** The recorded update with this name. */
  function updateCase(name: string) {
    return golden.updates.find((entry) => entry.name === name);
  }

  it("merges over what was already there", () => {
    // The tunnel API keeps credential hashes, a revoked flag, expiry and
    // one-time invites in this same key, and the Durable Object rewrites its
    // status every sixty seconds. A blind put nulled tunnel, share and
    // control auth about a minute after every worker reconnect.
    const stored = updateCase("over an existing entry")?.stored;
    expect(stored?.share_token_hash).toBe("sha256:abc");
    expect(stored?.control_token_hash).toBe("sha256:def");
    expect(stored?.share_invite_token).toBe("one-time-share");
    expect(stored?.revoked).toBe(false);
    expect(stored?.expires_at).toBe(2000.0);
  });

  it("reads the entry before writing it", () => {
    // Which is what makes the write a merge rather than an overwrite.
    expect(updateCase("over an existing entry")?.calls).toStrictEqual(["get:session:w1", "put:session:w1"]);
  });

  it("lets the status fields win over the stored ones", () => {
    // Create, revoke and rotate stay authoritative for the credentials; the
    // status is authoritative for the status.
    expect(golden.existing.connector_type).toBe("telnet");
    expect(updateCase("over an existing entry")?.stored?.connector_type).toBe("unknown");
  });

  it("derives the lifecycle from the connection", () => {
    expect(updateCase("a fresh session")?.stored?.lifecycle_state).toBe("running");
    expect(updateCase("disconnecting without removing")?.stored?.lifecycle_state).toBe("stopped");
  });

  it("keeps the connection flag it already had when not told one", () => {
    // A heartbeat that does not know the connection state must not report the
    // session as gone.
    expect(updateCase("inheriting the connected flag")?.stored?.connected).toBe(true);
    expect(updateCase("inheriting from an entry that has none")?.stored?.connected).toBe(false);
  });

  it("removes the entry when the session disconnects", () => {
    // A stopped session should leave the fleet list rather than linger in it.
    const record = updateCase("disconnecting removes the entry");
    expect(record?.stored).toBeNull();
    expect(record?.calls).toStrictEqual(["delete:session:w1"]);
  });

  it("keeps it when asked to", () => {
    expect(updateCase("disconnecting without removing")?.stored).not.toBeNull();
  });

  it("fills in what the meta did not say", () => {
    const stored = updateCase("a fresh session")?.stored;
    expect(stored?.display_name).toBe("w1");
    expect(stored?.created_at).toBe(0.0);
    expect(stored?.connector_type).toBe("unknown");
    expect(stored?.visibility).toBe("public");
    expect(stored?.tags).toStrictEqual([]);
    expect(stored?.owner).toBeNull();
    expect(stored?.input_mode).toBe("hijack");
    expect(stored?.hijacked).toBe(false);
    expect(stored?.recording_enabled).toBe(true);
    expect(stored?.recording_available).toBe(false);
    expect(stored?.auto_start).toBe(false);
    expect(stored?.last_error).toBeNull();
  });

  it("treats an empty meta field as absent", () => {
    // A blank display name is not a name; falling back to the session id
    // keeps the fleet list readable.
    const stored = updateCase("a meta whose fields are empty")?.stored;
    expect(stored?.display_name).toBe("w1");
    expect(stored?.connector_type).toBe("unknown");
    expect(stored?.visibility).toBe("public");
  });

  it("uses everything the meta did say", () => {
    const stored = updateCase("with everything the meta can carry")?.stored;
    expect(stored?.display_name).toBe("Session One");
    expect(stored?.created_at).toBe(1234.5);
    expect(stored?.connector_type).toBe("ssh");
    expect(stored?.tags).toStrictEqual(["x", "y"]);
    expect(stored?.owner).toBe("bob");
    expect(stored?.visibility).toBe("private");
    expect(stored?.hijacked).toBe(true);
    expect(stored?.input_mode).toBe("observe");
    expect(stored?.recording_enabled).toBe(false);
    expect(stored?.recording_available).toBe(true);
  });
});

describe("when the store misbehaves", () => {
  it("writes anyway when the read failed", async () => {
    // A merge that could not read is still worth writing: losing the status
    // as well as the merge would take the session out of the fleet list.
    const kv = new FakeKV({}, "get");
    await updateKvSession({ SESSION_REGISTRY: kv }, "w1", { connected: true });
    expect(kv.stored()).toStrictEqual(golden.degradation.get_failure_still_writes);
  });

  it("says nothing when the write failed", async () => {
    // KV is a network call. A status write that could not land must not take
    // the session down with it.
    const kv = new FakeKV({}, "put");
    await expect(updateKvSession({ SESSION_REGISTRY: kv }, "w1", { connected: true })).resolves.toBeUndefined();
    expect(golden.degradation.put_failure_left_nothing).toBe(true);
  });

  it("says nothing when the delete failed", async () => {
    const kv = new FakeKV({ "session:w1": "{}" }, "delete");
    await expect(updateKvSession({ SESSION_REGISTRY: kv }, "w1", { connected: false })).resolves.toBeUndefined();
    await expect(deleteKvSession({ SESSION_REGISTRY: kv }, "w1")).resolves.toBeUndefined();
  });

  it("replaces an entry it cannot read", async () => {
    // Corrupt or of the wrong shape: there is nothing to merge, so the fresh
    // status stands on its own rather than the write being abandoned.
    const corrupt = new FakeKV({ "session:w1": "{not json" });
    await updateKvSession({ SESSION_REGISTRY: corrupt }, "w1", { connected: true });
    expect(corrupt.stored()).toStrictEqual(golden.degradation.corrupt_entry_is_replaced);

    const notAnObject = new FakeKV({ "session:w1": '["a list"]' });
    await updateKvSession({ SESSION_REGISTRY: notAnObject }, "w1", { connected: true });
    expect(notAnObject.stored()).toStrictEqual(golden.degradation.non_object_entry_is_replaced);

    expect(Object.hasOwn(golden.degradation.corrupt_entry_is_replaced, "share_token_hash")).toBe(false);
  });

  it("returns nothing when a read fails", async () => {
    const kv = new FakeKV({ "session:w1": "{}" }, "get");
    expect(await getKvSession({ SESSION_REGISTRY: kv }, "w1")).toBeUndefined();
    expect(golden.reads.get_failure).toBeNull();
  });

  it("returns nothing when a listing fails", async () => {
    // One unreachable store should cost the listing, not the request.
    const kv = new FakeKV({ "session:w1": "{}" }, "list");
    expect(await listKvSessions({ SESSION_REGISTRY: kv })).toStrictEqual([]);
    expect(golden.reads.list_failure).toStrictEqual([]);
  });
});

describe("reading sessions back", () => {
  /** A store holding the corpus's own entries. */
  function populated(): FakeKV {
    return new FakeKV({
      "session:w1": JSON.stringify(golden.existing),
      "session:w2": JSON.stringify({ session_id: "w2", connected: false }),
      "other:thing": JSON.stringify({ not: "a session" }),
      "session:broken": "{not json",
      "session:list": '["not an object"]',
    });
  }

  it("returns one entry as it is stored", async () => {
    // Unredacted: a Durable Object reads its own entry to bootstrap a tunnel,
    // and needs the credential material the list must not hand out.
    expect(await getKvSession({ SESSION_REGISTRY: populated() }, "w1")).toStrictEqual(golden.reads.one);
    expect(golden.reads.one.share_token_hash).toBe("sha256:abc");
  });

  it("returns nothing for a session that is not there", async () => {
    expect(await getKvSession({ SESSION_REGISTRY: populated() }, "nobody")).toBeUndefined();
  });

  it("lists every session, and only sessions", async () => {
    // The prefix is what separates session documents from everything else
    // sharing the store.
    const listed = await listKvSessions({ SESSION_REGISTRY: populated() });
    expect(listed).toStrictEqual(golden.reads.listed);
    expect(listed.map((row) => row.session_id)).toStrictEqual(["w1", "w2"]);
  });

  it("strips credential material from the listing", () => {
    // Token material and invite secrets live in the same document, because a
    // Durable Object needs them. Handing them out in a fleet listing would be
    // a long-lived credential in a response during the invite window.
    const [row] = golden.reads.listed;
    for (const secret of [
      "share_invite_token",
      "control_invite_token",
      "share_token",
      "control_token",
      "worker_token",
      "worker_token_hash",
      "share_token_hash",
      "control_token_hash",
      "share_invite_hash",
      "control_invite_hash",
    ]) {
      expect(Object.hasOwn(row as object, secret)).toBe(false);
    }
    expect(Object.hasOwn(golden.existing, "share_token_hash")).toBe(true);
  });

  it("keeps the fields that are not secrets", () => {
    const [row] = golden.reads.listed;
    expect(row?.session_id).toBe("w1");
    expect(row?.owner).toBe("alice");
    expect(row?.revoked).toBe(false);
  });

  it("steps over an entry it cannot read", async () => {
    // One corrupt document must not cost the whole listing.
    const listed = await listKvSessions({ SESSION_REGISTRY: populated() });
    expect(listed).toHaveLength(2);
  });

  it("removes an entry when asked", async () => {
    const kv = populated();
    await deleteKvSession({ SESSION_REGISTRY: kv }, "w1");
    expect(await getKvSession({ SESSION_REGISTRY: kv }, "w1")).toBeUndefined();
  });
});

describe("a listing that arrives in another shape", () => {
  it("reads keys given as objects", async () => {
    const kv = new FakeKV({ "session:w1": JSON.stringify(golden.existing) }).withListing(() => ({
      // A key that is not an object at all, alongside ones that are: the
      // listing is somebody else's data and may hold anything.
      keys: [{ name: "session:w1" }, { name: "" }, { nothing: "here" }, "session:w1", 7, null],
    }));
    expect(await listKvSessions({ SESSION_REGISTRY: kv })).toStrictEqual(golden.list_shapes.keys_as_objects);
  });

  it("reads a listing that is a plain object", async () => {
    // A deliberate divergence, and a hardening. The reference reads
    // `result.keys` whenever the attribute exists — and every dict has one, as
    // a method — so a shim returning a plain mapping iterates a bound method
    // and raises out of the listing entirely. Here both shapes are read.
    expect(golden.list_shapes.python_rejects_a_dict_listing).toContain("not iterable");
    const kv = new FakeKV({ "session:w1": JSON.stringify(golden.existing) }).withListing(() => ({
      keys: [{ name: "session:w1" }],
    }));
    expect(await listKvSessions({ SESSION_REGISTRY: kv })).toHaveLength(1);
  });

  it("steps over a key the listing named but the store has lost", async () => {
    // The two are eventually consistent, so a listing can name an entry that
    // has since been deleted. That is a gap in the listing, not an error.
    const kv = new FakeKV({ "session:w1": JSON.stringify(golden.existing) }).withListing(() => ({
      keys: [{ name: "session:w1" }, { name: "session:gone" }],
    }));
    expect(await listKvSessions({ SESSION_REGISTRY: kv })).toHaveLength(1);
  });

  it("copes with a listing that has no keys at all", async () => {
    const kv = new FakeKV({}).withListing(() => ({}));
    expect(await listKvSessions({ SESSION_REGISTRY: kv })).toStrictEqual([]);
  });

  it("copes with a listing that is not an object", async () => {
    const kv = new FakeKV({}).withListing(() => "nonsense");
    expect(await listKvSessions({ SESSION_REGISTRY: kv })).toStrictEqual([]);
  });
});

describe("a Worker with no registry configured", () => {
  it("does nothing, quietly", async () => {
    // The registry is optional. A deployment without it should run, not fail
    // on every status write.
    await expect(updateKvSession({}, "w1", { connected: true })).resolves.toBeUndefined();
    await expect(deleteKvSession({}, "w1")).resolves.toBeUndefined();
    expect(await getKvSession({}, "w1")).toBeUndefined();
    expect(await listKvSessions({})).toStrictEqual([]);
    expect(golden.no_binding.get).toBeNull();
    expect(golden.no_binding.list).toStrictEqual([]);
  });
});

describe("the heartbeat interval", () => {
  it("is what the alarm reschedules on", () => {
    expect(KV_REFRESH_S).toBe(golden.kv_refresh_s);
  });
});
