//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * JWT verification for the Worker, and the cache in front of the JWKS
 * endpoint.
 *
 * Port of the verifying half of `provide.uterm.cloudflare.auth.jwt`.
 *
 * The reference has two verification paths — Web Crypto inside the Cloudflare
 * Pyodide runtime, PyJWT everywhere else — because Python has no one
 * implementation that runs in both. `crypto.subtle` is native in Node and in
 * a Worker, so there is one path here, and unlike the reference's (which is
 * marked no-cover and reachable only from integration tests) it is tested
 * directly.
 */

import type { JwtConfig } from "./config.ts";
import { extractRoles, findJwk, JwtValidationError, type Principal, parseJwtParts, validateClaims } from "./jwt.ts";

/** How long a fetched JWKS is served without asking again. */
export const JWKS_CACHE_TTL_S = 60;

/**
 * How long a failed refresh is remembered before another is attempted.
 *
 * Without it a known-bad endpoint is re-hit on every single request.
 */
export const JWKS_NEGATIVE_TTL_S = 5;

/** The one algorithm this verifier implements. */
const SUPPORTED_ALGORITHM = "RS256";

/** What `crypto.subtle` calls it. */
const WEB_CRYPTO_ALGORITHM = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" } as const;

/** What the cache holds for one endpoint. */
interface CacheEntry {
  fetchedAt: number;
  jwks: unknown;
}

/** How to reach a JWKS endpoint, and what time it is. */
export interface JwksCacheOptions {
  fetch(url: string): Promise<unknown>;
  /** A monotonic clock, in seconds. */
  now?(): number;
  ttlS?: number;
  negativeTtlS?: number;
}

/**
 * A per-isolate JWKS cache with a stale-on-error fallback.
 *
 * A network round-trip on every authenticated request would be expensive
 * enough; a JWKS endpoint that flaps would be worse, because it would take
 * down all authentication while it did. So when a refresh fails and a
 * previously-fetched copy is still held, that copy is served and further
 * attempts are suppressed for the negative TTL.
 *
 * A first-ever fetch with nothing to fall back on still fails: there is no
 * copy to serve, and inventing one would mean accepting tokens signed by
 * anything.
 */
export class JwksCache {
  readonly #fetch: (url: string) => Promise<unknown>;
  readonly #now: () => number;
  readonly #ttlS: number;
  readonly #negativeTtlS: number;
  /** Cached keys, per endpoint — one issuer's must never be served for another's. */
  readonly #entries = new Map<string, CacheEntry>();
  /** Endpoints whose refresh failed, and when another attempt is allowed. */
  readonly #retryAfter = new Map<string, number>();

  constructor(options: JwksCacheOptions) {
    this.#fetch = options.fetch;
    this.#now = options.now ?? (() => Date.now() / 1000);
    this.#ttlS = options.ttlS ?? JWKS_CACHE_TTL_S;
    this.#negativeTtlS = options.negativeTtlS ?? JWKS_NEGATIVE_TTL_S;
  }

  /** The keys for one endpoint, fetched or served from cache. */
  async get(url: string): Promise<unknown> {
    const now = this.#now();
    const cached = this.#entries.get(url);
    if (cached !== undefined) {
      if (now - cached.fetchedAt < this.#ttlS) {
        return cached.jwks;
      }
      // Stale, but a recent refresh failed: keep serving it rather than
      // re-hitting an endpoint already known to be down.
      if (now < (this.#retryAfter.get(url) ?? 0)) {
        return cached.jwks;
      }
    }

    let fetched: unknown;
    try {
      fetched = await this.#fetch(url);
    } catch (error) {
      if (cached === undefined) {
        throw error;
      }
      this.#retryAfter.set(url, now + this.#negativeTtlS);
      return cached.jwks;
    }

    this.#entries.set(url, { fetchedAt: this.#now(), jwks: fetched });
    // Housekeeping rather than logic: a success can only happen once the
    // deadline has passed, so the stale deadline could never suppress a
    // later refresh. Kept so the map does not grow one entry per endpoint
    // that ever failed.
    this.#retryAfter.delete(url);
    return fetched;
  }
}

/**
 * Verify a token's signature against a JWKS, then validate its claims.
 *
 * A correctly signed token is not thereby a usable one, so both happen here
 * and in that order — there is no point checking the claims of something
 * nobody signed.
 */
export async function verifyRs256(
  token: string,
  jwks: unknown,
  config: JwtConfig,
  now?: number,
): Promise<Record<string, unknown>> {
  const { header, payload, signature, signingInput } = parseJwtParts(token);
  // A header that is `null` or a list parses without complaint — nothing
  // upstream refuses it — and reads as having no fields at all, which the
  // algorithm check below then rejects.
  // The empty fallback cannot change an answer — reading `.alg` off a string
  // or an array yields undefined either way, and the check below refuses it.
  // It is here so the narrowing is stated rather than relied upon.
  const fields = (typeof header === "object" && header !== null ? header : {}) as Record<string, unknown>;
  const alg = fields.alg;

  // The deployment's list first: `none` is the classic forgery — a token
  // asking not to be checked — and it is refused here rather than reaching
  // the verifier.
  // The type test narrows for the comparison below; at runtime the
  // membership check alone would refuse a non-string just as surely, since
  // the configured list holds only strings.
  if (typeof alg !== "string" || !config.algorithms.includes(alg)) {
    throw new JwtValidationError(`unsupported algorithm: ${String(alg)}`);
  }
  // Then what this verifier actually implements. A deployment configuring
  // ES256 has to be told, not handed an RS256 check against an EC key.
  if (alg !== SUPPORTED_ALGORITHM) {
    throw new JwtValidationError(`Web Crypto path only supports RS256, got ${alg}`);
  }

  // Reachable only once the algorithm check has passed, so the header is
  // known to carry fields by here.
  const kid = fields.kid;
  const jwk = findJwk(jwks, typeof kid === "string" ? kid : undefined, alg);

  let key: Awaited<ReturnType<typeof crypto.subtle.importKey>>;
  try {
    // Cast rather than typed: the JWK shape lives in the DOM library, which
    // this package does not take, and a Worker has no DOM either.
    key = await crypto.subtle.importKey("jwk", jwk as never, WEB_CRYPTO_ALGORITHM, false, ["verify"]);
  } catch (error) {
    // A malformed JWK is a refusal, not an exception escaping into the
    // request path.
    throw new JwtValidationError(`unusable key in JWKS: ${(error as Error).message}`);
  }

  // Re-wrapped rather than passed straight through: with the DOM types in
  // scope, `BufferSource` wants an `ArrayBuffer`-backed view, and these are
  // typed over the wider `ArrayBufferLike`. Same bytes either way.
  const valid = await crypto.subtle.verify(
    WEB_CRYPTO_ALGORITHM.name,
    key,
    new Uint8Array(signature),
    new Uint8Array(signingInput),
  );
  if (!valid) {
    throw new JwtValidationError("signature verification failed");
  }

  validateClaims(payload, config, now);
  return payload as Record<string, unknown>;
}

/**
 * Fetch a JWKS document over HTTP.
 *
 * The scheme is checked before the request is made. The URL comes from
 * deployment configuration and should always be http(s); refusing anything
 * else turns an assumption into a preflight check, and keeps a misconfigured
 * `file:` URL from being opened.
 */
export async function requestJwks(url: string): Promise<unknown> {
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    throw new JwtValidationError(`JWKS URL must be http(s), got: ${url}`);
  }
  const response = await fetch(url);
  if (!response.ok) {
    throw new JwtValidationError(`JWKS fetch failed: ${response.status}`);
  }
  return response.json();
}

