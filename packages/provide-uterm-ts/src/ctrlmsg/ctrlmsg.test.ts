//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  LinkPattern,
  LinkPatternRegistry,
  makeIdentity,
  makeLinkPatterns,
  makePresenceUpdate,
  makeResume,
  makeResumeFailed,
  makeResumeOk,
  makeSessionToken,
} from "./index.ts";

interface CtrlmsgGolden {
  secret: string;
  identity: Array<{
    name: string;
    subject: string;
    claims: Record<string, unknown> | null;
    fingerprint: string | null;
    transport: string | null;
    secret: string | null;
    frame: Record<string, unknown>;
  }>;
  session_token: Array<{ token: string; player_id: number | null; frame: Record<string, unknown> }>;
  resume: Array<{ token: string; player_id: number | null; frame: Record<string, unknown> }>;
  resume_ok: Record<string, unknown>;
  resume_failed: Array<{ reason: string | null; frame: Record<string, unknown> }>;
  presence_update: Array<{ user_id: string; fields: Record<string, unknown>; frame: Record<string, unknown> }>;
  link_patterns: Array<{ name: string; entries: Array<Record<string, unknown>>; frame: Record<string, unknown> }>;
  link_pattern_rejects: Array<{ name: string; entries: Array<Record<string, unknown>>; error_prefix: string | null }>;
  pattern_entries: Array<{ entry: Record<string, unknown> }>;
  registry: {
    steps: Array<{ step: string; payload: Record<string, unknown> }>;
    removed_known: boolean;
    removed_unknown: boolean;
  };
}

const golden = loadGolden<CtrlmsgGolden>("ctrlmsg_golden.json");

describe("makeIdentity", () => {
  it("builds the base frame with the documented defaults", () => {
    expect(makeIdentity("user:alice")).toStrictEqual({
      type: "identity",
      version: 1,
      subject: "user:alice",
      fingerprint: "",
      transport: "ssh",
    });
  });

  it("omits claims when none are supplied but keeps an empty mapping", () => {
    expect(makeIdentity("u")).not.toHaveProperty("claims");
    expect(makeIdentity("u", { claims: {} })).toHaveProperty("claims", {});
  });

  it("rejects an empty subject", () => {
    expect(() => makeIdentity("")).toThrow("make_identity: 'subject' must be a non-empty string");
  });

  it("adds no signature without a secret", () => {
    expect(makeIdentity("u")).not.toHaveProperty("signature");
  });

  it("treats an empty secret as no secret", () => {
    expect(makeIdentity("u", { secret: "" })).not.toHaveProperty("signature");
  });

  it("signs with a hex-encoded HMAC-SHA256", () => {
    const frame = makeIdentity("u", { secret: "s" });
    expect(frame.signature).toMatch(/^[0-9a-f]{64}$/);
  });

  it("accepts a byte secret and a string secret interchangeably", () => {
    const fromString = makeIdentity("u", { secret: "s" });
    const fromBytes = makeIdentity("u", { secret: new TextEncoder().encode("s") });
    expect(fromBytes.signature).toBe(fromString.signature);
  });

  it("signs claims by canonical order, not insertion order", () => {
    const forward = makeIdentity("u", { claims: { a: 1, z: 2 }, secret: "s" });
    const reversed = makeIdentity("u", { claims: { z: 2, a: 1 }, secret: "s" });
    expect(reversed.signature).toBe(forward.signature);
  });

  it("changes the signature when any signed field changes", () => {
    const base = makeIdentity("u", { claims: { a: 1 }, secret: "s" }).signature;
    expect(makeIdentity("u2", { claims: { a: 1 }, secret: "s" }).signature).not.toBe(base);
    expect(makeIdentity("u", { claims: { a: 2 }, secret: "s" }).signature).not.toBe(base);
    expect(makeIdentity("u", { claims: { a: 1 }, fingerprint: "f", secret: "s" }).signature).not.toBe(base);
    expect(makeIdentity("u", { claims: { a: 1 }, transport: "websocket", secret: "s" }).signature).not.toBe(base);
    expect(makeIdentity("u", { claims: { a: 1 }, secret: "s2" }).signature).not.toBe(base); // pragma: allowlist secret
  });

  it("signs an absent claims mapping as an empty object", () => {
    expect(makeIdentity("u", { secret: "s" }).signature).toBe(makeIdentity("u", { claims: {}, secret: "s" }).signature);
  });
});

