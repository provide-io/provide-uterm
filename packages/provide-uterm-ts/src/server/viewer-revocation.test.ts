//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Revoking a session's share viewers.
 *
 * Port of the reference's `tests/server/test_viewer_revocation.py`. A share
 * viewer is a `share:{session_id}:...` subject holding only the viewer role;
 * `POST /api/sessions/{id}/viewers/revoke` records a cutoff and every such
 * token issued at or before it is refused from then on. This server binds no
 * browser WebSocket, so there are never live sockets to close and `closed` is
 * always zero — but the refusal on every read path is the same.
 */

import { describe, expect, it } from "vitest";
import { NullRecordingStore } from "../recording/index.ts";
import { encodeJwt } from "../serverauth/index.ts";
import { type Logger, noopLogger } from "../telemetry/index.ts";
import { createServerApp } from "./app.ts";
import { handleHijackRequest } from "./hijack-routes.ts";
import { SessionHub } from "./session-hub.ts";
import { SessionRegistry } from "./session-registry.ts";
import { sessionDefinitionFrom } from "./session-status.ts";
import {
  isShareViewer,
  SHARE_VIEWER_REVOCATION_TTL_S,
  SHARE_VIEWER_REVOCATIONS_MAX,
  SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX,
  ShareViewerRevocations,
  ShareViewerRevocationsFullError,
  VIEWER_REVOKED_CLOSE_CODE,
  VIEWER_REVOKED_REASON,
} from "./viewer-revocation.ts";

function viewer(sessionId: string, iat: unknown, roles: string[] = ["viewer"]) {
  return {
    subject_id: `share:${sessionId}:blackbetty:7`,
    roles: new Set(roles),
    scopes: new Set<string>(),
    claims: iat === undefined ? {} : { iat },
  };
}

describe("who counts as a share viewer", () => {
  it("is a share subject for this session holding nothing beyond viewer", () => {
    expect(isShareViewer(viewer("s1", 1), "s1")).toBe(true);
    expect(isShareViewer(viewer("s1", 1, []), "s1")).toBe(true);
    expect(isShareViewer(viewer("s2", 1), "s1")).toBe(false);
    expect(isShareViewer(viewer("s1x", 1), "s1")).toBe(false);
    expect(isShareViewer(viewer("s1", 1, ["viewer", "operator"]), "s1")).toBe(false);
    expect(isShareViewer(viewer("s1", 1, ["admin"]), "s1")).toBe(false);
    expect(isShareViewer({ ...viewer("s1", 1), admin_session_scope: "s1" }, "s1")).toBe(false);
    expect(isShareViewer({ ...viewer("s1", 1), admin_session_scope: null }, "s1")).toBe(true);
    expect(isShareViewer({ subject_id: "alice", roles: new Set(["viewer"]), scopes: new Set() }, "s1")).toBe(false);
  });
});

