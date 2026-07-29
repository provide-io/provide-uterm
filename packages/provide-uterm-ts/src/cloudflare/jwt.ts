//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Claim handling for the Worker's JWT authentication.
 *
 * Port of the pure half of the Python module
 * `provide.uterm.cloudflare.auth.jwt` — everything either side of the
 * signature check: splitting the token, choosing a key from a JWKS,
 * validating the standard claims, and deriving roles from what is left.
 *
 * The Worker is always internet-facing and has no dev bypass, so every
 * principal comes from a cryptographically verified token. Nothing here
 * decides that a token is *authentic*; it decides what an authentic token is
 * allowed to say.
 */

import { pyB64UrlDecode } from "../pycompat/base64.ts";
import type { JwtConfig } from "./config.ts";

/** A token that cannot be trusted, whatever the reason. */
export class JwtValidationError extends Error {}

/** Who a verified token says it is. */
export interface Principal {
  subjectId: string;
  roles: readonly string[];
}

/** A token split into the parts a verifier needs. */
export interface JwtParts {
  header: unknown;
  payload: unknown;
  signature: Uint8Array;
  /** The bytes the signature covers, exactly as they arrived. */
  signingInput: Uint8Array;
}

/** The roles this system knows, strongest first. */
const KNOWN_ROLES = ["admin", "operator", "viewer"] as const;

/** The role a principal falls back to when it holds none this system knows. */
const FALLBACK_ROLE = "viewer";

/** The cookie a browser WebSocket carries its token in. */
const ACCESS_COOKIE = "CF_Authorization";

/** The authorization scheme, compared case-insensitively. */
const BEARER_PREFIX = "bearer ";

/**
 * Decode one base64url segment, the way CPython does.
 *
 * The semantics — padding computed before anything outside the alphabet is
 * discarded — live in `pycompat/base64`, because the server's own JWT path
 * needs the same decoder and two copies of a decoder are two decoders.
 */
export function b64urlDecode(text: string): Uint8Array {
  return pyB64UrlDecode(text);
}

/** Split a token into its header, payload, signature and signed bytes. */
export function parseJwtParts(token: string): JwtParts {
  const parts = token.split(".");
  if (parts.length !== 3) {
    throw new JwtValidationError("malformed JWT: expected 3 parts");
  }
  const [header, payload, signature] = parts as [string, string, string];
  return {
    // Not checked for being an object: nothing downstream does either, and a
    // header that is a list simply reads its fields as absent and fails the
    // algorithm check.
    header: JSON.parse(Buffer.from(b64urlDecode(header)).toString("utf8")),
    payload: JSON.parse(Buffer.from(b64urlDecode(payload)).toString("utf8")),
    signature: b64urlDecode(signature),
    // The bytes as they arrived. Re-encoding the header from the parsed
    // object would change them, and every signature would fail.
    signingInput: new TextEncoder().encode(`${header}.${payload}`),
  };
}

/** Read a field off a claims object, whatever shape it arrived in. */
function claim(source: unknown, name: string): unknown {
  return typeof source === "object" && source !== null ? (source as Record<string, unknown>)[name] : undefined;
}

/**
 * Choose a key from a JWKS by the token's key id, or failing that its
 * algorithm.
 *
 * A named key id wins outright: a key whose algorithm merely matches is not a
 * substitute for the one the token asked for, and verifying against it would
 * accept a token signed by a key the issuer had rotated away from.
 */
export function findJwk(jwks: unknown, kid?: string, alg?: string): Record<string, unknown> {
  const keys = claim(jwks, "keys");
  if (!Array.isArray(keys) || keys.length === 0) {
    // Distinct from "no matching key": one is a misconfigured endpoint, the
    // other a rotated key.
    throw new JwtValidationError("JWKS contains no keys");
  }
  for (const key of keys) {
    if (kid !== undefined) {
      if (claim(key, "kid") === kid) {
        return { ...(key as Record<string, unknown>) };
      }
      continue;
    }
    const keyAlg = claim(key, "alg");
    if (alg === undefined || keyAlg === undefined || keyAlg === alg) {
      return { ...(key as Record<string, unknown>) };
    }
  }
  throw new JwtValidationError("no matching key found in JWKS");
}

/** Refuse a token with no expiry, or one past it. */
export function checkExp(payload: unknown, now: number, leeway: number): void {
  const exp = claim(payload, "exp");
  // Absent rather than skipped: a token with no expiry is not a token that
  // never expires.
  if (exp === undefined || exp === null) {
    throw new JwtValidationError("missing exp claim");
  }
  if (now > (exp as number) + leeway) {
    throw new JwtValidationError("token has expired");
  }
}

/** Refuse a token that is not valid yet. */
export function checkNbf(payload: unknown, now: number, leeway: number): void {
  const nbf = claim(payload, "nbf");
  if (nbf !== undefined && nbf !== null && now < (nbf as number) - leeway) {
    throw new JwtValidationError("token not yet valid");
  }
}

