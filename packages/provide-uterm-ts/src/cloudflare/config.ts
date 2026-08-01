//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Worker configuration, read from the environment.
 *
 * Port of the Python module `provide.uterm.cloudflare.config`.
 *
 * A Worker is always internet-facing. There is no loopback to fall back on
 * and no "only reachable from inside", so the refusals here are the outermost
 * auth boundary a deployment has — and each one is a deployment that must not
 * start rather than a warning to be logged.
 *
 * The reference repeats several values that live elsewhere in the codebase,
 * because the Cloudflare package is separate and may not import from the
 * server or the core. There is one package here, so the shared ones are
 * imported: the DeckMux idle window, and CPython's own numeric parsing.
 */

import { DEFAULT_AUTO_TRANSFER_IDLE_S } from "../deckmux/index.ts";
import { pyFloat, pyInt } from "../pycompat/index.ts";

/** JWT settings. */
export interface JwtConfig {
  mode: string;
  issuer: string | null;
  audience: string | null;
  algorithms: readonly string[];
  public_key_pem: string | null;
  jwks_url: string | null;
  clock_skew_seconds: number;
  jwt_roles_claim: string;
  jwt_scopes_claim: string;
  jwt_default_role: string;
  jwt_role_map: Record<string, string>;
  jwt_service_token_admin: boolean;
}

/** Size and rate ceilings. */
export interface LimitsConfig {
  max_ws_message_bytes: number;
  max_input_chars: number;
  max_events_per_worker: number;
  max_buffer_bytes: number;
  backpressure_high_water_bytes: number;
  backpressure_low_water_bytes: number;
  backpressure_ack_grace_s: number;
}

/** Where the Worker connects out to. */
export interface UpstreamConfig {
  base_ws_url: string;
  connect_timeout_ms: number;
  heartbeat_s: number;
  max_backoff_s: number;
}

/** Everything the Worker was configured with. */
export interface CloudflareConfig {
  environment: string;
  log_level: string;
  durable_object_class: string;
  jwt: JwtConfig;
  limits: LimitsConfig;
  upstream: UpstreamConfig;
  worker_bearer_token: string | null;
  tunnel_token_ttl_s: number;
  tunnel_token_transport: string;
  tunnel_ip_binding: boolean;
  security_mode: string;
  security_csp: string | null;
  security_hsts: string | null;
  security_x_frame_options: string | null;
  security_x_content_type_options: string | null;
  security_referrer_policy: string | null;
  security_permissions_policy: string | null;
  deckmux_auto_transfer_idle_s: number;
  deckmux_keystroke_queue: string;
  resume_ttl_s: number;
  hijack_lease_s: number;
  resume_enabled: boolean;
}

/** Raised when a configuration must not be started with. */
export class CloudflareConfigError extends Error {}

/**
 * The shortest worker bearer token that will be accepted.
 *
 * Thirty-two characters is roughly 128 bits of entropy across the common
 * encodings — raw bytes, hex, base64.
 */
export const MIN_BEARER_TOKEN_CHARS = 32;

/** The HMAC algorithms, which must never be combined with an asymmetric key. */
const HMAC_ALGORITHMS: ReadonlySet<string> = new Set(["HS256", "HS384", "HS512"]);

/** Tokens an operator might plausibly have left in by mistake. */
const PLACEHOLDER_BEARER_VALUES: ReadonlySet<string> = new Set([
  "change-me",
  "changeme",
  "placeholder",
  "replace-me",
  "secret",
  "test",
  "password",
  "token",
  "dev",
  "worker-token",
  "test-worker-token",
  "dummy-token",
  "worker-secret",
]);

/**
 * Compound phrases that mark a placeholder wherever they appear.
 *
 * Kept compound deliberately: a bare "token" as a substring would reject
 * legitimate high-entropy material that happens to contain it.
 */
const PLACEHOLDER_BEARER_MARKERS: readonly string[] = [
  "change-me",
  "changeme",
  "placeholder",
  "replace-me",
  "replace-with",
  "replace_with",
];

/** The words that mean yes. */
const TRUTHY: ReadonlySet<string> = new Set(["1", "true", "yes", "y", "on"]);

/**
 * Refuse a placeholder or low-entropy worker bearer token.
 *
 * Applied unconditionally, not only in production: a Worker is always
 * internet-facing, so this token is an edge auth boundary whatever the
 * environment claims.
 *
 * @throws {CloudflareConfigError} On a known placeholder or a short token.
 */