describe("the revocation cutoff", () => {
  it("refuses tokens issued at or before it, and only share viewers of that session", () => {
    const revocations = new ShareViewerRevocations();
    expect(revocations.revoked(viewer("s1", 1), "s1")).toBe(false);
    expect(revocations.cutoff("s1")).toBeUndefined();

    expect(revocations.revoke("s1", 500.5)).toBe(500.5);
    expect(revocations.cutoff("s1")).toBe(500.5);

    expect(revocations.revoked(viewer("s1", 499), "s1")).toBe(true);
    expect(revocations.revoked(viewer("s1", 500.5), "s1")).toBe(true);
    expect(revocations.revoked(viewer("s1", 501), "s1")).toBe(false);
    // No numeric iat: the token cannot show it postdates the cutoff.
    expect(revocations.revoked(viewer("s1", undefined), "s1")).toBe(true);
    expect(revocations.revoked(viewer("s1", "600"), "s1")).toBe(true);
    expect(revocations.revoked(viewer("s1", Number.NaN), "s1")).toBe(true);
    const { claims: _claims, ...claimless } = viewer("s1", 600);
    expect(revocations.revoked(claimless, "s1")).toBe(true);

    expect(revocations.revoked(viewer("s2", 1), "s2")).toBe(false);
    expect(revocations.revoked(viewer("s1", 1, ["admin"]), "s1")).toBe(false);

    revocations.revoke("s1", 900);
    expect(revocations.revoked(viewer("s1", 600), "s1")).toBe(true);
  });

  it("refuses a new session when the table is full instead of evicting a live cutoff", () => {
    const revocations = new ShareViewerRevocations();
    const base = 1e9;
    for (let index = 0; index < SHARE_VIEWER_REVOCATIONS_MAX; index += 1) {
      revocations.revoke(`s${index}`, base + index);
    }
    // A session already holding a cutoff can always move it forward.
    expect(revocations.revoke("s0", base + 1e4)).toBe(base + 1e4);
    // A new one is refused rather than pushing a live cutoff out.
    expect(() => revocations.revoke("overflow", base + 2e4)).toThrow(ShareViewerRevocationsFullError);
    expect(revocations.cutoff("overflow")).toBeUndefined();
    expect(revocations.cutoff("s1")).toBe(base + 1);
    // Once the oldest cutoffs have aged out there is room again.
    const later = base + 1 + SHARE_VIEWER_REVOCATION_TTL_S + 0.5;
    expect(revocations.revoke("overflow", later)).toBe(later);
    expect(revocations.cutoff("s1")).toBeUndefined();
    expect(revocations.cutoff("s0")).toBe(base + 1e4);
  });

  const CAP = SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX;

  it("holds at most a principal's cap of cutoffs, keeping the newest", () => {
    const revocations = new ShareViewerRevocations();
    for (let index = 0; index < CAP + 2; index += 1) {
      revocations.revoke(`s${index}`, 100 + index, "alice");
    }
    // Alice's two oldest made room for her two newest.
    expect(revocations.cutoff("s0")).toBeUndefined();
    expect(revocations.cutoff("s1")).toBeUndefined();
    expect(revocations.cutoff("s2")).toBe(102);
    expect(revocations.cutoff(`s${CAP + 1}`)).toBe(100 + CAP + 1);
    expect(revocations.size).toBe(CAP);
  });

  it("leaves other principals' cutoffs alone", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revoke("bobs", 1, "bob");
    revocations.revoke("admins", 1);
    for (let index = 0; index <= CAP; index += 1) {
      revocations.revoke(`s${index}`, 10 + index, "alice");
    }
    expect(revocations.cutoff("bobs")).toBe(1);
    expect(revocations.cutoff("admins")).toBe(1);
  });

  it("evicts nothing when a principal moves its own cutoff forward", () => {
    const revocations = new ShareViewerRevocations();
    for (let index = 0; index < CAP; index += 1) {
      revocations.revoke(`s${index}`, 100 + index, "alice");
    }
    revocations.revoke("s0", 500, "alice");
    expect(revocations.size).toBe(CAP);
    expect(revocations.cutoff("s0")).toBe(500);
    // s0 is now alice's newest, so her next new session pushes s1 out.
    revocations.revoke("extra", 600, "alice");
    expect(revocations.cutoff("s0")).toBe(500);
    expect(revocations.cutoff("s1")).toBeUndefined();
  });

  it("leaves a re-revoked cutoff with its first revoker", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revoke("shared", 1, "alice");
    // Bob re-revokes alice's session: the cutoff moves forward, but it stays in
    // alice's quota, so bob's own revokes cannot flush it out.
    revocations.revoke("shared", 5, "bob");
    expect(revocations.cutoff("shared")).toBe(5);
    for (let index = 0; index <= CAP; index += 1) {
      revocations.revoke(`b${index}`, 10 + index, "bob");
    }
    expect(revocations.cutoff("shared")).toBe(5);
    expect(revocations.cutoff("b0")).toBeUndefined();
    // It is still alice's: a full cap of her own pushes it out.
    for (let index = 0; index < CAP; index += 1) {
      revocations.revoke(`a${index}`, 500 + index, "alice");
    }
    expect(revocations.cutoff("shared")).toBeUndefined();
    expect(revocations.cutoff("a0")).toBe(500);
  });

  it("neither caps an uncapped cutoff nor frees a capped one on a re-revoke", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revoke("admins", 1);
    revocations.revoke("alices", 1, "alice");
    revocations.revoke("admins", 2, "bob");
    revocations.revoke("alices", 2);
    for (let index = 0; index <= CAP; index += 1) {
      revocations.revoke(`b${index}`, 10 + index, "bob");
    }
    expect(revocations.cutoff("admins")).toBe(2);
    for (let index = 0; index < CAP; index += 1) {
      revocations.revoke(`a${index}`, 500 + index, "alice");
    }
    expect(revocations.cutoff("alices")).toBeUndefined();
  });

  it("reports each cutoff the cap evicts and nothing else", () => {
    const revocations = new ShareViewerRevocations();
    const evicted: string[] = [];
    const onEvict = (sessionId: string) => evicted.push(sessionId);
    for (let index = 0; index < CAP; index += 1) {
      revocations.revoke(`s${index}`, 100 + index, "alice", onEvict);
    }
    // A re-revoke at the cap evicts nothing.
    revocations.revoke("s5", 300, "alice", onEvict);
    expect(evicted).toEqual([]);
    revocations.revoke("new", 400, "alice", onEvict);
    expect(evicted).toEqual(["s0"]);
    expect(revocations.cutoff("s0")).toBeUndefined();
  });

  it("never evicts an uncapped cutoff for the per-principal cap", () => {
    const revocations = new ShareViewerRevocations();
    for (let index = 0; index < CAP + 5; index += 1) {
      revocations.revoke(`s${index}`, 100 + index);
    }
    expect(revocations.cutoff("s0")).toBe(100);
    expect(revocations.size).toBe(CAP + 5);
  });

  it("lets a principal at its cap make its own room in a full table", () => {
    const revocations = new ShareViewerRevocations();
    const base = 1e9;
    for (let index = 0; index < CAP; index += 1) {
      revocations.revoke(`a${index}`, base + index, "alice");
    }
    for (let index = 0; index < SHARE_VIEWER_REVOCATIONS_MAX - CAP; index += 1) {
      revocations.revoke(`s${index}`, base + CAP + index);
    }
    expect(revocations.revoke("a-new", base + 2e4, "alice")).toBe(base + 2e4);
    expect(revocations.cutoff("a0")).toBeUndefined();
    expect(revocations.cutoff("s0")).toBe(base + CAP);
  });

  it("frees a principal's slots as its cutoffs age out", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revoke("old", 1, "alice");
    for (let index = 0; index < CAP; index += 1) {
      revocations.revoke(`s${index}`, 10 + SHARE_VIEWER_REVOCATION_TTL_S + index, "alice");
    }
    expect(revocations.cutoff("s0")).toBe(10 + SHARE_VIEWER_REVOCATION_TTL_S);
    expect(revocations.size).toBe(CAP);
  });

  it("ages out cutoffs older than a day", () => {
    expect(SHARE_VIEWER_REVOCATION_TTL_S).toBe(86400);
    const revocations = new ShareViewerRevocations();
    revocations.revoke("old", 1000);
    revocations.revoke("edge", 2000);
    revocations.revoke("new", 2000 + SHARE_VIEWER_REVOCATION_TTL_S);
    expect(revocations.cutoff("old")).toBeUndefined();
    expect(revocations.cutoff("edge")).toBe(2000);
    expect(revocations.cutoff("new")).toBe(2000 + SHARE_VIEWER_REVOCATION_TTL_S);
  });

  it("closes with an application code and a generic reason", () => {
    expect(VIEWER_REVOKED_CLOSE_CODE).toBe(4410);
    expect(VIEWER_REVOKED_REASON).toBe("viewer access revoked");
  });
});

