//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Server configuration validation.
 *
 * Port of the Python module `provide.uterm.server.config_schema`.
 *
 * Configuration is where a deployment's security posture is actually set, and
 * a validator that accepts a bad combination has made that decision for the
 * operator without telling them.
 */

import { AUTH_DEFAULTS, AUTH_FIELDS } from "./defaults.ts";

/** Hosts that count as this machine. */
export const LOOPBACK_HOSTS: readonly string[] = ["127.0.0.1", "::1", "localhost"];

const LOOPBACK = new Set(LOOPBACK_HOSTS);

/**
 * Normalise a mount path.
 *
 * A path without a leading slash registers a route nothing matches, and a
 * trailing one makes two spellings of the same mount.
 */
export function cleanPath(value: string | undefined, fallback: string): string {
  let text = String(value === undefined || value === "" ? fallback : value).trim();
  if (!text.startsWith("/")) {
    text = `/${text}`;
  }
  // The root has to survive: stripping its trailing slash would leave nothing.
  return text.replace(/\/+$/, "") || "/";
}

/**
 * Refuse a cleartext outbound URL to anywhere but this machine.
 *
 * These channels carry HMAC secrets, auth headers and the JWKS used to
 * validate admin tokens, so cleartext to a routable host is not a warning.
 * Any scheme that is neither `http` nor `https` is refused outright rather
 * than handed to a client that might do something surprising with it.
 *
 * @throws {Error} Naming the field, so an operator knows which line to change.
 */
export function requireSecureUrl(url: string | undefined, fieldName: string): void {
  if (url === undefined || url === "") {
    return;
  }
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`${fieldName} must use http(s)`);
  }
  if (parsed.protocol === "https:") {
    return;
  }
  if (parsed.protocol !== "http:") {
    throw new Error(`${fieldName} must use http(s)`);
  }
  // Brackets come off an IPv6 host; the comparison is against the address.
  // The lower-casing is belt and braces here — WHATWG URL parsing already
  // normalises an ASCII hostname — but the reference lower-cases explicitly
  // and the comparison should not depend on which parser is underneath.
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  // `.localhost` resolves to loopback by convention. A host that merely ends
  // in the *word* is somebody else's, and matching it would be an SSRF.
  if (LOOPBACK.has(host) || host.endsWith(".localhost")) {
    return;
  }
  throw new Error(`${fieldName} must use https:// (cleartext http:// is only allowed for loopback hosts)`);
}

/** Whether a configured string is present rather than blank. */
function present(value: unknown): boolean {
  return String(value ?? "").trim() !== "";
}

/**
 * Refuse a field the schema does not define.
 *
 * A typo in a config file is otherwise a setting that silently does nothing —
 * including a security setting the operator believes is on.
 *
 * @throws {Error} When the input carries a field the model does not have.
 */
function rejectUnknownFields(input: Record<string, unknown>, known: ReadonlySet<string>): void {
  for (const key of Object.keys(input)) {
    if (!known.has(key)) {
      throw new Error("Extra inputs are not permitted");
    }
  }
}

/** What an auth section may carry. */
export interface AuthConfigInput {
  [key: string]: unknown;
}

/**
 * Check an auth section.
 *
 * @throws {Error} On an unknown field, an unsatisfiable combination, or a
 *   cleartext outbound URL.
 */
export function validateAuthConfig(input: AuthConfigInput): void {
  rejectUnknownFields(input, AUTH_FIELDS);
  if (input.require_upstream_proxy_secret === true && !present(input.upstream_proxy_secret)) {
    throw new Error("auth.upstream_proxy_secret is required when auth.require_upstream_proxy_secret=True");
  }
  requireSecureUrl(input.webhook_idp_url as string | undefined, "auth.webhook_idp_url");
  requireSecureUrl(input.jwt_jwks_url as string | undefined, "auth.jwt_jwks_url");
  const identityProvider = (input.identity_provider ?? AUTH_DEFAULTS.identityProvider) as string;
  const requireSigned =
    (input.webhook_idp_require_signed_response as boolean | undefined) ?? AUTH_DEFAULTS.webhookIdpRequireSignedResponse;
  // Verifying a response signature without a shared secret can never succeed.
  // Refusing at load time beats failing every request, or silently not
  // verifying at all.
  if (identityProvider === "webhook" && requireSigned && !present(input.webhook_idp_secret)) {
    throw new Error(
      "requiring a signed IdP response needs auth.webhook_idp_secret; set the secret or " +
        "set auth.webhook_idp_require_signed_response=false to disable verification",
    );
  }
}