function rejectWeakBearerToken(value: string): void {
  const text = value.trim();
  const lowered = text.toLowerCase();
  if (PLACEHOLDER_BEARER_VALUES.has(lowered) || PLACEHOLDER_BEARER_MARKERS.some((marker) => lowered.includes(marker))) {
    throw new CloudflareConfigError(
      "WORKER_BEARER_TOKEN uses a known placeholder value. Set a high-entropy runtime " +
        "token (e.g. `python -c 'import secrets; print(secrets.token_urlsafe(32))'`).",
    );
  }
  if (text.length < MIN_BEARER_TOKEN_CHARS) {
    throw new CloudflareConfigError(
      `WORKER_BEARER_TOKEN must be at least ${MIN_BEARER_TOKEN_CHARS} characters of ` +
        "high-entropy material. CF Workers are always internet-facing, so the worker " +
        "bearer token is an edge auth boundary — use a long random token.",
    );
  }
}

/**
 * Whether a PEM carries an asymmetric marker.
 *
 * A raw HMAC shared secret has no PEM header, so it is not one — refusing it
 * would block a legitimate shared-secret deployment.
 */
function looksLikeAsymmetricKey(pem: string | null): boolean {
  return pem !== null && (pem.includes("PUBLIC KEY") || pem.includes("BEGIN CERTIFICATE"));
}

/**
 * Refuse a JWT configuration that invites algorithm confusion.
 *
 * With an HS* algorithm configured alongside an asymmetric algorithm, a JWKS
 * URL, or an asymmetric public key, a token can be forged by using the public
 * key bytes as the HMAC secret. The deployment looks fine and accepts forged
 * tokens, so the combination is refused loudly at startup.
 *
 * @throws {CloudflareConfigError} On such a combination.
 */
function rejectJwtAlgorithmConfusion(
  algorithms: readonly string[],
  publicKeyPem: string | null,
  jwksUrl: string | null,
): void {
  if (!algorithms.some((algorithm) => HMAC_ALGORITHMS.has(algorithm))) {
    return;
  }
  const hasAsymmetricAlgorithm = algorithms.some((algorithm) => !HMAC_ALGORITHMS.has(algorithm));
  const hasAsymmetricKey = (jwksUrl !== null && jwksUrl !== "") || looksLikeAsymmetricKey(publicKeyPem);
  if (hasAsymmetricAlgorithm || hasAsymmetricKey) {
    throw new CloudflareConfigError(
      "JWT_ALGORITHMS must not combine HMAC (HS*) with asymmetric algorithms " +
        "or an asymmetric public key / JWKS URL (algorithm-confusion risk)",
    );
  }
}

/** The defaults, before any environment is read. */
export function defaultConfig(): CloudflareConfig {
  return {
    environment: "development",
    log_level: "info",
    durable_object_class: "SessionRuntime",
    jwt: {
      mode: "jwt",
      issuer: null,
      audience: null,
      algorithms: ["RS256"],
      public_key_pem: null,
      jwks_url: null,
      clock_skew_seconds: 30,
      jwt_roles_claim: "roles",
      jwt_scopes_claim: "scope",
      jwt_default_role: "viewer",
      jwt_role_map: {},
      jwt_service_token_admin: false,
    },
    limits: {
      max_ws_message_bytes: 1_048_576,
      max_input_chars: 10_000,
      max_events_per_worker: 2_000,
      max_buffer_bytes: 1_048_576,
      backpressure_high_water_bytes: 4_194_304,
      backpressure_low_water_bytes: 1_048_576,
      backpressure_ack_grace_s: 10.0,
    },
    upstream: { base_ws_url: "", connect_timeout_ms: 3_000, heartbeat_s: 25, max_backoff_s: 5 },
    worker_bearer_token: null,
    tunnel_token_ttl_s: 3600,
    tunnel_token_transport: "cookie",
    tunnel_ip_binding: false,
    security_mode: "strict",
    security_csp: null,
    security_hsts: null,
    security_x_frame_options: null,
    security_x_content_type_options: null,
    security_referrer_policy: null,
    security_permissions_policy: null,
    // Imported rather than repeated: the reference restates it only because
    // its Cloudflare package cannot import from the core one.
    deckmux_auto_transfer_idle_s: DEFAULT_AUTO_TRANSFER_IDLE_S,
    deckmux_keystroke_queue: "display",
    resume_ttl_s: 300,
    hijack_lease_s: 60,
    resume_enabled: true,
  };
}

/** Read one variable out of whatever shape the environment arrived in. */
function reader(env: unknown): (name: string) => string | null {
  const source = (env as { vars?: unknown } | null)?.vars ?? env;
  return (name: string): string | null => {
    const value = (source as Record<string, unknown> | null)?.[name];
    return value === undefined || value === null ? null : String(value);
  };
}