describe("makeSessionToken and makeResume", () => {
  it("builds the minimal frames", () => {
    expect(makeSessionToken("t")).toStrictEqual({ type: "session_token", token: "t" });
    expect(makeResume("t")).toStrictEqual({ type: "resume", token: "t" });
  });

  it("includes a player id when given, including zero", () => {
    expect(makeSessionToken("t", 7)).toStrictEqual({ type: "session_token", token: "t", player_id: 7 });
    expect(makeSessionToken("t", 0)).toStrictEqual({ type: "session_token", token: "t", player_id: 0 });
  });

  it("rejects an empty token", () => {
    expect(() => makeSessionToken("")).toThrow("make_session_token: 'token' must be a non-empty string");
    expect(() => makeResume("")).toThrow("make_resume: 'token' must be a non-empty string");
  });
});

describe("makeResumeOk and makeResumeFailed", () => {
  it("builds the acknowledgement", () => {
    expect(makeResumeOk()).toStrictEqual({ type: "resume_ok" });
  });

  it("omits an absent reason but keeps an empty one", () => {
    expect(makeResumeFailed()).toStrictEqual({ type: "resume_failed" });
    expect(makeResumeFailed("")).toStrictEqual({ type: "resume_failed", reason: "" });
    expect(makeResumeFailed("expired")).toStrictEqual({ type: "resume_failed", reason: "expired" });
  });
});

describe("makePresenceUpdate", () => {
  it("builds the minimal frame", () => {
    expect(makePresenceUpdate("u1")).toStrictEqual({ type: "presence_update", user_id: "u1" });
  });

  it("merges arbitrary extra fields", () => {
    expect(makePresenceUpdate("u1", { scroll_line: 5 })).toStrictEqual({
      type: "presence_update",
      user_id: "u1",
      scroll_line: 5,
    });
  });
});

describe("makeLinkPatterns", () => {
  it("builds an empty frame", () => {
    expect(makeLinkPatterns([])).toStrictEqual({ type: "link_patterns", patterns: [] });
  });

  it("keeps a minimal entry as written", () => {
    expect(makeLinkPatterns([{ pattern: "p", action: "cmd" }])).toStrictEqual({
      type: "link_patterns",
      patterns: [{ pattern: "p", action: "cmd" }],
    });
  });

  it("accepts a string group, because the field is int or str", () => {
    expect(makeLinkPatterns([{ pattern: "p", action: "cmd", group: "one" }]).patterns).toStrictEqual([
      { pattern: "p", action: "cmd", group: "one" },
    ]);
  });

  it("drops an explicitly-null optional field", () => {
    expect(makeLinkPatterns([{ pattern: "p", action: "cmd", id: null, hover: null }]).patterns).toStrictEqual([
      { pattern: "p", action: "cmd" },
    ]);
  });

  it("accepts a structured payload, because the field is untyped", () => {
    expect(makeLinkPatterns([{ pattern: "p", action: "cmd", payload: { a: [1] } }]).patterns).toStrictEqual([
      { pattern: "p", action: "cmd", payload: { a: [1] } },
    ]);
  });

  it.each([
    ["a missing pattern", { action: "cmd" }],
    ["a missing action", { pattern: "p" }],
    ["an unknown action", { pattern: "p", action: "nope" }],
    ["an unmodelled field", { pattern: "p", action: "cmd", extra: 1 }],
    ["a non-string pattern", { pattern: 1, action: "cmd" }],
    ["a non-scalar group", { pattern: "p", action: "cmd", group: [] }],
    ["a non-string flags", { pattern: "p", action: "cmd", flags: 1 }],
  ])("rejects %s", (_name, entry) => {
    expect(() => makeLinkPatterns([entry as Record<string, unknown>])).toThrow(/^make_link_patterns: entry\[0\]/);
  });

  it("reports the index of the offending entry", () => {
    expect(() => makeLinkPatterns([{ pattern: "a", action: "cmd" }, { action: "cmd" }])).toThrow(
      /^make_link_patterns: entry\[1\]/,
    );
  });
});

