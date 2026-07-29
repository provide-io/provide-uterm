//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  consumeTunnelInvite,
  discardTunnelInvitesForSession,
  hashToken,
  INVITE_TTL_S,
  type InviteStore,
  issueTunnelInvites,
  sweepExpiredTunnelInvites,
  tunnelInviteMatchesTokenHash,
  verifyToken,
} from "./index.ts";

interface InvitesGolden {
  invite_ttl_s: number;
  now: number;
  hashed: Array<{ name: string; plain: string; hash: string }>;
  verified: Array<{ name: string; plain: string; stored_hash: string; matches: boolean }>;
  issued: Array<{
    name: string;
    tunnel_expires_at: number;
    invite_length: number;
    invites_differ: boolean;
    keyed_by_hash: boolean;
    entries: Array<Record<string, unknown>>;
  }>;
  consumed: Array<{
    name: string;
    stored: Record<string, unknown> | null;
    offered: string;
    session_id: string;
    now: number;
    consumed: Record<string, unknown> | null;
    second_attempt_consumed: boolean;
    remaining_keys: string[];
  }>;
  swept: { remaining_keys: string[] };
  discarded: { remaining_keys: string[] };
  matches_token_hash: Array<{ name: string; tunnel_token: string; token_hash: string; matches: boolean }>;
}

const golden = loadGolden<InvitesGolden>("tunnelinvites_golden.json");

/** The store as the corpus builds it: the case's entry, plus one nobody touches. */
function storeFor(entry: Record<string, unknown> | null): InviteStore {
  const store: InviteStore = new Map();
  if (entry !== null) {
    store.set(hashToken("invite-1"), { ...entry });
  }
  store.set(hashToken("other-invite"), {
    session_id: "sess-9",
    role: "viewer",
    tunnel_token: "share-token", // pragma: allowlist secret
    expires_at: golden.now + 60,
    issued_ip: "203.0.113.7",
  });
  return store;
}

describe("hashing a token for storage", () => {
  it.each(golden.hashed)("$name", (record) => {
    expect(hashToken(record.plain)).toBe(record.hash);
  });

  it("gives nothing back for nothing", () => {
    // So "no token configured" reads the same as "no match" at a call site.
    expect(hashToken("")).toBe("");
  });

  it("is BLAKE2b-256, not a truncated BLAKE2b-512", () => {
    // Node's own `createHash` offers only the 512-bit variant, and cutting it
    // short would not give this digest — BLAKE2b mixes the output length into
    // its parameter block, so the two differ from the first byte.
    const digest = hashToken("abc");
    expect(digest).toHaveLength(64);
    expect(digest).toBe("bddd813c634239723171ef3fee98579b94964e3bb1cb3e427262c8c068d52319"); // pragma: allowlist secret
  });

  it("says nothing about the token's length", () => {
    // Which is the point of storing a digest at all.
    expect(hashToken("x")).toHaveLength(hashToken("x".repeat(4096)).length);
  });
});

describe("checking a token against a stored hash", () => {
  it.each(golden.verified)("$name", (record) => {
    expect(verifyToken(record.plain, record.stored_hash)).toBe(record.matches);
  });

  it("refuses everybody when the slot is empty", () => {
    // A configured-but-empty slot authenticating any caller would be a hole
    // that opens itself.
    for (const plain of ["", "anything", hashToken("")]) {
      expect(verifyToken(plain, "")).toBe(false);
    }
  });

  it("refuses a hash offered in place of the token", () => {
    // Somebody who read the store still cannot walk in with what they read.
    const stored = hashToken("share-token"); // pragma: allowlist secret
    expect(verifyToken(stored, stored)).toBe(false);
  });

  it("compares the digest, not the token", () => {
    expect(verifyToken("share-token", hashToken("share-token"))).toBe(true); // pragma: allowlist secret
    expect(verifyToken("share-tokeo", hashToken("share-token"))).toBe(false); // pragma: allowlist secret
  });

  it("takes a stored hash of any length without failing", () => {
    // Constant-time comparison is length-sensitive; a store holding something
    // the wrong length must answer no, not raise.
    for (const stored of ["ab", "z".repeat(63), "z".repeat(65), "not-hex"]) {
      expect(verifyToken("share-token", stored)).toBe(false);
    }
  });
});

