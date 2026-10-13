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
import { createServerApp } from "./app.ts";
import { handleHijackRequest } from "./hijack-routes.ts";
import { SessionHub } from "./session-hub.ts";
import { SessionRegistry } from "./session-registry.ts";
import { sessionDefinitionFrom } from "./session-status.ts";
import {
  isShareViewer,
  SHARE_VIEWER_REVOCATIONS_MAX,
  ShareViewerRevocations,
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

  it("is bounded and forgets the oldest cutoff first", () => {
    const revocations = new ShareViewerRevocations();
    for (let index = 0; index < SHARE_VIEWER_REVOCATIONS_MAX; index += 1) {
      revocations.revoke(`s${index}`, index);
    }
    revocations.revoke("s0", 1e6); // now the newest, so s1 falls out next
    revocations.revoke("overflow", 2e6);
    expect(revocations.cutoff("s1")).toBeUndefined();
    expect(revocations.cutoff("s0")).toBe(1e6);
    expect(revocations.cutoff("s2")).toBe(2);
    expect(revocations.cutoff("overflow")).toBe(2e6);
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

function buildApp(now?: () => number) {
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
