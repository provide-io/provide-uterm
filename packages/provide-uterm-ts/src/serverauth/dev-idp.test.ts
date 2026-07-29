//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The `dev_token` stub IdP, held to `serverjwt_golden`'s `dev_idp` section.
 *
 * What is compared is what the reference *left behind*: the claim set and its
 * order, the lifetime, and the configuration the mode collapsed to. The
 * secret and the worker token are fresh random material on both sides, so
 * they are compared by width — which is the part that is load-bearing, since
 * the config validator holds an HMAC secret to a 32-character floor.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { DEV_AUDIENCE, DEV_ISSUER, type DevIdpAuthConfig, setupDevIdp } from "./dev-idp.ts";
import { decodeJwt } from "./jwt.ts";
import { type AuthSettings, principalFromJwtToken } from "./principal.ts";

interface DevIdpCase {
  name: string;
  why: string;
  config: Partial<DevIdpAuthConfig>;
  options: { subject?: string; roles?: string[]; tenant?: string; ttl_s?: number };
  result: {
    claims: Record<string, unknown>;
    claim_order: string[];
    ttl_s: number;
    auth: {
      mode: string;
      jwt_algorithms: string[];
      jwt_issuer: string;
      jwt_audience: string;
      secret_length: number;
      worker_bearer_token: string | number;
    };
    verifies_against_the_configured_key: boolean;
  };
}

const CORPUS = JSON.parse(
  readFileSync(join(import.meta.dirname, "..", "..", "testdata", "serverjwt_golden.json"), "utf8"),
) as { dev_idp: DevIdpCase[] };

/** The `auth` section of the default configuration, as far as this reads it. */
function defaultAuth(overrides: Partial<DevIdpAuthConfig> = {}): DevIdpAuthConfig {
  return {
    mode: "dev_token",
    jwt_public_key_pem: null,
    jwt_algorithms: ["HS256"],
    jwt_issuer: "provide-uterm",
    jwt_audience: "provide-uterm-server",
    jwt_roles_claim: "roles",
    jwt_tenant_claim: "tenant_id",
    worker_bearer_token: null,
    ...overrides,
  };
}

/** Read the claims of a token without checking anything about it. */
function claimsOf(token: string): Record<string, unknown> {
  const payload = token.split(".")[1] as string;
  return JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as Record<string, unknown>;
}

describe("what the stub identity provider mints", () => {
  it("covers every case the reference recorded", () => {
    expect(CORPUS.dev_idp.length).toBe(7);
  });

  for (const one of CORPUS.dev_idp) {
    it(`${one.name}: ${one.why}`, () => {
      const auth = defaultAuth(one.config);
      const issued = 1_700_000_000;
      const minted = setupDevIdp(auth, {
        subject: one.options.subject,
        roles: one.options.roles,
        tenant: one.options.tenant,
        ttlSeconds: one.options.ttl_s,
        now: () => issued,
      });

      const claims = claimsOf(minted.token);
      expect(claims.iat).toBe(issued);
      expect(claims.exp).toBe(issued + one.result.ttl_s);
      const { iat: _iat, exp: _exp, ...rest } = claims;
      expect(rest).toEqual(one.result.claims);
      // Order too: the reference writes the claims in one order and a JWT is
      // compared as bytes by anything that caches it.
      expect(Object.keys(claims)).toEqual([
        ...one.result.claim_order.slice(0, 3),
        "iat",
        "exp",
        ...one.result.claim_order.slice(3),
      ]);

      expect(auth.mode).toBe(one.result.auth.mode);
      expect(auth.jwt_algorithms).toEqual(one.result.auth.jwt_algorithms);
      expect(auth.jwt_issuer).toBe(one.result.auth.jwt_issuer);
      expect(auth.jwt_audience).toBe(one.result.auth.jwt_audience);
      expect(auth.jwt_public_key_pem).toBe(minted.secret);
      expect(minted.secret.length).toBe(one.result.auth.secret_length);
      expect(minted.expiresAt).toBe(issued + one.result.ttl_s);
      if (typeof one.result.auth.worker_bearer_token === "string") {
        expect(auth.worker_bearer_token).toBe(one.result.auth.worker_bearer_token);
      } else {
        expect(auth.worker_bearer_token?.length).toBe(one.result.auth.worker_bearer_token);
      }

      // The whole point of the mode: the token it minted goes through the
      // ordinary validator rather than round some side of it.
      const settings: AuthSettings = {
        jwt_public_key_pem: auth.jwt_public_key_pem,
        jwt_algorithms: auth.jwt_algorithms,
        jwt_issuer: auth.jwt_issuer,
        jwt_audience: auth.jwt_audience,
        jwt_roles_claim: "roles",
        jwt_scopes_claim: "scope",
        jwt_tenant_claim: "tenant_id",
        clock_skew_seconds: 15,
      };
      const principal = principalFromJwtToken(minted.token, settings, () => issued + 1);
      expect(principal.subject_id).toBe(one.result.claims.sub);
      expect(one.result.verifies_against_the_configured_key).toBe(true);
    });
  }
});

describe("the secrets it generates", () => {
  it("does not mint the same secret twice", () => {
    // A stub IdP that reused a secret across restarts would let a token from
    // a previous process go on working against this one.
    const first = setupDevIdp(defaultAuth());
    const second = setupDevIdp(defaultAuth());
    expect(first.secret).not.toBe(second.secret);
    expect(first.token).not.toBe(second.token);
  });

  it("mints a secret past the floor the config validator holds an HMAC key to", () => {
    // RFC 8725 §3.5 puts an HS256 key at 256 bits; the config refuses shorter
    // than 32 characters, and this must not be the thing that trips it.
    expect(setupDevIdp(defaultAuth()).secret.length).toBeGreaterThanOrEqual(32);
  });

  it("names a default issuer and audience for a config that named neither", () => {
    const auth = defaultAuth({ jwt_issuer: "", jwt_audience: "" });
    setupDevIdp(auth);
    expect(auth.jwt_issuer).toBe(DEV_ISSUER);
    expect(auth.jwt_audience).toBe(DEV_AUDIENCE);
  });

  it("expires a day from now when nobody says otherwise", () => {
    const before = Math.trunc(Date.now() / 1000);
    const minted = setupDevIdp(defaultAuth());
    expect(minted.expiresAt).toBeGreaterThanOrEqual(before + 24 * 3600);
  });

  it("takes its randomness from wherever the caller points it", () => {
    const auth = defaultAuth();
    const minted = setupDevIdp(auth, { randomToken: (bytes) => `r${bytes}`, now: () => 0 });
    expect(minted.secret).toBe("r48");
    expect(auth.worker_bearer_token).toBe("r32");
    const options = { key: "r48", algorithms: ["HS256"], audience: auth.jwt_audience ?? undefined, now: () => 1 };
    expect(decodeJwt(minted.token, options).sub).toBe("dev-user");
  });
});
