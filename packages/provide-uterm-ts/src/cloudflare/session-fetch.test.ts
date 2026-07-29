//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { describe, expect, it } from "vitest";
import { loadGolden } from "../testing/golden.ts";
import {
  INVITE_REDEEM_HEADER,
  INVITE_REDEEM_PREFIX,
  INVITE_REDEEM_PROVENANCE,
  inviteRedemptionAllowed,
  inviteSessionId,
  lazyWorkerId,
  UNKNOWN_WORKER_ID,
  WORKER_ID_PREFIXES,
} from "./index.ts";

interface FetchGolden {
  invite_prefix: string;
  invite_header: string;
  invite_provenance: string;
  paths: Array<{ name: string; worker_id: string; url: string; resolved: string }>;
  unreadable_url: string;
  redeem: Array<{
    name: string;
    provenance: string | null;
    session_id: string;
    worker_id: string;
    allowed: boolean;
  }>;
}

const golden = loadGolden<FetchGolden>("sessionfetch_golden.json");

describe("which session a request is for", () => {
  it.each(golden.paths)("$name", (record) => {
    expect(lazyWorkerId(record.worker_id, record.url)).toBe(record.resolved);
  });

  it("keeps an identity the runtime already has", () => {
    // The URL is the caller's; the identity is not.
    expect(lazyWorkerId("sess-9", "https://x.example/ws/browser/sess-1")).toBe("sess-9");
  });

  it("reads a session id from every route that carries one", () => {
    for (const prefix of WORKER_ID_PREFIXES) {
      expect(lazyWorkerId(UNKNOWN_WORKER_ID, `https://x.example${prefix}sess-1`)).toBe("sess-1");
    }
  });

  it("stops at the first segment, whatever follows it", () => {
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, "https://x.example/api/sessions/sess-1/hijack")).toBe("sess-1");
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, "https://x.example/ws/browser/sess-1?token=x")).toBe("sess-1");
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, "https://x.example/ws/browser/sess-1#frag")).toBe("sess-1");
  });

  it("leaves an encoded segment encoded outside the invite route", () => {
    // Not decoding is what stops `%2F` becoming a separator: whatever the
    // path literally said is the id, and it cannot name another session.
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, "https://x.example/ws/browser/a%2Fb")).toBe("a%2Fb");
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, "https://x.example/ws/browser/sess%2D1")).toBe("sess%2D1");
  });

  it("decodes on the invite route and then refuses a slash", () => {
    // That route does decode, so the slash check has to live there — and it
    // does: an encoded separator yields nothing rather than another session.
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, `https://x.example${INVITE_REDEEM_PREFIX}sess%2D1/redeem`)).toBe("sess-1");
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, `https://x.example${INVITE_REDEEM_PREFIX}a%2Fb/redeem`)).toBe(
      UNKNOWN_WORKER_ID,
    );
  });

  it("takes nothing from a path that names nothing", () => {
    for (const path of ["/ws/browser/", "/ws/browser", "/healthz", "/"]) {
      expect(lazyWorkerId(UNKNOWN_WORKER_ID, `https://x.example${path}`)).toBe(UNKNOWN_WORKER_ID);
    }
  });

  it("takes nothing from an invite path of the wrong shape", () => {
    for (const path of [
      `${INVITE_REDEEM_PREFIX}redeem`,
      `${INVITE_REDEEM_PREFIX}a/b/redeem`,
      `${INVITE_REDEEM_PREFIX}sess-1/redeems`,
      `${INVITE_REDEEM_PREFIX}/redeem`,
    ]) {
      expect(lazyWorkerId(UNKNOWN_WORKER_ID, `https://x.example${path}`)).toBe(UNKNOWN_WORKER_ID);
    }
  });

  it("leaves the identity alone when the URL cannot be read", () => {
    // There is nothing better to put there than what it already had.
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, undefined)).toBe(golden.unreadable_url);
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, "not a url")).toBe(UNKNOWN_WORKER_ID);
    expect(lazyWorkerId("sess-9", undefined)).toBe("sess-9");
  });

  it("keeps the invite route and the ordinary ones apart", () => {
    // Why an invite path never falls through to the prefix list: neither
    // family can match the other, so an invite path that fails its own shape
    // check answers nothing rather than answering as something else.
    for (const prefix of WORKER_ID_PREFIXES) {
      expect(prefix.startsWith(INVITE_REDEEM_PREFIX)).toBe(false);
      expect(INVITE_REDEEM_PREFIX.startsWith(prefix)).toBe(false);
    }
  });

  it("routes by the prefixes the reference routes by", () => {
    expect([...WORKER_ID_PREFIXES]).toEqual([
      "/ws/worker/",
      "/ws/browser/",
      "/ws/raw/",
      "/tunnel/",
      "/worker/",
      "/api/sessions/",
    ]);
  });
});