/** Read a whole number, clamped to a floor. */
function readInt(raw: string | null, fallback: string, floor: number, name: string): number {
  const parsed = pyInt(raw ?? fallback);
  if (parsed === undefined) {
    // Parsed the reference's way, which raises rather than coercing: a Worker
    // that started with a silently-zeroed limit would have the protection
    // disabled and nothing to show for it.
    throw new CloudflareConfigError(`${name} must be a whole number, not ${JSON.stringify(raw)}`);
  }
  return Math.max(floor, parsed);
}

/** Read a fractional number, clamped to a floor. */
function readFloat(raw: string | null, fallback: string, floor: number, name: string): number {
  const parsed = pyFloat(raw ?? fallback);
  if (parsed === undefined) {
    throw new CloudflareConfigError(`${name} must be a number, not ${JSON.stringify(raw)}`);
  }
  return Math.max(floor, parsed);
}

/** Read a flag, defaulting per-variable rather than globally. */
function readBoolean(raw: string | null, fallback: boolean): boolean {
  return TRUTHY.has((raw ?? (fallback ? "1" : "0")).trim().toLowerCase());
}

/** A mapping from IdP group to role, or nothing usable. */
function readRoleMap(raw: string | null): Record<string, string> {
  // Both of these reach the same answer as falling through to the parse — an
  // empty or blank string is not JSON either — but they say that an unset
  // variable is an absent mapping rather than a malformed one.
  const text = (raw ?? "").trim();
  if (text === "") {
    return {};
  }
  // Ignored rather than fatal: this maps IdP groups to roles, so losing it
  // costs a mapping where refusing to start costs the deployment.
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return {};
    }
    return Object.fromEntries(Object.entries(parsed).map(([key, value]) => [String(key), String(value)]));
  } catch {
    return {};
  }
}

/**
 * Read a configuration from a Worker environment.
 *
 * @throws {CloudflareConfigError} On any configuration that must not be
 *   started with — a weak bearer token, an open auth mode, a JWT setup that
 *   invites algorithm confusion, or a numeric setting that is not a number.
 */
