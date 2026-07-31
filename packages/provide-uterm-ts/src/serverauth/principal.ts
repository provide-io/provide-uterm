//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Who a request is, in `jwt` mode.
 *
 * Port of `provide.uterm.server.auth.LocalIdentityProvider`'s JWT path: the
 * bearer header is read, the token is verified, and what the claims are
 * allowed to say becomes a principal.
 *
 * The single most important thing here is the fallback. A token that does not
 * verify does not raise out to the caller — it produces the *anonymous*
 * principal, exactly as a request with no token at all does, and the route
 * gate turns both into the same 401. Telling the two apart would answer, for
 * free, whether a guessed token was well-formed.
 */

import { canonicalTenantId } from "./api-keys.ts";
import { decodeJwt, type JwtClaims, JwtError } from "./jwt.ts";
import { filterKnownRoles, KNOWN_ROLES } from "./roles.ts";

const KNOWN_ROLE_SET = new Set(KNOWN_ROLES);

/**
 * Fill empty `jwt_jwks_url` / `jwt_issuer` from a Cloudflare Access team
 * domain, in place. Explicit operator values always win. `jwt_issuer`
 * defaults to the non-empty `"provide-uterm"`, so operators must clear it
 * for the team-domain issuer fill to apply — same as Go, C# and Python.
 *
 * Takes the raw parsed `auth` section rather than {@link AuthSettings}
 * because `jwt_jwks_url` is not part of the JWT verification path this port
 * implements yet; the fill still runs so a server.toml using
 * `cf_access_team_domain` produces the same resulting config every port
 * would.
 */