describe("issuing a pair of invites", () => {
  it.each(golden.issued)("$name", (record) => {
    const store: InviteStore = new Map();
    const [shareInvite, controlInvite] = issueTunnelInvites(store, {
      sessionId: "sess-1",
      shareToken: "share-token", // pragma: allowlist secret
      controlToken: "control-token", // pragma: allowlist secret
      tunnelExpiresAt: record.tunnel_expires_at,
      issuedIp: "203.0.113.7",
      now: golden.now,
    });
    expect(shareInvite).toHaveLength(record.invite_length);
    expect(shareInvite !== controlInvite).toBe(record.invites_differ);
    expect([...store.keys()].sort()).toEqual([hashToken(shareInvite), hashToken(controlInvite)].sort());
    expect([...store.values()].sort((a, b) => String(a.role).localeCompare(String(b.role)))).toEqual(record.entries);
  });

  it("keys the store by the invite's hash, never by the invite", () => {
    // A memory disclosure leaks digests, which redeem nothing.
    const store: InviteStore = new Map();
    const [shareInvite] = issueTunnelInvites(store, {
      sessionId: "sess-1",
      shareToken: "share-token", // pragma: allowlist secret
      controlToken: "control-token", // pragma: allowlist secret
      tunnelExpiresAt: golden.now + 3600,
      issuedIp: null,
      now: golden.now,
    });
    expect(store.has(shareInvite)).toBe(false);
    expect(store.has(hashToken(shareInvite))).toBe(true);
  });

  it("gives an invite the shorter of five minutes and the tunnel's own life", () => {
    // An invite outliving the tunnel would let somebody in after the share
    // was meant to have ended.
    expect(INVITE_TTL_S).toBe(golden.invite_ttl_s);
    for (const [tunnelExpiresAt, expected] of [
      [golden.now + 3600, golden.now + INVITE_TTL_S],
      [golden.now + 30, golden.now + 30],
      [golden.now - 1, golden.now - 1],
    ] as const) {
      const store: InviteStore = new Map();
      issueTunnelInvites(store, {
        sessionId: "sess-1",
        shareToken: "share-token", // pragma: allowlist secret
        controlToken: "control-token", // pragma: allowlist secret
        tunnelExpiresAt,
        issuedIp: null,
        now: golden.now,
      });
      for (const entry of store.values()) {
        expect(entry.expires_at).toBe(expected);
      }
    }
  });

  it("gives the two invites different roles and different tokens", () => {
    const store: InviteStore = new Map();
    issueTunnelInvites(store, {
      sessionId: "sess-1",
      shareToken: "share-token", // pragma: allowlist secret
      controlToken: "control-token", // pragma: allowlist secret
      tunnelExpiresAt: golden.now + 3600,
      issuedIp: null,
      now: golden.now,
    });
    const byRole = new Map([...store.values()].map((entry) => [entry.role, entry.tunnel_token]));
    expect(byRole.get("viewer")).toBe("share-token");
    expect(byRole.get("operator")).toBe("control-token");
  });

  it("returns the viewer's invite first and the operator's second", () => {
    // Handing them back the other way round would put the control token in
    // the link meant for watchers.
    const store: InviteStore = new Map();
    const [shareInvite, controlInvite] = issueTunnelInvites(store, {
      sessionId: "sess-1",
      shareToken: "share-token", // pragma: allowlist secret
      controlToken: "control-token", // pragma: allowlist secret
      tunnelExpiresAt: golden.now + 3600,
      issuedIp: null,
      now: golden.now,
    });
    expect(store.get(hashToken(shareInvite))).toMatchObject({ role: "viewer", tunnel_token: "share-token" });
    expect(store.get(hashToken(controlInvite))).toMatchObject({ role: "operator", tunnel_token: "control-token" });
    expect(consumeTunnelInvite(store, shareInvite, "sess-1", golden.now)?.role).toBe("viewer");
    expect(consumeTunnelInvite(store, controlInvite, "sess-1", golden.now)?.role).toBe("operator");
  });

  it("does not reuse an invite between issuances", () => {
    // The invite is the whole credential, so two shares must not collide.
    const seen = new Set<string>();
    for (let index = 0; index < 50; index += 1) {
      const store: InviteStore = new Map();
      for (const invite of issueTunnelInvites(store, {
        sessionId: "sess-1",
        shareToken: "share-token", // pragma: allowlist secret
        controlToken: "control-token", // pragma: allowlist secret
        tunnelExpiresAt: golden.now + 3600,
        issuedIp: null,
        now: golden.now,
      })) {
        expect(seen.has(invite)).toBe(false);
        seen.add(invite);
      }
    }
    expect(seen.size).toBe(100);
  });

  it("issues something a URL can carry", () => {
    // It travels in a share link.
    const store: InviteStore = new Map();
    for (const invite of issueTunnelInvites(store, {
      sessionId: "sess-1",
      shareToken: "share-token", // pragma: allowlist secret
      controlToken: "control-token", // pragma: allowlist secret
      tunnelExpiresAt: golden.now + 3600,
      issuedIp: null,
      now: golden.now,
    })) {
      expect(invite).toMatch(/^[A-Za-z0-9_-]{43}$/);
    }
  });
});