describe("LinkPattern", () => {
  it("rejects an invalid action", () => {
    expect(() => new LinkPattern({ pattern: "p", action: "nope" as "cmd" })).toThrow(/invalid action/);
  });

  it("omits default and empty optional fields from the wire entry", () => {
    expect(new LinkPattern({ pattern: "p", action: "cmd" }).toFrameEntry()).toStrictEqual({
      pattern: "p",
      action: "cmd",
    });
  });

  it("omits flags when they are the default", () => {
    expect(new LinkPattern({ pattern: "p", action: "cmd", flags: "g" }).toFrameEntry()).not.toHaveProperty("flags");
    expect(new LinkPattern({ pattern: "p", action: "cmd", flags: "gi" }).toFrameEntry()).toHaveProperty("flags", "gi");
  });

  it("omits a zero group", () => {
    expect(new LinkPattern({ pattern: "p", action: "cmd", group: 0 }).toFrameEntry()).not.toHaveProperty("group");
    expect(new LinkPattern({ pattern: "p", action: "cmd", group: 2 }).toFrameEntry()).toHaveProperty("group", 2);
  });

  it("serialises the class field under its wire name", () => {
    expect(new LinkPattern({ pattern: "p", action: "cmd", className: "c" }).toFrameEntry()).toHaveProperty(
      "class",
      "c",
    );
  });
});

describe("LinkPatternRegistry", () => {
  it("starts empty", () => {
    expect(new LinkPatternRegistry().syncPayload()).toStrictEqual({ type: "link_patterns", patterns: [] });
  });

  it("replaces a pattern with the same id in place", () => {
    const registry = new LinkPatternRegistry();
    registry.register(new LinkPattern({ pattern: "a", action: "cmd", id: "one" }));
    registry.register(new LinkPattern({ pattern: "b", action: "cmd", id: "two" }));
    registry.register(new LinkPattern({ pattern: "a2", action: "cmd", id: "one" }));
    expect(registry.getAll().map((p) => p.pattern)).toStrictEqual(["a2", "b"]);
  });

  it("appends id-less patterns without collision", () => {
    const registry = new LinkPatternRegistry();
    registry.register(new LinkPattern({ pattern: "a", action: "cmd" }));
    registry.register(new LinkPattern({ pattern: "b", action: "cmd" }));
    expect(registry.getAll().map((p) => p.pattern)).toStrictEqual(["a", "b"]);
  });

  it("reports whether an unregister removed anything", () => {
    const registry = new LinkPatternRegistry();
    registry.register(new LinkPattern({ pattern: "a", action: "cmd", id: "one" }));
    expect(registry.unregister("one")).toBe(true);
    expect(registry.unregister("one")).toBe(false);
  });

  it("resets the id-less counter on clear", () => {
    const registry = new LinkPatternRegistry();
    registry.register(new LinkPattern({ pattern: "a", action: "cmd" }));
    registry.clear();
    registry.register(new LinkPattern({ pattern: "b", action: "cmd" }));
    expect(registry.getAll().map((p) => p.pattern)).toStrictEqual(["b"]);
  });

  it("leaves the registry unchanged when building a payload", () => {
    const registry = new LinkPatternRegistry();
    registry.register(new LinkPattern({ pattern: "a", action: "cmd" }));
    registry.syncPayload();
    expect(registry.getAll()).toHaveLength(1);
  });
});