describe("who may redeem a tunnel invite", () => {
  it.each(golden.redeem)("$name", (record) => {
    expect(inviteRedemptionAllowed(record.provenance ?? undefined, record.session_id, record.worker_id)).toBe(
      record.allowed,
    );
  });

  it("refuses an empty or slashed id even when the session's own id matches", () => {
    // Each condition stands on its own: matching a session whose id is empty,
    // or one whose id contains a separator, is still not a well-formed
    // redemption.
    expect(inviteRedemptionAllowed(INVITE_REDEEM_PROVENANCE, "", "")).toBe(false);
    expect(inviteRedemptionAllowed(INVITE_REDEEM_PROVENANCE, "a/b", "a/b")).toBe(false);
  });

  it("needs all three, and refuses when any one is missing", () => {
    // Each of them answers a different question: who sent this, whether the
    // path was crafted, and whether it reached the object it names.
    expect(inviteRedemptionAllowed(INVITE_REDEEM_PROVENANCE, "sess-1", "sess-1")).toBe(true);
    expect(inviteRedemptionAllowed(undefined, "sess-1", "sess-1")).toBe(false);
    expect(inviteRedemptionAllowed("browser", "sess-1", "sess-1")).toBe(false);
    expect(inviteRedemptionAllowed("", "sess-1", "sess-1")).toBe(false);
    expect(inviteRedemptionAllowed(INVITE_REDEEM_PROVENANCE, "", "sess-1")).toBe(false);
    expect(inviteRedemptionAllowed(INVITE_REDEEM_PROVENANCE, "a/b", "sess-1")).toBe(false);
    expect(inviteRedemptionAllowed(INVITE_REDEEM_PROVENANCE, "sess-2", "sess-1")).toBe(false);
  });

  it("matches the provenance exactly", () => {
    // A near-miss is somebody guessing rather than the Worker calling.
    for (const provenance of [
      INVITE_REDEEM_PROVENANCE.toUpperCase(),
      `${INVITE_REDEEM_PROVENANCE} `,
      INVITE_REDEEM_PROVENANCE.slice(0, -1),
      "worker-invite-redemption-v2",
    ]) {
      expect(inviteRedemptionAllowed(provenance, "sess-1", "sess-1")).toBe(false);
    }
  });

  it("names the route and header the reference names", () => {
    expect(INVITE_REDEEM_PREFIX).toBe(golden.invite_prefix);
    expect(INVITE_REDEEM_HEADER).toBe(golden.invite_header);
    expect(INVITE_REDEEM_PROVENANCE).toBe(golden.invite_provenance);
  });

  it("reads a session id out of an invite path", () => {
    expect(inviteSessionId(`${INVITE_REDEEM_PREFIX}sess-1/redeem`)).toBe("sess-1");
    expect(inviteSessionId(`${INVITE_REDEEM_PREFIX}sess%2D1/redeem`)).toBe("sess-1");
  });

  it("reads nothing out of anything else", () => {
    for (const path of [
      "/api/sessions/sess-1/redeem",
      `${INVITE_REDEEM_PREFIX}sess-1`,
      `${INVITE_REDEEM_PREFIX}a/b/redeem`,
      `${INVITE_REDEEM_PREFIX}sess-1/redeems`,
      "/",
    ]) {
      expect(inviteSessionId(path)).toBe("");
    }
  });

  it("leaves a malformed escape as it found it", () => {
    // Which is what `unquote` does, and refusing here would answer a
    // different question than the one being asked.
    expect(inviteSessionId(`${INVITE_REDEEM_PREFIX}a%ZZ/redeem`)).toBe("a%ZZ");
    expect(lazyWorkerId(UNKNOWN_WORKER_ID, `https://x.example${INVITE_REDEEM_PREFIX}a%ZZ/redeem`)).toBe("a%ZZ");
  });
});