export function configFromEnv(env: unknown): CloudflareConfig {
  const defaults = defaultConfig();
  const get = reader(env);
  /** A value, or the default when the variable is absent or empty. */
  const orElse = (name: string, fallback: string): string => {
    const value = get(name);
    return value === null || value === "" ? fallback : value;
  };

  const mode = (get("AUTH_MODE") ?? "jwt").trim().toLowerCase() || "jwt";
  if (mode !== "jwt") {
    // dev and none are gone rather than gated: on a Worker they would be an
    // admin bypass regardless of what ENVIRONMENT says.
    throw new CloudflareConfigError(
      "AUTH_MODE must be 'jwt' (dev/none modes are removed; the worker is always internet-facing)",
    );
  }

  const algorithms = (get("JWT_ALGORITHMS") ?? "RS256")
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part !== "");
  const publicKeyPem = get("JWT_PUBLIC_KEY_PEM") || null;
  const jwksUrl = get("JWT_JWKS_URL") || null;
  const jwt: JwtConfig = {
    mode,
    issuer: get("JWT_ISSUER") || null,
    audience: get("JWT_AUDIENCE") || null,
    // Never empty: a Worker accepting no algorithm at all rejects every token
    // rather than being secure.
    algorithms: algorithms.length > 0 ? algorithms : defaults.jwt.algorithms,
    public_key_pem: publicKeyPem,
    jwks_url: jwksUrl,
    clock_skew_seconds: readInt(get("JWT_CLOCK_SKEW_SECONDS"), "30", 0, "JWT_CLOCK_SKEW_SECONDS"),
    jwt_roles_claim: orElse("JWT_ROLES_CLAIM", defaults.jwt.jwt_roles_claim),
    jwt_scopes_claim: orElse("JWT_SCOPES_CLAIM", defaults.jwt.jwt_scopes_claim),
    jwt_default_role: orElse("JWT_DEFAULT_ROLE", defaults.jwt.jwt_default_role),
    jwt_role_map: readRoleMap(get("JWT_ROLE_MAP")),
    jwt_service_token_admin: readBoolean(get("JWT_SERVICE_TOKEN_ADMIN"), false),
  };
  rejectJwtAlgorithmConfusion(jwt.algorithms, jwt.public_key_pem, jwt.jwks_url);

  const workerBearerToken = get("WORKER_BEARER_TOKEN") || null;
  if (workerBearerToken === null) {
    throw new CloudflareConfigError("WORKER_BEARER_TOKEN is required when AUTH_MODE='jwt'");
  }
  rejectWeakBearerToken(workerBearerToken);

  const securityModeRaw = (get("SECURITY_MODE") ?? "strict").trim().toLowerCase() || "strict";
  // Unrecognised falls back to the safe end, so a typo cannot quietly loosen
  // the headers.
  const securityMode = securityModeRaw === "strict" || securityModeRaw === "dev" ? securityModeRaw : "strict";

  return {
    environment: orElse("ENVIRONMENT", defaults.environment),
    log_level: orElse("LOG_LEVEL", defaults.log_level),
    durable_object_class: orElse("DO_CLASS_NAME", defaults.durable_object_class),
    jwt,
    limits: {
      max_ws_message_bytes: readInt(get("MAX_WS_MESSAGE_BYTES"), "1048576", 1024, "MAX_WS_MESSAGE_BYTES"),
      max_input_chars: readInt(get("MAX_INPUT_CHARS"), "10000", 100, "MAX_INPUT_CHARS"),
      max_events_per_worker: readInt(get("MAX_EVENTS_PER_WORKER"), "2000", 100, "MAX_EVENTS_PER_WORKER"),
      max_buffer_bytes: readInt(get("MAX_BUFFER_BYTES"), "1048576", 1024, "MAX_BUFFER_BYTES"),
      backpressure_high_water_bytes: readInt(
        get("BACKPRESSURE_HIGH_WATER_BYTES"),
        "4194304",
        1024,
        "BACKPRESSURE_HIGH_WATER_BYTES",
      ),
      backpressure_low_water_bytes: readInt(
        get("BACKPRESSURE_LOW_WATER_BYTES"),
        "1048576",
        0,
        "BACKPRESSURE_LOW_WATER_BYTES",
      ),
      backpressure_ack_grace_s: readFloat(get("BACKPRESSURE_ACK_GRACE_S"), "10", 0, "BACKPRESSURE_ACK_GRACE_S"),
    },
    upstream: {
      base_ws_url: get("UPSTREAM_BASE_WS_URL") ?? defaults.upstream.base_ws_url,
      connect_timeout_ms: readInt(get("UPSTREAM_CONNECT_TIMEOUT_MS"), "3000", 100, "UPSTREAM_CONNECT_TIMEOUT_MS"),
      heartbeat_s: readInt(get("UPSTREAM_HEARTBEAT_S"), "25", 1, "UPSTREAM_HEARTBEAT_S"),
      max_backoff_s: readInt(get("UPSTREAM_MAX_BACKOFF_S"), "5", 1, "UPSTREAM_MAX_BACKOFF_S"),
    },
    worker_bearer_token: workerBearerToken,
    tunnel_token_ttl_s: readInt(get("TUNNEL_TOKEN_TTL_S"), "3600", 60, "TUNNEL_TOKEN_TTL_S"),
    tunnel_token_transport: orElse("TUNNEL_TOKEN_TRANSPORT", defaults.tunnel_token_transport),
    tunnel_ip_binding: readBoolean(get("TUNNEL_IP_BINDING"), false),
    security_mode: securityMode,
    // Present-but-empty is kept: an operator writing an empty header is
    // switching it off deliberately, and defaulting it back would override
    // them. Only an absent variable means "unset".
    security_csp: get("SECURITY_CSP"),
    security_hsts: get("SECURITY_HSTS"),
    security_x_frame_options: get("SECURITY_X_FRAME_OPTIONS"),
    security_x_content_type_options: get("SECURITY_X_CONTENT_TYPE_OPTIONS"),
    security_referrer_policy: get("SECURITY_REFERRER_POLICY"),
    security_permissions_policy: get("SECURITY_PERMISSIONS_POLICY"),
    deckmux_auto_transfer_idle_s: readInt(
      get("DECKMUX_AUTO_TRANSFER_IDLE_S"),
      String(DEFAULT_AUTO_TRANSFER_IDLE_S),
      1,
      "DECKMUX_AUTO_TRANSFER_IDLE_S",
    ),
    deckmux_keystroke_queue: orElse("DECKMUX_KEYSTROKE_QUEUE", defaults.deckmux_keystroke_queue),
    resume_ttl_s: readInt(get("RESUME_TTL_S"), "300", 30, "RESUME_TTL_S"),
    // Clamped from both ends: a zero-second lease disables hijacking, and an
    // hour-plus one holds a session hostage past any plausible use.
    hijack_lease_s: Math.min(3600, readInt(get("HIJACK_LEASE_S"), "60", 1, "HIJACK_LEASE_S")),
    resume_enabled: readBoolean(get("RESUME_ENABLED"), true),
  };
}