/** Refuse a token from another issuer, when one is configured. */
export function checkIssuer(payload: unknown, config: JwtConfig): void {
  if (config.issuer && claim(payload, "iss") !== config.issuer) {
    throw new JwtValidationError("invalid issuer");
  }
}

/** Refuse a token minted for another audience, when one is configured. */
export function checkAudience(payload: unknown, config: JwtConfig): void {
  if (!config.audience) {
    return;
  }
  const aud = claim(payload, "aud");
  // A token minted for several audiences is still minted for this one.
  if (Array.isArray(aud) ? !aud.includes(config.audience) : aud !== config.audience) {
    throw new JwtValidationError("invalid audience");
  }
}

/**
 * Validate the standard claims.
 *
 * The clock is a parameter rather than read here, so this is testable at a
 * fixed instant; the reference reads it directly and its own corpus has to
 * pin it from the outside.
 */
export function validateClaims(payload: unknown, config: JwtConfig, now: number = Date.now() / 1000): void {
  // Never negative: the setting means tolerance, and read literally a
  // negative one would expire a token *early*, which is the opposite.
  const leeway = Math.max(0, Math.trunc(config.clock_skew_seconds));
  checkExp(payload, now, leeway);
  checkNbf(payload, now, leeway);
  checkIssuer(payload, config);
  checkAudience(payload, config);
}

/**
 * Read a roles claim, which may be a list or a comma-separated string.
 *
 * Not split on spaces: that shape is a scope, read separately, and splitting
 * it here would turn one role into two.
 */
export function parseRolesClaim(raw: unknown): readonly string[] {
  if (typeof raw === "string") {
    return raw
      .split(",")
      .map((part) => part.trim())
      .filter((part) => part !== "");
  }
  if (Array.isArray(raw)) {
    return raw.map((role) => String(role));
  }
  return [];
}

/** Apply the deployment's group mapping, then its default. */
function applyRoleMap(roles: readonly string[], config: JwtConfig): readonly string[] {
  const roleMap = config.jwt_role_map;
  const mapped = Object.keys(roleMap).length > 0 ? roles.map((role) => roleMap[role] ?? role) : roles;
  // A Cloudflare Access token carries no roles at all, so without a default
  // every such user would have none.
  return mapped.length > 0 ? mapped : [config.jwt_default_role || FALLBACK_ROLE];
}

/**
 * Derive the roles a token grants.
 *
 * The roles claim wins over the scope: both present means the deployment set
 * a roles claim, and that is the authoritative one.
 */
export function extractRoles(claims: unknown, config: JwtConfig): readonly string[] {
  let roles = parseRolesClaim(claim(claims, config.jwt_roles_claim));
  if (roles.length === 0) {
    const scope = claim(claims, config.jwt_scopes_claim);
    if (typeof scope === "string" && scope !== "") {
      roles = scope.split(" ").filter((part) => part !== "");
    }
  }
  return applyRoleMap(roles, config);
}

/**
 * The single role a principal acts as.
 *
 * The strongest it holds, and viewer for anything this system does not know —
 * a role that cannot be recognised must not be treated as more than the
 * least.
 */
export function resolveRole(principal: Principal): string {
  const held = new Set(principal.roles);
  return KNOWN_ROLES.find((role) => held.has(role)) ?? FALLBACK_ROLE;
}

/**
 * Read one header, or nothing if the request cannot answer.
 *
 * The callable check cannot change an answer — a request with no header bag
 * would throw instead, and both callers already swallow that. It is here so
 * the absent case reads as a fact about the request rather than as an
 * exception nobody meant to raise.
 */
function header(request: unknown, name: string): string {
  const headers = claim(request, "headers");
  const get = claim(headers, "get");
  if (typeof get !== "function") {
    return "";
  }
  return String((get as (key: string) => unknown).call(headers, name) ?? "");
}

/**
 * Find the token on a request.
 *
 * The bearer header first, then the Cloudflare Access cookie — a browser
 * WebSocket cannot send custom headers, so on a WS upgrade the cookie is the
 * only mechanism there is. The header wins because it is what this request
 * presented, where the cookie is whatever the browser happened to hold.
 *
 * Total by design: it runs on the request path, where an exception would turn
 * a request that simply had no token into a 500.
 */
export function extractBearerOrCookie(request: unknown): string | undefined {
  try {
    const authorization = header(request, "Authorization");
    if (authorization.toLowerCase().startsWith(BEARER_PREFIX)) {
      const token = authorization.slice(BEARER_PREFIX.length).trim();
      if (token !== "") {
        return token;
      }
    }
  } catch {
    // Falls through to the cookie, which may still carry one.
  }
  try {
    for (const part of header(request, "Cookie").split(";")) {
      const trimmed = part.trim();
      const separator = trimmed.indexOf("=");
      if (separator === -1) {
        continue;
      }
      // The whole name, so a cookie merely ending in it does not match. The
      // value keeps any equals sign of its own.
      if (trimmed.slice(0, separator).trim() === ACCESS_COOKIE) {
        const value = trimmed.slice(separator + 1).trim();
        if (value !== "") {
          return value;
        }
      }
    }
  } catch {
    // No token, rather than a failed request.
  }
  return undefined;
}