/**
 * Check an audit section.
 *
 * @throws {Error} When the chain is on with nowhere to write — a
 *   misconfiguration, not a silent no-op, and the chain's whole point is that
 *   it is tamper-evident.
 */
export function validateAuditConfig(input: Record<string, unknown>): void {
  if (input.chain_enabled === true && !present(input.chain_file)) {
    throw new Error("audit.chain_enabled requires audit.chain_file (the append-only WORM log path)");
  }
}

/**
 * Check a recording section.
 *
 * Zero means unlimited, and zero retention means keep indefinitely; a
 * negative is a typo.
 *
 * @throws {Error} On a negative bound.
 */
export function validateRecordingConfig(input: Record<string, unknown>): void {
  const maxBytes = input.max_bytes;
  if (typeof maxBytes === "number" && maxBytes < 0) {
    throw new Error(`recording.max_bytes must be >= 0 (0 = unlimited), got: ${maxBytes}`);
  }
  const retention = input.retention_s;
  if (typeof retention === "number" && retention < 0) {
    throw new Error(`recording.retention_s must be >= 0 (0 = keep indefinitely), got: ${retention}`);
  }
  requireSecureUrl(input.webhook_url as string | undefined, "recording.webhook_url");
}

/**
 * Check a control-plane section.
 *
 * @throws {Error} On a non-positive reap interval — zero would be a loop with
 *   no delay in it — a negative retention, or a sqlite backend with nowhere
 *   to store.
 */
export function validateControlPlaneConfig(input: Record<string, unknown>): void {
  const interval = input.reap_interval_s;
  if (typeof interval === "number" && interval <= 0) {
    throw new Error(`control_plane.reap_interval_s must be > 0, got: ${interval}`);
  }
  const retention = input.reap_retention_s;
  if (typeof retention === "number" && retention < 0) {
    throw new Error(`control_plane.reap_retention_s must be >= 0 (0 = reap as soon as past expiry), got: ${retention}`);
  }
  if (input.backend === "sqlite" && !present(input.database_url)) {
    throw new Error("control_plane.database_url is required when control_plane.backend='sqlite'");
  }
}

/**
 * Check a PAM section.
 *
 * @throws {Error} On a cleartext relay URL — it carries session notifications
 *   about who logged in where.
 */
export function validatePamConfig(input: Record<string, unknown>): void {
  requireSecureUrl(input.relay_url as string | undefined, "pam.relay_url");
}

/**
 * Check a governance section.
 *
 * Five outbound channels, every one of them either carrying a shared secret or
 * being asked whether a session may proceed. A policy or authorisation answer
 * arriving over cleartext is one anybody on the path can rewrite.
 *
 * @throws {Error} On a cleartext URL to anywhere but loopback.
 */
export function validateGovernanceConfig(input: Record<string, unknown>): void {
  for (const field of [
    "policy_webhook_url",
    "registry_webhook_url",
    "authz_webhook_url",
    "behavioral_audit_url",
    "telemetry_webhook_url",
  ]) {
    requireSecureUrl(input[field] as string | undefined, `governance.${field}`);
  }
}

/**
 * The base URL the server hands out in links.
 *
 * Derived from the bind when the operator gave none; behind a proxy the bind
 * address is not what a browser can reach, so an explicit one always wins.
 */
export function derivePublicBaseUrl(host: string, port: number, explicit?: string): string {
  return explicit !== undefined && explicit !== "" ? explicit : `http://${host}:${port}`;
}
