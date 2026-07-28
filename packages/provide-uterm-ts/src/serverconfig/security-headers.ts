//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The security headers a response carries.
 *
 * Port of the resolver in `provide.uterm.server.security`. Which headers go
 * out is a function of the mode and any per-header overrides, and nothing
 * else — the middleware that attaches them is framework-specific and is not
 * here.
 */

/** The strict set, which is what a production deployment serves. */
const STRICT_DEFAULTS: Readonly<Record<string, string>> = {
  "Content-Security-Policy":
    "default-src 'self'; " +
    "script-src 'self' cdn.jsdelivr.net; " +
    "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net fonts.googleapis.com; " +
    "font-src fonts.gstatic.com; " +
    "connect-src 'self' ws: wss:; " +
    "img-src 'self' data:",
  "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
  "X-Frame-Options": "DENY",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
};

/**
 * The relaxed set.
 *
 * One header survives: content sniffing is not a development convenience, it
 * is a bug in a browser that a page cannot work around.
 */
const DEV_DEFAULTS: Readonly<Record<string, string>> = {
  "X-Content-Type-Options": "nosniff",
};

/** The only mode that is strict. Anything else takes the relaxed set. */
const STRICT_MODE = "strict";

/** Each configurable field and the header it sets, in the order they are emitted. */
export const SECURITY_HEADER_FIELDS: ReadonlyArray<readonly [string, string]> = [
  ["csp", "Content-Security-Policy"],
  ["hsts", "Strict-Transport-Security"],
  ["x_frame_options", "X-Frame-Options"],
  ["x_content_type_options", "X-Content-Type-Options"],
  ["referrer_policy", "Referrer-Policy"],
  ["permissions_policy", "Permissions-Policy"],
];

/** Just the fields the resolver reads. */
export interface SecurityHeaderConfig {
  mode: string;
  csp?: string | null;
  hsts?: string | null;
  x_frame_options?: string | null;
  x_content_type_options?: string | null;
  referrer_policy?: string | null;
  permissions_policy?: string | null;
}

/**
 * Build the header list from a configuration.
 *
 * An override that is a non-empty string replaces the default; one that is
 * *empty* suppresses the header entirely; an absent one takes the mode's
 * default. Those last two are different intentions and the configuration
 * expresses them differently — a deployment behind a proxy that already sets
 * a policy has to be able to turn one off without turning them all off.
 *
 * The mode is compared exactly, with no trimming or folding. That is the
 * reference's behaviour and it is worth knowing rather than smoothing over:
 * `security.mode` is normalised where the posture report reads it and taken
 * verbatim here, so a config writing `STRICT` reports as strict and serves
 * the relaxed headers. Erring toward the relaxed set is at least the visible
 * failure — a misspelling that silently produced strictness nobody asked for
 * would break a page with no clue why.
 */
export function resolveSecurityHeaders(config: SecurityHeaderConfig): ReadonlyArray<readonly [string, string]> {
  const defaults = config.mode === STRICT_MODE ? STRICT_DEFAULTS : DEV_DEFAULTS;
  const resolved: Array<readonly [string, string]> = [];
  for (const [field, header] of SECURITY_HEADER_FIELDS) {
    const override = (config as unknown as Record<string, unknown>)[field];
    if (override !== undefined && override !== null) {
      // Only the empty string suppresses; a value of one space is a value.
      if (override !== "") {
        resolved.push([header, String(override)]);
      }
      continue;
    }
    const fallback = defaults[header];
    if (fallback !== undefined) {
      resolved.push([header, fallback]);
    }
  }
  return resolved;
}
