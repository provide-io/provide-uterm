//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Revoking one share subject of a session, not every viewer of it.
 *
 * Port of the reference's `tests/server/test_viewer_subject_revocation.py`.
 * `POST /api/sessions/{id}/viewers/revoke` with `{"subject": "share:{id}:..."}`
 * refuses that subject's tokens issued at or before the cutoff and leaves the
 * session's other share viewers alone; a token minted for the subject after
 * the cutoff is admitted. No body, or an empty one, still revokes them all.
 */

import { describe, expect, it } from "vitest";
import { NullRecordingStore } from "../recording/index.ts";
import { encodeJwt } from "../serverauth/index.ts";
import { createServerApp } from "./app.ts";
import { SessionHub } from "./session-hub.ts";
import { SessionRegistry } from "./session-registry.ts";
import { sessionDefinitionFrom } from "./session-status.ts";
import {
  isShareSubjectOf,
  SHARE_VIEWER_REVOCATION_TTL_S,
  SHARE_VIEWER_REVOCATIONS_MAX,
  SHARE_VIEWER_REVOKE_BODY_MAX_BYTES,
  SHARE_VIEWER_SUBJECT_MAX_BYTES,
  SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX,
  SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX,
  ShareViewerRevocations,
  ShareViewerRevocationsFullError,
  ShareViewerSubjectRevocationsFullError,
} from "./viewer-revocation.ts";

function share(subject: string, iat: unknown, roles: string[] = ["viewer"]) {
  return { subject_id: subject, roles: new Set(roles), scopes: new Set<string>(), claims: { iat } };
}

const CAP = SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX;

