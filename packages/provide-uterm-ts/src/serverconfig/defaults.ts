//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The server configuration's defaults and its field names.
 *
 * Port of the model definitions in `provide.uterm.server.config_schema`.
 * Every default here is the safe side of its choice: an identity the webhook
 * could not verify is denied rather than downgraded to a viewer, a response
 * must be signed, and an observer of an unregistered session is refused.
 */

/**
 * Every field an auth section may carry.
 *
 * Named rather than inferred, because the point of the list is to refuse a
 * field that is *not* on it: a typo is otherwise a setting that silently does
 * nothing, including a security setting the operator believes is on.
 */
export const AUTH_FIELDS: ReadonlySet<string> = new Set([
  "allow_adhoc_browser_observers",
  "api_keys_enabled",
  "clock_skew_seconds",
  "delegate_roles",
  "header_mode_acknowledged",
  "identity_provider",
  "jwt_algorithms",
  "jwt_audience",
  "jwt_issuer",
  "jwt_jwks_url",
  "jwt_public_key_pem",
  "jwt_roles_claim",
  "jwt_scopes_claim",
  "jwt_tenant_claim",
  "mode",
  "principal_cookie",
  "principal_header",
  "require_jwt_in_production",
  "require_upstream_proxy_secret",
  "role_cookie",
  "role_header",
  "surface_cookie",
  "tenant_cookie",
  "tenant_header",
  "token_cookie",
  "trusted_proxy_ips",
  "upstream_proxy_secret",
  "webhook_idp_forward_cookies",
  "webhook_idp_forward_headers",
  "webhook_idp_on_failure",
  "webhook_idp_require_response_nonce",
  "webhook_idp_require_signed_response",
  "webhook_idp_secret",
  "webhook_idp_timeout_s",
  "webhook_idp_url",
  "worker_bearer_token",
]);

/** The auth defaults. */
export const AUTH_DEFAULTS = {
  mode: "jwt",
  identityProvider: "local",
  /** Deny, not viewer: a webhook that failed has not identified anybody. */
  webhookIdpOnFailure: "deny",
  /** A response nobody signed could have come from the network. */
  webhookIdpRequireSignedResponse: true,
  webhookIdpRequireResponseNonce: false,
  delegateRoles: true,
  clockSkewSeconds: 15,
  jwtAlgorithms: ["HS256"] as readonly string[],
  trustedProxyIps: [] as readonly string[],
  /** An unregistered session has no definition to authorise against. */
  allowAdhocBrowserObservers: false,
} as const;

/** Where the UI mounts. */
export const UI_DEFAULTS = {
  appPath: "/app",
  assetsPath: "/_terminal",
} as const;