describe("differential parity with CPython", () => {
  it("matches every identity frame, signatures included", () => {
    for (const record of golden.identity) {
      const options: Parameters<typeof makeIdentity>[1] = {};
      if (record.claims !== null) {
        options.claims = record.claims;
      }
      if (record.fingerprint !== null) {
        options.fingerprint = record.fingerprint;
      }
      if (record.transport !== null) {
        options.transport = record.transport;
      }
      if (record.secret !== null) {
        options.secret = record.secret;
      }
      expect({ name: record.name, frame: makeIdentity(record.subject, options) }).toStrictEqual({
        name: record.name,
        frame: record.frame,
      });
    }
    expect(golden.identity.length).toBeGreaterThan(20);
  });

  it("matches every session-token frame", () => {
    for (const record of golden.session_token) {
      expect(makeSessionToken(record.token, record.player_id ?? undefined)).toStrictEqual(record.frame);
    }
  });

  it("matches every resume frame", () => {
    for (const record of golden.resume) {
      expect(makeResume(record.token, record.player_id ?? undefined)).toStrictEqual(record.frame);
    }
    expect(makeResumeOk()).toStrictEqual(golden.resume_ok);
    for (const record of golden.resume_failed) {
      expect(makeResumeFailed(record.reason ?? undefined)).toStrictEqual(record.frame);
    }
  });

  it("matches every presence-update frame", () => {
    for (const record of golden.presence_update) {
      expect(makePresenceUpdate(record.user_id, record.fields)).toStrictEqual(record.frame);
    }
  });

  it("matches every link-patterns frame", () => {
    for (const record of golden.link_patterns) {
      expect({ name: record.name, frame: makeLinkPatterns(record.entries) }).toStrictEqual({
        name: record.name,
        frame: record.frame,
      });
    }
  });

  it("rejects everything CPython rejected", () => {
    for (const record of golden.link_pattern_rejects) {
      expect(record.error_prefix).toBe("make_link_patterns");
      expect(() => makeLinkPatterns(record.entries)).toThrow(/^make_link_patterns: entry\[\d+\]/);
    }
    expect(golden.link_pattern_rejects.length).toBeGreaterThan(6);
  });

  it("matches every wire entry the value object produces", () => {
    const cases = [
      new LinkPattern({ pattern: "p", action: "cmd" }),
      new LinkPattern({ pattern: "p", action: "url", id: "x" }),
      new LinkPattern({ pattern: "p", action: "cmd", flags: "gi" }),
      new LinkPattern({ pattern: "p", action: "cmd", flags: "g" }),
      new LinkPattern({ pattern: "p", action: "cmd", group: 2 }),
      new LinkPattern({ pattern: "p", action: "cmd", group: 0 }),
      new LinkPattern({ pattern: "p", action: "cmd", payload: "$1" }),
      new LinkPattern({ pattern: "p", action: "cmd", payload: "" }),
      new LinkPattern({ pattern: "p", action: "cmd", hover: "h" }),
      new LinkPattern({ pattern: "p", action: "cmd", className: "c" }),
      new LinkPattern({
        pattern: "p",
        action: "cmd",
        id: "x",
        flags: "i",
        group: 1,
        payload: "a",
        hover: "b",
        className: "c",
      }),
    ];
    expect(cases.map((p) => ({ entry: p.toFrameEntry() }))).toStrictEqual(golden.pattern_entries);
  });

  it("matches the recorded registry walk step for step", () => {
    const registry = new LinkPatternRegistry();
    const actual: Array<{ step: string; payload: Record<string, unknown> }> = [];
    const snapshot = (step: string): void => {
      actual.push({ step, payload: registry.syncPayload() });
    };

    snapshot("empty");
    registry.register(new LinkPattern({ pattern: "a", action: "cmd", id: "one" }));
    snapshot("one registered");
    registry.register(new LinkPattern({ pattern: "b", action: "url", id: "two" }));
    snapshot("two registered");
    registry.register(new LinkPattern({ pattern: "a2", action: "key", id: "one" }));
    snapshot("first replaced in place");
    registry.register(new LinkPattern({ pattern: "c", action: "focus" }));
    registry.register(new LinkPattern({ pattern: "d", action: "focus" }));
    snapshot("two anonymous appended");
    const removedKnown = registry.unregister("two");
    snapshot("known id removed");
    const removedUnknown = registry.unregister("nope");
    snapshot("unknown id removal is a no-op");
    registry.clear();
    snapshot("cleared");
    registry.register(new LinkPattern({ pattern: "e", action: "cmd" }));
    snapshot("registered after clear");

    expect(actual).toStrictEqual(golden.registry.steps);
    expect({ removedKnown, removedUnknown }).toStrictEqual({
      removedKnown: golden.registry.removed_known,
      removedUnknown: golden.registry.removed_unknown,
    });
  });
});