/**
 * Rows the port deliberately answers differently on; each has a named test
 * below saying why.
 */
const DIVERGENT = new Set(["an invite whose token is null"]);

describe("redeeming an invite", () => {
  it.each(golden.consumed.filter((record) => !DIVERGENT.has(record.name)))("$name", (record) => {
    const store = storeFor(record.stored);
    const consumed = consumeTunnelInvite(store, record.offered, record.session_id, record.now);
    expect(
      consumed === undefined
        ? null
        : {
            session_id: consumed.sessionId,
            role: consumed.role,
            tunnel_token: consumed.tunnelToken,
            expires_at: consumed.expiresAt,
            // The corpus is JSON, where "issued to nobody" is `null`; on this
            // runtime it is an absent field.
            issued_ip: consumed.issuedIp ?? null,
          },
    ).toEqual(record.consumed);
    expect(consumeTunnelInvite(store, record.offered, record.session_id, record.now) !== undefined).toBe(
      record.second_attempt_consumed,
    );
    expect([...store.keys()].sort()).toEqual(record.remaining_keys);
  });

  it('refuses an invite whose token is null, where the reference hands back "None"', () => {
    // `str(None)` is "None", so the reference reads a null token as a
    // five-character token and succeeds with it. It authenticates nobody —
    // it will not verify against the tunnel's hash — but answering "here is
    // your token" for a store written wrong is worse than refusing.
    const record = golden.consumed.find((entry) => entry.name === "an invite whose token is null");
    expect(record?.consumed).toMatchObject({ tunnel_token: "None" });
    const store = storeFor(record?.stored ?? null);
    expect(consumeTunnelInvite(store, "invite-1", "sess-1", golden.now)).toBeUndefined();
  });

  it("reads a field that is absent the same as the reference does", () => {
    // Absent is "" on both sides; only present-and-null differs.
    for (const missing of ["session_id", "role", "tunnel_token", "expires_at"]) {
      const entry: Record<string, unknown> = {
        session_id: "sess-1",
        role: "viewer",
        tunnel_token: "share-token", // pragma: allowlist secret
        expires_at: golden.now + 60,
      };
      delete entry[missing];
      expect(consumeTunnelInvite(storeFor(entry), "invite-1", "sess-1", golden.now)).toBeUndefined();
    }
  });

  it("spends an invite even when redeeming it failed", () => {
    // Removed before anything about it is checked, so a refused attempt
    // cannot be retried with a different session or at a different time.
    const store = storeFor({
      session_id: "sess-1",
      role: "viewer",
      tunnel_token: "share-token", // pragma: allowlist secret
      expires_at: golden.now + 60,
      issued_ip: null,
    });
    expect(consumeTunnelInvite(store, "invite-1", "sess-2", golden.now)).toBeUndefined();
    expect(consumeTunnelInvite(store, "invite-1", "sess-1", golden.now)).toBeUndefined();
  });

  it("leaves the store alone when nothing was offered", () => {
    // An empty invite is refused before the store is touched, so a caller
    // sending nothing cannot evict anybody else's invite.
    const store = storeFor({
      session_id: "sess-1",
      role: "viewer",
      tunnel_token: "share-token", // pragma: allowlist secret
      expires_at: golden.now + 60,
      issued_ip: null,
    });
    // Including an entry keyed by the empty string, which is what an empty
    // invite would hash to if it ever reached the store.
    store.set("", { session_id: "sess-1", role: "viewer", tunnel_token: "t", expires_at: golden.now + 60 });
    const before = [...store.keys()].sort();
    for (const offered of ["", "   ", "\t\n"]) {
      expect(consumeTunnelInvite(store, offered, "sess-1", golden.now)).toBeUndefined();
    }
    expect([...store.keys()].sort()).toEqual(before);
  });

  it("takes an invite at the instant it expires and not a moment later", () => {
    for (const [now, expected] of [
      [golden.now + 60, true],
      [golden.now + 60.001, false],
    ] as const) {
      const store = storeFor({
        session_id: "sess-1",
        role: "viewer",
        tunnel_token: "share-token", // pragma: allowlist secret
        expires_at: golden.now + 60,
        issued_ip: null,
      });
      expect(consumeTunnelInvite(store, "invite-1", "sess-1", now) !== undefined).toBe(expected);
    }
  });

  it("refuses an invite for another session", () => {
    // Or a link handed out for one session would open every other.
    const store = storeFor({
      session_id: "sess-1",
      role: "viewer",
      tunnel_token: "share-token", // pragma: allowlist secret
      expires_at: golden.now + 60,
      issued_ip: null,
    });
    expect(consumeTunnelInvite(store, "invite-1", "sess-2", golden.now)).toBeUndefined();
  });

  it("refuses a role nobody defined", () => {
    // Including one that only differs in case: an invite is not a place to
    // acquire a role the system does not have.
    for (const role of ["admin", "Viewer", "OPERATOR", "", "owner"]) {
      const store = storeFor({
        session_id: "sess-1",
        role,
        tunnel_token: "share-token", // pragma: allowlist secret
        expires_at: golden.now + 60,
        issued_ip: null,
      });
      expect(consumeTunnelInvite(store, "invite-1", "sess-1", golden.now)).toBeUndefined();
    }
  });

  it("refuses an invite carrying no token to hand over", () => {
    for (const tunnelToken of ["", "   "]) {
      const store = storeFor({
        session_id: "sess-1",
        role: "viewer",
        tunnel_token: tunnelToken,
        expires_at: golden.now + 60,
        issued_ip: null,
      });
      expect(consumeTunnelInvite(store, "invite-1", "sess-1", golden.now)).toBeUndefined();
    }
  });

  it("refuses an expiry that is not a number", () => {
    // A missing or unreadable expiry is not an invite that never expires.
    for (const expiresAt of [null, undefined, "soon", true, Number.NaN, {}]) {
      const store = storeFor({
        session_id: "sess-1",
        role: "viewer",
        tunnel_token: "share-token", // pragma: allowlist secret
        expires_at: expiresAt,
        issued_ip: null,
      });
      expect(consumeTunnelInvite(store, "invite-1", "sess-1", golden.now)).toBeUndefined();
    }
  });

  it("trims what it was offered and what it hands back", () => {
    const store = storeFor({
      session_id: "sess-1",
      role: "viewer",
      tunnel_token: "  share-token  ", // pragma: allowlist secret
      expires_at: golden.now + 60,
      issued_ip: null,
    });
    expect(consumeTunnelInvite(store, "  invite-1  ", "sess-1", golden.now)?.tunnelToken).toBe("share-token");
  });

  it("reports the address the invite was issued to, or nobody", () => {
    for (const [issuedIp, expected] of [
      ["203.0.113.7", "203.0.113.7"],
      [null, undefined],
      [undefined, undefined],
    ] as const) {
      const store = storeFor({
        session_id: "sess-1",
        role: "viewer",
        tunnel_token: "share-token", // pragma: allowlist secret
        expires_at: golden.now + 60,
        issued_ip: issuedIp,
      });
      expect(consumeTunnelInvite(store, "invite-1", "sess-1", golden.now)?.issuedIp).toBe(expected);
    }
  });
});