const SECRET = "uterm-test-secret-32-byte-minimum-key";
const AUTH = {
  jwt_public_key_pem: SECRET,
  jwt_algorithms: ["HS256"],
  jwt_issuer: "provide-uterm",
  jwt_audience: "provide-uterm-server",
  jwt_roles_claim: "roles",
  jwt_scopes_claim: "scope",
  jwt_tenant_claim: "tenant_id",
  clock_skew_seconds: 0,
};
const BASE = "http://127.0.0.1:0";
const PATH = "/api/sessions/bb/viewers/revoke";

function token(sub: string, roles: string[], iat: number): string {
  return encodeJwt({ sub, roles, iss: AUTH.jwt_issuer, aud: AUTH.jwt_audience, iat, exp: iat + 3600 }, SECRET);
}

function bearer(value: string): Record<string, string> {
  return { Authorization: `Bearer ${value}` };
}

function buildApp(now?: () => number, logger?: Logger) {
  const registry = new SessionRegistry(
    [sessionDefinitionFrom({ session_id: "bb", visibility: "public", owner: "owner1" }, "2026-01-01T00:00:00.000Z")],
    false,
  );
  return createServerApp({
    registry,
    auth: AUTH,
    hub: new SessionHub(),
    connectors: { setMode: async () => undefined },
    recordings: {
      recordingStore: new NullRecordingStore(),
      recordingDirectory: ".",
      flushRecording: async () => undefined,
    },
    version: "0.0.0",
    controlPlaneBackend: "memory",
    startupTime: 0,
    now,
    logger,
  });
}