describe("the subject cutoff table", () => {
  it("refuses that subject's tokens issued at or before it", () => {
    for (const [iat, revoked] of [
      [499, true],
      [500, true],
      [500.5, true],
      [501, false],
    ] as const) {
      const revocations = new ShareViewerRevocations();
      expect(revocations.revokeSubject("s1", "share:s1:a", 500.5)).toBe(500.5);
      expect(revocations.subjectCutoff("s1", "share:s1:a")).toBe(500.5);
      expect(revocations.revoked(share("share:s1:a", iat), "s1")).toBe(revoked);
    }
  });

  it("leaves the session's other viewers and every other session alone", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revokeSubject("s1", "share:s1:a", 500);
    expect(revocations.revoked(share("share:s1:b", 1), "s1")).toBe(false);
    expect(revocations.cutoff("s1")).toBeUndefined();
    expect(revocations.size).toBe(0);
    expect(revocations.subjectCutoff("s2", "share:s1:a")).toBeUndefined();
    expect(revocations.revoked(share("share:s1:a", 1), "s2")).toBe(false);
  });

  it("refuses a token without a numeric iat and never anyone but a share viewer", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revokeSubject("s1", "share:s1:a", 500);
    expect(revocations.revoked(share("share:s1:a", undefined), "s1")).toBe(true);
    expect(revocations.revoked(share("share:s1:a", 1, ["viewer", "operator"]), "s1")).toBe(false);
  });

  it("lets the later of the session and subject cutoffs decide", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revoke("s1", 300);
    revocations.revokeSubject("s1", "share:s1:a", 500);
    expect(revocations.revoked(share("share:s1:a", 400), "s1")).toBe(true);
    expect(revocations.revoked(share("share:s1:b", 400), "s1")).toBe(false);
    revocations.revoke("s1", 600);
    expect(revocations.revoked(share("share:s1:a", 550), "s1")).toBe(true);
  });

  it("refuses a new subject on a full session but moves a held one forward", () => {
    const revocations = new ShareViewerRevocations();
    for (let n = 0; n < CAP; n += 1) {
      revocations.revokeSubject("s1", `share:s1:${n}`, 100 + n);
    }
    expect(() => revocations.revokeSubject("s1", "share:s1:new", 500)).toThrow(
      new ShareViewerSubjectRevocationsFullError(),
    );
    expect(new ShareViewerSubjectRevocationsFullError().message).toBe(
      "share-viewer subject revocation table is full for this session",
    );
    expect(new ShareViewerSubjectRevocationsFullError().name).toBe("ShareViewerSubjectRevocationsFullError");
    expect(revocations.subjectCutoff("s1", "share:s1:new")).toBeUndefined();
    expect(revocations.subjectCutoff("s1", "share:s1:0")).toBe(100);
    expect(revocations.revokeSubject("s1", "share:s1:0", 600)).toBe(600);
    expect(revocations.revokeSubject("s2", "share:s2:a", 600)).toBe(600);
  });

  it("ages out subject cutoffs older than the TTL, freeing their slots", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revokeSubject("s1", "share:s1:a", 10);
    revocations.revokeSubject("s1", "share:s1:b", 20);
    // Exactly the TTL old is kept; older goes.
    revocations.revokeSubject("s2", "share:s2:a", 20 + SHARE_VIEWER_REVOCATION_TTL_S);
    expect(revocations.subjectCutoff("s1", "share:s1:a")).toBeUndefined();
    expect(revocations.subjectCutoff("s1", "share:s1:b")).toBe(20);

    const full = new ShareViewerRevocations();
    for (let n = 0; n < CAP; n += 1) {
      full.revokeSubject("s1", `share:s1:${n}`, n);
    }
    expect(full.revokeSubject("s1", "share:s1:new", 1 + SHARE_VIEWER_REVOCATION_TTL_S)).toBe(
      1 + SHARE_VIEWER_REVOCATION_TTL_S,
    );
    expect(full.subjectCutoff("s1", "share:s1:0")).toBeUndefined();
  });

  it("forgets a session whose last subject cutoff aged out", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revokeSubject("s1", "share:s1:a", 10);
    revocations.revokeSubject("s2", "share:s2:a", 11 + SHARE_VIEWER_REVOCATION_TTL_S);
    expect(revocations.subjectCutoff("s1", "share:s1:a")).toBeUndefined();
    for (let n = 0; n < CAP; n += 1) {
      revocations.revokeSubject("s1", `share:s1:${n}`, 12 + SHARE_VIEWER_REVOCATION_TTL_S);
    }
    expect(revocations.subjectCutoff("s1", `share:s1:${CAP - 1}`)).toBe(12 + SHARE_VIEWER_REVOCATION_TTL_S);
  });

  it("ages a re-revoked subject out from its newest cutoff", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revokeSubject("s1", "share:s1:a", 10);
    revocations.revokeSubject("s1", "share:s1:b", 20);
    revocations.revokeSubject("s1", "share:s1:a", 30);
    revocations.revokeSubject("s2", "share:s2:a", 25 + SHARE_VIEWER_REVOCATION_TTL_S);
    expect(revocations.subjectCutoff("s1", "share:s1:b")).toBeUndefined();
    expect(revocations.subjectCutoff("s1", "share:s1:a")).toBe(30);
  });

  it("keys a session and subject so no pair collides with another", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revokeSubject("a", "b,c", 10);
    expect(revocations.subjectCutoff("a,b", "c")).toBeUndefined();
  });

  it("knows a share subject of the session from anything else", () => {
    const prefix = "share:s1:";
    const longest = prefix + "x".repeat(SHARE_VIEWER_SUBJECT_MAX_BYTES - prefix.length);
    expect(isShareSubjectOf("share:s1:blackbetty:7", "s1")).toBe(true);
    expect(isShareSubjectOf(longest, "s1")).toBe(true);
    expect(isShareSubjectOf(`${longest}x`, "s1")).toBe(false);
    expect(isShareSubjectOf(prefix + "é".repeat((SHARE_VIEWER_SUBJECT_MAX_BYTES - prefix.length) / 2 + 1), "s1")).toBe(
      false,
    );
    for (const subject of ["share:s1:", "share:s1", "share:s1x:7", "share:s2:7", "alice"]) {
      expect(isShareSubjectOf(subject, "s1")).toBe(false);
    }
    expect(SHARE_VIEWER_SUBJECT_MAX_BYTES).toBe(512);
    expect(CAP).toBe(100);
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
const ALICE = "share:bb:blackbetty:7";
const ISSUED = 1_700_000_000;

function token(sub: string, roles: string[], iat = ISSUED): string {
  return encodeJwt({ sub, roles, iss: AUTH.jwt_issuer, aud: AUTH.jwt_audience, iat, exp: iat + 3600 }, SECRET);
}

function buildApp(now: () => number) {
  const registry = new SessionRegistry(
    [sessionDefinitionFrom({ session_id: "bb", visibility: "public", owner: "owner1" }, "2026-01-01T00:00:00.000Z")],
    false,
  );
  const app = createServerApp({
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
  const post = (body: BodyInit | null, credential = token("ops", ["admin"])) =>
    app.handle(
      // A stream body needs `duplex`, which the DOM typings do not know yet.
      new Request(`${BASE}${PATH}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${credential}` },
        body,
        duplex: "half",
      } as RequestInit),
    );
  const read = (credential: string) =>
    app.handle(new Request(`${BASE}/api/sessions/bb`, { headers: { Authorization: `Bearer ${credential}` } }));
  return { post, read };
}

describe("POST /api/sessions/{id}/viewers/revoke with a subject", () => {
  it("refuses only that subject's tokens and admits one minted after the cutoff", async () => {
    let clock = ISSUED + 100;
    const { post, read } = buildApp(() => clock);
    const response = await post(JSON.stringify({ subject: ALICE }));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      session_id: "bb",
      revoked_at: ISSUED + 100,
      closed: 0,
      close_code: 4410,
      close_reason: "viewer access revoked",
      subject: ALICE,
    });
    expect((await read(token(ALICE, ["viewer"]))).status).toBe(403);
    expect((await read(token("share:bb:blackbetty:8", ["viewer"]))).status).toBe(200);
    clock = ISSUED + 300;
    expect((await read(token(ALICE, ["viewer"], ISSUED + 200))).status).toBe(200);
  });

  it("revokes every share viewer for no body, a blank one, null, or no subject", async () => {
    for (const body of [null, " \t\r\n", "{}", "null", " null\n"]) {
      const { post, read } = buildApp(() => ISSUED + 100);
      const response = await post(body);
      expect(response.status).toBe(200);
      // A whole-session revoke keeps the original answer: no subject key.
      expect(Object.keys(await response.json()).sort()).toEqual([
        "close_code",
        "close_reason",
        "closed",
        "revoked_at",
        "session_id",
      ]);
      expect((await read(token("share:bb:blackbetty:8", ["viewer"]))).status).toBe(403);
    }
  });

  it("answers 422 for a malformed body and revokes nothing", async () => {
    for (const body of [
      "not json",
      '{"subject": ',
      "[]",
      "[".repeat(2000) + "]".repeat(2000),
      '"share:bb:x"',
      '{"subject": null}',
      '{"subject": 7}',
      '{"subject": "share:bb:x", "extra": 1}',
      '{"subjects": "share:bb:x"}',
      new Uint8Array([0xff]),
      new Uint8Array([...new TextEncoder().encode('{"subject": "share:bb:'), 0xff, 0x22, 0x7d]),
      "{} {}",
      "X",
      "\f",
      // A no-break space and a byte-order mark: whitespace JSON does not allow.
      String.fromCodePoint(0xa0),
      `${String.fromCodePoint(0xfeff)}{}`,
    ]) {
      const { post, read } = buildApp(() => ISSUED + 100);
      const response = await post(body);
      expect(response.status).toBe(422);
      expect(await response.json()).toEqual({ detail: "invalid request body" });
      expect((await read(token(ALICE, ["viewer"]))).status).toBe(200);
    }
  });

  it("answers 422 for a subject that is not this session's share subject", async () => {
    for (const subject of ["share:other:7", "share:bbx:7", "share:bb", "share:bb:", "alice"]) {
      const { post } = buildApp(() => ISSUED + 100);
      const response = await post(JSON.stringify({ subject }));
      expect(response.status).toBe(422);
      expect(await response.json()).toEqual({ detail: "subject must be a share subject of this session" });
    }
  });

  it("refuses a subject spelling a lone surrogate, which is not text", async () => {
    for (const [spelling, status] of [
      ["\\ud83d\\ude00", 200],
      ["\\\\ud800", 200],
      ["\\u0041", 200],
      ["\\ud800", 422],
      ["\\udc00", 422],
      ["\\ud800x", 422],
      ["\\ud800\\u0041", 422],
      ["\\ud800\\ud800", 422],
      ["\\ude00\\ud83d", 422],
    ] as const) {
      const { post } = buildApp(() => ISSUED + 100);
      expect((await post(`{"subject": "share:bb:${spelling}"}`)).status).toBe(status);
    }
  });

  it("answers 429 when the session is full of revoked subjects, and still revokes them all", async () => {
    const { post } = buildApp(() => ISSUED + 100);
    for (let n = 0; n < CAP; n += 1) {
      expect((await post(JSON.stringify({ subject: `share:bb:${n}` }))).status).toBe(200);
    }
    const response = await post(JSON.stringify({ subject: ALICE }));
    expect(response.status).toBe(429);
    expect(await response.json()).toEqual({
      detail: "too many viewers of this session revoked one by one; revoke them all instead",
    });
    expect((await post(null)).status).toBe(200);
  });

  it("refuses a caller who may not revoke before judging its body", async () => {
    const { post } = buildApp(() => ISSUED + 100);
    expect((await post("not json", token("alice", ["viewer"]))).status).toBe(403);
  });

  it("refuses a body past the cap unread and reads one at it", async () => {
    const { post } = buildApp(() => ISSUED + 100);
    const over = await post(`${" ".repeat(SHARE_VIEWER_REVOKE_BODY_MAX_BYTES - 1)}{}`);
    expect(over.status).toBe(422);
    expect(await over.json()).toEqual({ detail: "invalid request body" });
    expect((await post(`${" ".repeat(SHARE_VIEWER_REVOKE_BODY_MAX_BYTES - 2)}{}`)).status).toBe(200);
    expect(SHARE_VIEWER_REVOKE_BODY_MAX_BYTES).toBe(4096);

    let pulled = 0;
    const endless = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulled += 1;
        controller.enqueue(new Uint8Array(1024).fill(0x20));
      },
    });
    expect((await post(endless)).status).toBe(422);
    // Four chunks fill the cap and the fifth crosses it; no more are pulled
    // than the stream's own one-chunk read-ahead.
    expect(pulled).toBeLessThanOrEqual(6);
  });

  it("reads a body that arrives in several chunks whole", async () => {
    const { post, read } = buildApp(() => ISSUED + 100);
    const encoded = new TextEncoder().encode(JSON.stringify({ subject: ALICE }));
    const chunked = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoded.slice(0, 5));
        controller.enqueue(encoded.slice(5, 17));
        controller.enqueue(encoded.slice(17));
        controller.close();
      },
    });
    expect((await post(chunked)).status).toBe(200);
    expect((await read(token(ALICE, ["viewer"]))).status).toBe(403);
    expect((await read(token("share:bb:blackbetty:8", ["viewer"]))).status).toBe(200);
  });

  it("answers 429 when the whole subject table is full", async () => {
    const { post } = buildApp(() => ISSUED + 100);
    const original = ShareViewerRevocations.prototype.revokeSubject;
    ShareViewerRevocations.prototype.revokeSubject = () => {
      throw new ShareViewerRevocationsFullError();
    };
    try {
      const response = await post(JSON.stringify({ subject: ALICE }));
      expect(response.status).toBe(429);
      expect(await response.json()).toEqual({ detail: "too many viewers revoked one by one; try again later" });
    } finally {
      ShareViewerRevocations.prototype.revokeSubject = original;
    }
  });

  it("frees a full session's subject slots on a whole-session revoke", async () => {
    const { post } = buildApp(() => ISSUED + 100);
    for (let n = 0; n < CAP; n += 1) {
      await post(JSON.stringify({ subject: `share:bb:${n}` }));
    }
    expect((await post(JSON.stringify({ subject: ALICE }))).status).toBe(429);
    expect((await post(null)).status).toBe(200);
    expect((await post(JSON.stringify({ subject: ALICE }))).status).toBe(200);
  });
});