/**
 * The cache the Worker uses, one per isolate.
 *
 * The reference keeps this in module globals for the same reason: it is a
 * per-isolate cache, and its TTL only means anything within one long-lived
 * isolate.
 */
export const defaultJwksCache = new JwksCache({ fetch: requestJwks });

/** How to verify, for a caller supplying its own keys or clock. */
export interface DecodeJwtDeps {
  verify?(token: string, config: JwtConfig): Promise<unknown>;
  /** The keys, if not the default per-isolate cache. */
  jwks?: Pick<JwksCache, "get">;
  now?: number;
}

/** Read a claim off a verified payload. */
function claim(claims: unknown, name: string): unknown {
  return typeof claims === "object" && claims !== null ? (claims as Record<string, unknown>)[name] : undefined;
}

/**
 * Verify a token and derive the principal it names.
 *
 * There is no dev bypass: the Worker is always internet-facing, so without a
 * key configured nothing can be verified and nothing is.
 */
export async function decodeJwt(token: string, config: JwtConfig, deps: DecodeJwtDeps = {}): Promise<Principal> {
  if (!config.public_key_pem && !config.jwks_url) {
    throw new JwtValidationError("missing jwt public key");
  }

  // The default fetches the advertised keys and checks the signature. A
  // caller that already holds the keys, or that is testing, supplies its own.
  const verify =
    deps.verify ??
    (async (candidate: string, active: JwtConfig) => {
      if (!active.jwks_url) {
        // A static PEM is the reference's other path; this verifier reads a
        // JWKS, so say which is missing rather than failing on the fetch.
        throw new JwtValidationError("jwt_jwks_url must be configured to verify with Web Crypto");
      }
      const jwks = await (deps.jwks ?? defaultJwksCache).get(active.jwks_url);
      return verifyRs256(candidate, jwks, active, deps.now);
    });

  let claims: unknown;
  try {
    claims = await verify(token, config);
  } catch (error) {
    // A validation error already says why the token was refused; wrapping it
    // would bury that. Anything else is reported as a refusal too, because
    // as far as the request is concerned an unusable key is simply not
    // authenticated.
    if (error instanceof JwtValidationError) {
      throw error;
    }
    throw new JwtValidationError(`failed to verify token: ${(error as Error).message}`);
  }

  const commonName = String(claim(claims, "common_name") ?? "");
  // A Cloudflare Access service token carries a common name and no human
  // identity. An `email` claim means a user token, which can never be
  // elevated as a service token however the rest of it reads.
  const isServiceToken = commonName !== "" && !claim(claims, "email");

  const subject = String(claim(claims, "sub") ?? "") || commonName;
  if (subject === "") {
    // An unnamed principal cannot be audited, so there is nothing to grant.
    throw new JwtValidationError("missing sub");
  }

  // Admin only where the deployment opted in: a bare common name is too weak
  // a signal to grant it automatically.
  const roles = isServiceToken && config.jwt_service_token_admin ? ["admin"] : extractRoles(claims, config);
  return { subjectId: subject, roles };
}
