//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The stub identity provider behind `auth.mode = "dev_token"`.
 *
 * Port of `provide.uterm.server.dev_idp`.
 *
 * It reads like a development shortcut and deliberately is not one. The mode
 * it replaced (`dev`/`none`) turned authentication off, so anything that could
 * reach the loopback address — a sidecar, a container, somebody else's ssh
 * tunnel — could claim any principal by setting a header. What this does
 * instead is *be* an identity provider: it generates a fresh HS256 secret,
 * mints a real JWT against it, and rewrites the configuration to `jwt` mode so
 * that from the next line onwards there is exactly one authentication path in
 * the process, the same one a production deployment runs.
 *
 * That matters most in the live conformance harness, which runs every port's
 * server in this mode. A port that recognised its own token instead of
 * verifying it would pass every scenario and ship an authentication bypass.
 */

import { encodeJwt } from "./jwt.ts";

/** The `auth.*` fields this rewrites, in the config document's own names. */
export interface DevIdpAuthConfig {
  mode: string;
  jwt_public_key_pem: string | null;
  jwt_algorithms: readonly string[];
  jwt_issuer: string;
  jwt_audience: string;
  jwt_roles_claim: string;
  jwt_tenant_claim: string;
  worker_bearer_token: string | null;
}

/** How the stub IdP mints. */
export interface DevIdpOptions {
  subject?: string | undefined;
  roles?: readonly string[] | undefined;
  tenant?: string | undefined;
  /** How long the token lives, in seconds. */
  ttlSeconds?: number | undefined;
  /** The current time in seconds. The runtime's clock unless a test says otherwise. */
  now?: (() => number) | undefined;
  /** Where the secrets come from. The runtime's own unless a test says otherwise. */
  randomToken?: ((bytes: number) => string) | undefined;
}

/**
 * How long an auto-issued token lives.
 *
 * A day: long enough to survive a working day of interactive use, short
 * enough that a leaked token file goes stale rather than staying a key.
 */
export const DEV_TOKEN_TTL_S = 24 * 3600;

/** The issuer a deployment that named none is given. */
export const DEV_ISSUER = "provide-uterm-dev";

/** The audience a deployment that named none is given. */
export const DEV_AUDIENCE = "provide-uterm-server";

/** What the stub IdP produced. */
export interface DevIdpToken {
  /** The bearer token to present. */
  token: string;
  /** The secret it was signed with, which is now the configured key. */
  secret: string;
  /** When it stops working, in seconds. */
  expiresAt: number;
}

/** URL-safe random text of the width `secrets.token_urlsafe` produces. */
function randomUrlSafe(bytes: number): string {
  const raw = new Uint8Array(bytes);
  globalThis.crypto.getRandomValues(raw);
  return Buffer.from(raw).toString("base64url");
}

/**
 * Configure `auth` for `dev_token` mode and return the token it issued.
 *
 * Mutates `auth`, as the reference does: the mode collapses to `jwt`, so
 * every downstream check — the entropy floor, the issuer and audience
 * enforcement, the signature — runs exactly as it would in production.
 *
 * @returns The freshly minted token and the secret it is verifiable with.
 */
export function setupDevIdp(auth: DevIdpAuthConfig, options: DevIdpOptions = {}): DevIdpToken {
  const random = options.randomToken ?? randomUrlSafe;
  // 48 bytes: comfortably past the 32-character floor the config validator
  // holds an HMAC secret to, which RFC 8725 §3.5 puts at 256 bits.
  const secret = random(48);

  auth.mode = "jwt";
  auth.jwt_public_key_pem = secret;
  auth.jwt_algorithms = ["HS256"];
  auth.jwt_issuer = auth.jwt_issuer || DEV_ISSUER;
  auth.jwt_audience = auth.jwt_audience || DEV_AUDIENCE;
  // Independent of the signing key, and held to the same floor: a worker
  // connects with a raw bearer token rather than a JWT.
  if (!auth.worker_bearer_token) {
    auth.worker_bearer_token = random(32);
  }

  const issued = Math.trunc((options.now ?? (() => Date.now() / 1000))());
  const expiresAt = issued + (options.ttlSeconds ?? DEV_TOKEN_TTL_S);
  const claims: Record<string, unknown> = {
    sub: options.subject ?? "dev-user",
    iss: auth.jwt_issuer,
    aud: auth.jwt_audience,
    iat: issued,
    exp: expiresAt,
    [auth.jwt_roles_claim]: [...(options.roles ?? ["admin"])],
  };
  // A tenant is optional, and a blank one is no tenant rather than a tenant
  // whose name is nothing.
  if (options.tenant !== undefined && options.tenant.trim() !== "") {
    claims[auth.jwt_tenant_claim] = options.tenant;
  }
  return { token: encodeJwt(claims, secret), secret, expiresAt };
}