describe("the subject cutoffs a whole-session revoke subsumes", () => {
  it("drops the session's subject cutoffs at or before it and keeps the rest", () => {
    const revocations = new ShareViewerRevocations();
    revocations.revokeSubject("s1", "share:s1:a", 100);
    revocations.revokeSubject("s1", "share:s1:b", 200);
    revocations.revokeSubject("s1", "share:s1:c", 300);
    revocations.revokeSubject("s2", "share:s2:a", 100);
    revocations.revoke("s1", 200);
    expect(revocations.subjectCutoff("s1", "share:s1:a")).toBeUndefined();
    expect(revocations.subjectCutoff("s1", "share:s1:b")).toBeUndefined();
    expect(revocations.subjectCutoff("s1", "share:s1:c")).toBe(300);
    expect(revocations.subjectCutoff("s2", "share:s2:a")).toBe(100);
    expect(revocations.revoked(share("share:s1:a", 150), "s1")).toBe(true);
    expect(revocations.revoked(share("share:s1:c", 250), "s1")).toBe(true);
  });

  it("drops none when the session table refuses the revoke", () => {
    const revocations = new ShareViewerRevocations();
    for (let n = 0; n < SHARE_VIEWER_REVOCATIONS_MAX; n += 1) {
      revocations.revoke(`other-${n}`, 100);
    }
    revocations.revokeSubject("s1", "share:s1:a", 100);
    expect(() => revocations.revoke("s1", 200)).toThrow(ShareViewerRevocationsFullError);
    expect(revocations.subjectCutoff("s1", "share:s1:a")).toBe(100);
  });

  it("refuses a new subject when every session together holds the global cap", () => {
    const revocations = new ShareViewerRevocations();
    for (let n = 0; n < SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX; n += 1) {
      revocations.revokeSubject(`s${Math.floor(n / CAP)}`, `share:s${Math.floor(n / CAP)}:${n % CAP}`, 100);
    }
    expect(() => revocations.revokeSubject("fresh", "share:fresh:a", 200)).toThrow(ShareViewerRevocationsFullError);
    expect(revocations.subjectCutoff("fresh", "share:fresh:a")).toBeUndefined();
    expect(revocations.revokeSubject("s0", "share:s0:0", 200)).toBe(200);
    const later = 100.5 + SHARE_VIEWER_REVOCATION_TTL_S;
    expect(revocations.revokeSubject("fresh", "share:fresh:a", later)).toBe(later);
    expect(SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX).toBe(10000);
  });
});
