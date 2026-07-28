//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The server's security posture, as one report.
 *
 * Port of `provide.uterm.server.app.posture`. Eight or so independent knobs
 * collapsed into a single answer: which weakening opt-outs are actually
 * active, what the server is bound to, and one `secure` summary. Pure, so it
 * can be logged at startup and served from an auth-gated endpoint.
 */

/** The hosts that mean "cannot be reached off-box". */
const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(["127.0.0.1", "localhost", "::1", "[::1]", "0:0:0:0:0:0:0:1"]);

/** Just the fields the report reads. */
export interface PostureConfig {
  environment: string;
  server: { host: string };
  auth: Record<string, unknown>;
  security: Record<string, unknown>;
  audit?: { chain_enabled?: boolean } | undefined;
}

/** What the report says. */
export interface SecurityPosture {
  environment: string;
  bind_host: string;
  is_loopback: boolean;
  auth_mode: string;
  /** Active, security-weakening opt-outs, sorted so the report reads the same every time. */
  dev_opt_outs: string[];
  idp_signing_required: boolean | null;
  idp_response_replay_protected: boolean;
  audit_chain_enabled: boolean;
  warnings: string[];
  secure: boolean;
}

/** Normalise a value a person wrote in a config file. */
function text(value: unknown, fallback = ""): string {
  return String(value ?? fallback)
    .trim()
    .toLowerCase();
}

/** Whether a bind address can be reached off-box. */
function isLoopbackHost(host: string): boolean {
  return LOOPBACK_HOSTS.has(host);
}

/**
 * Report the effective posture of a running configuration.
 *
 * An opt-out counts only where it is actually relaxing something. Two of the
 * knobs are acknowledgements: they weaken nothing on their own and matter
 * only paired with the mode they unlock. Listing them regardless would report
 * a posture worse than the deployment has, and a report that cries wolf stops
 * being read.
 */
export function computeSecurityPosture(config: PostureConfig): SecurityPosture {
  const bindHost = text(config.server.host);
  const isLoopback = isLoopbackHost(bindHost);
  // `dev_token` collapses to `jwt` once the dev IdP is set up at startup. The
  // declared mode is preferred so the report surfaces the opt-out rather than
  // the mode it turned into.
  const authMode = text(config.auth._declared_auth_mode ?? config.auth.mode);
  const securityMode = text(config.security.mode);

  const devOptOuts: string[] = [];
  const warnings: string[] = [];

  if (authMode === "dev_token") {
    devOptOuts.push("auth.mode=dev_token");
    warnings.push("dev_token auth on a non-loopback bind is refused at startup");
  }

  if (securityMode === "dev") {
    devOptOuts.push("security.mode=dev");
    warnings.push("security.mode=dev strips HSTS/CSP/X-Frame-Options");
    // Only weakening where it actually unlocks the relaxed header set.
    if (config.security.dev_mode_acknowledged === true) {
      devOptOuts.push("security.dev_mode_acknowledged");
    }
  }

  if (text(config.auth.webhook_idp_on_failure, "deny") === "viewer") {
    devOptOuts.push("auth.webhook_idp_on_failure=viewer");
    warnings.push("anonymous-viewer IDP fallback is enabled (webhook_idp_on_failure=viewer)");
  }

  const identityProvider = text(config.auth.identity_provider, "local");
  const requireSignedResponse = config.auth.webhook_idp_require_signed_response !== false;
  if (identityProvider === "webhook" && !requireSignedResponse) {
    devOptOuts.push("auth.webhook_idp_require_signed_response=false");
    warnings.push("webhook IdP responses are not signature-verified — a forged response can mint a principal");
  }

  // The always-on per-instance replay cache blocks verbatim replay within one
  // process but not across nodes; the nonce binding closes that gap.
  const requireResponseNonce = config.auth.webhook_idp_require_response_nonce === true;
  if (identityProvider === "webhook" && !requireResponseNonce) {
    warnings.push(
      "webhook IdP response replay is blocked only by a per-instance cache (not shared across nodes) — " +
        "enable auth.webhook_idp_require_response_nonce for HA / strict request-binding",
    );
  }

  if (authMode === "header" && config.auth.header_mode_acknowledged === true) {
    devOptOuts.push("auth.header_mode_acknowledged");
    warnings.push("header auth trusts X-Uterm-Role headers from callers");
  }

  if (config.auth.allow_adhoc_browser_observers === true) {
    devOptOuts.push("auth.allow_adhoc_browser_observers");
    warnings.push("non-admins may observe unregistered (ad-hoc) workers");
  }

  if (config.security.block_private_connector_targets !== true) {
    devOptOuts.push("security.block_private_connector_targets=false (connectors may reach internal hosts)");
  }

  const auditChainEnabled = config.audit?.chain_enabled === true;
  if (!auditChainEnabled) {
    // A compliance note rather than an opt-out: it does not relax an existing
    // control, it is the absence of a stronger one.
    warnings.push("audit log is not tamper-evident (audit.chain_enabled=false) — enable the WORM chain for compliance");
  }

  devOptOuts.sort();

  return {
    environment: String(config.environment),
    bind_host: bindHost,
    is_loopback: isLoopback,
    auth_mode: authMode,
    dev_opt_outs: devOptOuts,
    idp_signing_required:
      config.auth.webhook_idp_require_signed_response === undefined
        ? null
        : config.auth.webhook_idp_require_signed_response === true,
    // Unconditional: the per-instance cache is always on.
    idp_response_replay_protected: true,
    audit_chain_enabled: auditChainEnabled,
    warnings,
    // Production *and* (loopback or nothing relaxed). A weakening opt-out is
    // only dangerous where the listener is remotely reachable, so a
    // loopback-only relaxation does not demote the posture.
    secure: config.environment === "production" && (isLoopback || devOptOuts.length === 0),
  };
}