describe("POST /api/sessions/{id}/viewers/revoke", () => {
  it("refuses the revoked token on every read path and admits a later one", async () => {
    let clock = 1_700_000_100;
    const app = buildApp(() => clock);
    const watcher = token("share:bb:blackbetty:7", ["viewer"], 1_700_000_000);
    const plain = token("alice", ["viewer"], 1_700_000_000);
    const get = (path: string, credential: string) =>
      app.handle(new Request(`${BASE}${path}`, { headers: bearer(credential) }));
    const post = (credential: string) =>
      app.handle(new Request(`${BASE}${PATH}`, { method: "POST", headers: bearer(credential) }));

    expect((await get("/api/sessions/bb", watcher)).status).toBe(200);
    expect((await post(watcher)).status).toBe(403);
    expect((await post(plain)).status).toBe(403);

    const response = await post(token("owner1", ["operator"], 1_700_000_000));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      session_id: "bb",
      revoked_at: 1_700_000_100,
      closed: 0,
      close_code: 4410,
      close_reason: "viewer access revoked",
    });

    for (const path of ["/api/sessions/bb", "/api/sessions/bb/snapshot", "/api/sessions/bb/recording"]) {
      expect((await get(path, watcher)).status).toBe(403);
    }
    const listed = (await (await get("/api/sessions", watcher)).json()) as { sessions?: unknown[] } | unknown[];
    expect(JSON.stringify(listed)).not.toContain('"bb"');
    expect((await get("/worker/bb/hijack/0abc-1/snapshot", watcher)).status).toBe(403);

    // An ordinary viewer of the public session is untouched.
    expect((await get("/api/sessions/bb", plain)).status).toBe(200);

    clock = 1_700_000_300;
    const fresh = token("share:bb:blackbetty:7", ["viewer"], 1_700_000_200);
    expect((await get("/api/sessions/bb", fresh)).status).toBe(200);
    expect((await get("/api/sessions/bb/recording", fresh)).status).not.toBe(403);
    expect((await get("/worker/bb/hijack/0abc-1/snapshot", fresh)).status).not.toBe(403);
  });

  it("answers the gate in order: method, credential, grammar, existence", async () => {
    const app = buildApp(() => 1_700_000_100);
    const admin = token("ops", ["admin"], 1_700_000_000);
    const get = await app.handle(new Request(`${BASE}${PATH}`, { headers: bearer(admin) }));
    expect(get.status).toBe(405);
    expect(get.headers.get("Allow")).toBe("POST");
    expect((await app.handle(new Request(`${BASE}${PATH}`, { method: "POST" }))).status).toBe(401);
    const bad = await app.handle(
      new Request(`${BASE}/api/sessions/not.an.id/viewers/revoke`, { method: "POST", headers: bearer(admin) }),
    );
    expect(bad.status).toBe(422);
    const missing = await app.handle(
      new Request(`${BASE}/api/sessions/nope/viewers/revoke`, { method: "POST", headers: bearer(admin) }),
    );
    expect(missing.status).toBe(404);
  });

  it("answers 429 when the cutoff table is full", async () => {
    const app = buildApp(() => 1_700_000_100);
    const admin = token("ops", ["admin"], 1_700_000_000);
    const original = ShareViewerRevocations.prototype.revoke;
    ShareViewerRevocations.prototype.revoke = () => {
      throw new ShareViewerRevocationsFullError();
    };
    try {
      const response = await app.handle(new Request(`${BASE}${PATH}`, { method: "POST", headers: bearer(admin) }));
      expect(response.status).toBe(429);
      expect(await response.json()).toEqual({ detail: "too many sessions with revoked viewers; try again later" });
    } finally {
      ShareViewerRevocations.prototype.revoke = original;
    }
  });

  it("counts an owner's revoke against the owner but a global admin's against nobody", async () => {
    const app = buildApp(() => 1_700_000_100);
    const original = ShareViewerRevocations.prototype.revoke;
    const calls: unknown[][] = [];
    ShareViewerRevocations.prototype.revoke = function (this: ShareViewerRevocations, ...args) {
      calls.push(args.slice(2, 3));
      return original.apply(this, args);
    };
    try {
      for (const credential of [token("ops", ["admin"], 1_700_000_000), token("owner1", ["operator"], 1_700_000_000)]) {
        const response = await app.handle(
          new Request(`${BASE}${PATH}`, { method: "POST", headers: bearer(credential) }),
        );
        expect(response.status).toBe(200);
      }
    } finally {
      ShareViewerRevocations.prototype.revoke = original;
    }
    expect(calls).toEqual([[undefined], ["owner1"]]);
  });

  it("audits a cutoff the cap evicts, naming the session and the principal", async () => {
    const records: Array<[Record<string, unknown>, string | undefined]> = [];
    const logger: Logger = { ...noopLogger, info: (obj, msg) => records.push([obj, msg]) };
    const app = buildApp(() => 1_700_000_100, logger);
    const original = ShareViewerRevocations.prototype.revoke;
    ShareViewerRevocations.prototype.revoke = function (this: ShareViewerRevocations, ...args) {
      for (let index = 0; index < SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX; index += 1) {
        original.call(this, `mine-${index}`, 1_700_000_000, "owner1");
      }
      ShareViewerRevocations.prototype.revoke = original;
      return original.apply(this, args);
    };
    try {
      const owner = token("owner1", ["operator"], 1_700_000_000);
      const response = await app.handle(new Request(`${BASE}${PATH}`, { method: "POST", headers: bearer(owner) }));
      expect(response.status).toBe(200);
    } finally {
      ShareViewerRevocations.prototype.revoke = original;
    }
    expect(records).toEqual([
      [
        {
          action: "session.viewers.cutoff_evicted",
          principal: "owner1",
          session_id: "mine-0",
          revoked_session_id: "bb",
        },
        "audit_event",
      ],
    ]);
  });

  it("stamps the cutoff with the wall clock when no clock was given", async () => {
    const app = buildApp();
    const issued = Math.floor(Date.now() / 1000) - 10;
    const before = Date.now() / 1000;
    const response = await app.handle(
      new Request(`${BASE}${PATH}`, { method: "POST", headers: bearer(token("ops", ["admin"], issued)) }),
    );
    const body = (await response.json()) as { revoked_at: number };
    expect(body.revoked_at).toBeGreaterThanOrEqual(before);
    expect(body.revoked_at).toBeLessThanOrEqual(Date.now() / 1000);
  });
});

describe("the lease routes without a revocation table", () => {
  it("fall back to the read decision alone", async () => {
    const registry = new SessionRegistry(
      [sessionDefinitionFrom({ session_id: "bb", visibility: "public" }, "2026-01-01T00:00:00.000Z")],
      false,
    );
    const response = await handleHijackRequest(new Request(`${BASE}/worker/bb/hijack/0abc-1/snapshot`), {
      hub: new SessionHub(),
      registry,
      principal: viewer("bb", 1),
      authenticated: true,
    });
    expect(response?.status).not.toBe(403);
  });
});