export function applyCfAccessTeamDomain(auth: Record<string, unknown>): void {
  let team = String(auth.cf_access_team_domain ?? "").trim();
  if (team === "") {
    return;
  }
  team = team.replace(/^https?:\/\//, "");
  const slash = team.indexOf("/");
  if (slash >= 0) {
    team = team.slice(0, slash);
  }
  team = team.replace(/\.cloudflareaccess\.com$/, "").trim();
  if (team === "") {
    return;
  }
  if (String(auth.jwt_jwks_url ?? "").trim() === "") {
    auth.jwt_jwks_url = `https://${team}.cloudflareaccess.com/cdn-cgi/access/certs`;
  }
  if (String(auth.jwt_issuer ?? "").trim() === "") {
    auth.jwt_issuer = `https://${team}.cloudflareaccess.com`;
  }
}

/** The subset of `auth.*` configuration the JWT path reads. */
export interface AuthSettings {
  /** The HMAC secret or public key. */
  jwt_public_key_pem: string | null;
  jwt_algorithms: readonly string[];
  jwt_issuer: string;
  jwt_audience: string;
  jwt_roles_claim: string;
  jwt_scopes_claim: string;
  jwt_tenant_claim: string;
  clock_skew_seconds: number;
  /**
   * Applied when a verified JWT carries no known roles (typical Cloudflare
   * Access JWTs have no roles claim). Go, C# and Python already have this
   * field; ported here for parity.
   */
  jwt_default_role?: string | null | undefined;
}

/** Who the server decided a request is. */
export interface ServerPrincipal {
  subject_id: string;
  tenant_id: string | null;
  roles: ReadonlySet<string>;
  scopes: ReadonlySet<string>;
  claims: JwtClaims;
}

/**
 * The claims a token must carry, whatever else it says.
 *
 * `sub` because a request with no subject cannot be audited, and `exp`
 * because a token with no expiry is one that never stops working.
 */
export const REQUIRED_JWT_CLAIMS: readonly string[] = ["sub", "exp"];

/** The subject a request that authenticated nobody is filed under. */
export const ANONYMOUS_SUBJECT = "anonymous";

/** What the reference splits a textual roles claim on. */
const ROLE_SEPARATORS = /[,\s]+/;

/**
 * The bearer token an `Authorization` header carries, if it carries one.
 *
 * Split on the *first* space only, then trimmed — so `Bearer  abc` is `abc`
 * and `Bearer abc def` is `abc def`, both as the reference reads them. A
 * header that is not two parts, or whose scheme is not bearer, is no token
 * rather than an error: the caller is anonymous either way.
 */
export function extractBearerToken(headers: { get(name: string): string | null }): string | undefined {
  const authorization = (headers.get("authorization") ?? "").trim();
  if (authorization === "") {
    return undefined;
  }
  const boundary = authorization.indexOf(" ");
  if (boundary === -1) {
    return undefined;
  }
  if (authorization.slice(0, boundary).toLowerCase() !== "bearer") {
    return undefined;
  }
  // The reference falls back to "no token" for an empty remainder. That
  // cannot happen once the whole header has been trimmed: a scheme followed
  // by nothing but whitespace has already lost it, and was refused above as a
  // header of one part.
  return authorization.slice(boundary + 1).trim();
}

/** The principal a request that proved nothing gets. */
export function anonymousPrincipal(): ServerPrincipal {
  return { subject_id: ANONYMOUS_SUBJECT, tenant_id: null, roles: new Set(["viewer"]), scopes: new Set(), claims: {} };
}

/** The roles a claim set is allowed to hold, after the allow-list. */
export function rolesFromClaims(claims: JwtClaims, settings: AuthSettings): ReadonlySet<string> {
  const raw = claims[settings.jwt_roles_claim];
  let pieces: string[];
  if (typeof raw === "string") {
    pieces = raw
      .split(ROLE_SEPARATORS)
      .map((part) => part.trim().toLowerCase())
      .filter((part) => part !== "");
  } else if (Array.isArray(raw)) {
    pieces = raw.map((part) => String(part).trim().toLowerCase()).filter((part) => part !== "");
  } else {
    // Anything else is not a roles claim. Not an error: an issuer that put a
    // number there has said nothing about roles, and the allow-list stands in
    // with the least privileged one.
    pieces = [];
  }
  // Prefer claim roles when any known role is present. When the claim is
  // empty or only unknown values (typical Cloudflare Access JWTs have no
  // roles claim), apply jwt_default_role if configured, else
  // filterKnownRoles falls back to viewer. Matches Go's rolesFromClaims /
  // C#'s RolesFromClaimList / Python's _roles_from_claims.
  const known = new Set(pieces.filter((role) => KNOWN_ROLE_SET.has(role)));
  if (known.size > 0) {
    return known;
  }
  const defaultRole = (settings.jwt_default_role ?? "").trim();
  if (defaultRole !== "") {
    return filterKnownRoles([defaultRole]);
  }
  return filterKnownRoles(pieces);
}

/** The scopes a claim set holds. Unlike roles, these are not filtered. */
export function scopesFromClaims(claims: JwtClaims, settings: AuthSettings): ReadonlySet<string> {
  const raw = claims[settings.jwt_scopes_claim];
  if (typeof raw === "string") {
    return new Set(raw.split(/\s+/).filter((part) => part !== ""));
  }
  if (Array.isArray(raw)) {
    return new Set(raw.map((part) => String(part).trim()).filter((part) => part !== ""));
  }
  return new Set();
}

/**
 * Verify a token and turn it into a principal.
 *
 * @throws {JwtError} When the token does not verify, when it carries no
 *   usable subject, or when this deployment configured no key to check it
 *   with — the last being a refusal rather than an unverified accept.
 */
export function principalFromJwtToken(
  token: string,
  settings: AuthSettings,
  now?: (() => number) | undefined,
): ServerPrincipal {
  const key = settings.jwt_public_key_pem;
  if (key === null || key === "") {
    throw new JwtError("ValueError", "jwt_public_key_pem or jwt_jwks_url must be configured in jwt mode");
  }
  const claims = decodeJwt(token, {
    key,
    algorithms: settings.jwt_algorithms,
    // Neither can be absent: the reference types both as plain strings, so a
    // deployment cannot switch either check off by leaving one unset.
    issuer: settings.jwt_issuer,
    audience: settings.jwt_audience,
    // The reference floors a negative skew at zero rather than letting it
    // shorten a token's life.
    leeway: Math.max(0, Math.trunc(settings.clock_skew_seconds)),
    require: REQUIRED_JWT_CLAIMS,
    now,
  });

  // Present and not null: `sub` is one of the required claims, checked before
  // any claim is read.
  const subject = String(claims.sub).trim();
  if (subject === "") {
    throw new JwtError("ValueError", "sub claim is required");
  }
  const claimed = claims[settings.jwt_tenant_claim];
  const rawTenant = claimed === undefined || claimed === null ? "" : String(claimed).trim();
  const tenant = canonicalTenantId(rawTenant);
  if (rawTenant !== "" && tenant === undefined) {
    throw new JwtError("ValueError", "invalid tenant_id claim");
  }
  return {
    subject_id: subject,
    tenant_id: tenant ?? null,
    roles: rolesFromClaims(claims, settings),
    scopes: scopesFromClaims(claims, settings),
    claims,
  };
}

/**
 * The principal for a request, or the anonymous one.
 *
 * Every way of failing lands on the same principal, which is what makes "no
 * credential" and "a credential that does not verify" indistinguishable to
 * whoever sent them.
 */
export function resolveJwtPrincipal(
  headers: { get(name: string): string | null },
  settings: AuthSettings,
  now?: (() => number) | undefined,
): ServerPrincipal {
  const token = extractBearerToken(headers);
  if (token === undefined) {
    return anonymousPrincipal();
  }
  try {
    return principalFromJwtToken(token, settings, now);
  } catch {
    return anonymousPrincipal();
  }
}