describe("clearing invites out", () => {
  it("sweeps only what is past its expiry", () => {
    const store: InviteStore = new Map(
      (
        [
          ["live", golden.now + 60],
          ["at-the-instant", golden.now],
          ["expired", golden.now - 1],
          ["no-expiry", null],
          ["expiry-not-a-number", "soon"],
        ] as const
      ).map(([key, expiresAt]) => [
        key,
        {
          session_id: "sess-1",
          role: "viewer",
          tunnel_token: "share-token", // pragma: allowlist secret
          expires_at: expiresAt,
          issued_ip: null,
        },
      ]),
    );
    sweepExpiredTunnelInvites(store, golden.now);
    expect([...store.keys()].sort()).toEqual(golden.swept.remaining_keys);
  });

  it("leaves an unreadable expiry in place rather than guessing", () => {
    // Redeeming refuses it anyway, so a sweep dropping it would only hide a
    // store somebody wrote wrong.
    const store: InviteStore = new Map([["odd", { session_id: "s", role: "viewer", tunnel_token: "t" }]]);
    sweepExpiredTunnelInvites(store, golden.now);
    expect([...store.keys()]).toEqual(["odd"]);
  });

  it("discards every invite for one session and no others", () => {
    const store: InviteStore = new Map(
      (
        [
          ["one", "sess-1"],
          ["two", "sess-1"],
          ["other", "sess-2"],
          ["unnamed", ""],
        ] as const
      ).map(([key, sessionId]) => [
        key,
        {
          session_id: sessionId,
          role: "viewer",
          tunnel_token: "share-token", // pragma: allowlist secret
          expires_at: golden.now + 60,
          issued_ip: null,
        },
      ]),
    );
    discardTunnelInvitesForSession(store, "sess-1");
    expect([...store.keys()].sort()).toEqual(golden.discarded.remaining_keys);
  });

  it("discards nothing for a session with no invites", () => {
    const store = storeFor(null);
    const before = [...store.keys()];
    discardTunnelInvitesForSession(store, "sess-nobody");
    expect([...store.keys()]).toEqual(before);
  });

  it("treats an entry naming no session as belonging to no session", () => {
    // Rather than to whoever asks with an empty id.
    // Whether the field is empty or absent: both read as "" here, as they do
    // in the reference.
    const store: InviteStore = new Map([
      ["empty", { session_id: "", role: "viewer", tunnel_token: "t" }],
      ["absent", { role: "viewer", tunnel_token: "t" }],
      ["named", { session_id: "sess-1", role: "viewer", tunnel_token: "t" }],
    ]);
    discardTunnelInvitesForSession(store, "");
    expect([...store.keys()]).toEqual(["named"]);
  });
});

describe("checking a redeemed invite against the live tunnel", () => {
  it.each(golden.matches_token_hash)("$name", (record) => {
    expect(
      tunnelInviteMatchesTokenHash(
        { sessionId: "sess-1", role: "viewer", tunnelToken: record.tunnel_token, expiresAt: golden.now },
        record.token_hash,
      ),
    ).toBe(record.matches);
  });

  it("refuses an invite carrying a token the tunnel has rotated away from", () => {
    // The invite outlived the token it was minted with; the hash it is checked
    // against is the tunnel's current one.
    const invite = {
      sessionId: "sess-1",
      role: "viewer" as const,
      tunnelToken: "old-token", // pragma: allowlist secret
      expiresAt: golden.now,
    };
    expect(tunnelInviteMatchesTokenHash(invite, hashToken("old-token"))).toBe(true); // pragma: allowlist secret
    expect(tunnelInviteMatchesTokenHash(invite, hashToken("new-token"))).toBe(false); // pragma: allowlist secret
    expect(tunnelInviteMatchesTokenHash(invite, "")).toBe(false);
  });
});
