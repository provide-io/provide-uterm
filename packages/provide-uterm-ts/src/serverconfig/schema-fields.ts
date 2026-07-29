//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * What type each configuration field is, section by section.
 *
 * Transcribed from `provide.uterm.server.config_schema` by way of the corpus,
 * which records the annotations off the models themselves — so a field that
 * changes type, gains a choice, or stops existing fails the drift test rather
 * than being quietly accepted here.
 *
 * The order is the order each model declares its fields in, and it is
 * load-bearing: a report about a document with two mistakes in it lists them
 * in this order rather than the order the document happened to write them.
 *
 * The shorthand: `"str"`, `"int"`, `"float"`, `"bool"`, `"path"`, `"dict"`, a
 * `[]` suffix for a list of them, a `?` suffix for a field that may be null,
 * `"model:Name"` for a nested section, a `!` suffix for a field with no
 * default at all, and an array of strings for a closed set of choices.
 */

export const SECTION_FIELD_SPECS = {
  AuthConfig: {
    mode: "str",
    principal_header: "str",
    role_header: "str",
    tenant_header: "str",
    principal_cookie: "str", // pragma: allowlist secret
    role_cookie: "str", // pragma: allowlist secret
    tenant_cookie: "str", // pragma: allowlist secret
    surface_cookie: "str", // pragma: allowlist secret
    token_cookie: "str", // pragma: allowlist secret
    jwt_issuer: "str",
    jwt_audience: "str",
    jwt_jwks_url: "str?",
    jwt_public_key_pem: "str?",
    jwt_algorithms: "str[]",
    clock_skew_seconds: "int",
    jwt_roles_claim: "str",
    jwt_scopes_claim: "str",
    jwt_tenant_claim: "str",
    worker_bearer_token: "str?", // pragma: allowlist secret
    api_keys_enabled: "bool", // pragma: allowlist secret
    header_mode_acknowledged: "bool",
    require_jwt_in_production: "bool",
    trusted_proxy_ips: "str[]",
    upstream_proxy_secret: "str?", // pragma: allowlist secret
    require_upstream_proxy_secret: "bool", // pragma: allowlist secret
    identity_provider: ["local", "webhook"],
    delegate_roles: "bool",
    webhook_idp_url: "str?",
    webhook_idp_secret: "str?", // pragma: allowlist secret
    webhook_idp_timeout_s: "float",
    webhook_idp_on_failure: ["deny", "viewer"],
    webhook_idp_require_signed_response: "bool",
    webhook_idp_require_response_nonce: "bool",
    webhook_idp_forward_headers: "str[]",
    webhook_idp_forward_cookies: "str[]",
    allow_adhoc_browser_observers: "bool",
  },
  AuditConfig: {
    chain_enabled: "bool",
    chain_file: "str?",
  },
  UiConfig: {
    app_path: "str",
    assets_path: "str",
    xterm_cdn: "str",
    fitaddon_cdn: "str",
    fonts_cdn: "str",
    xterm_cdn_integrity: "str",
    fitaddon_cdn_integrity: "str",
  },
  RecordingConfig: {
    enabled_by_default: "bool",
    directory: "path",
    max_bytes: "int",
    retention_s: "int",
    control_channel_mode: ["exclude", "wire"],
    redact_sensitive: "bool",
    store_type: ["local", "memory", "null", "webhook"],
    webhook_url: "str?",
    webhook_secret: "str?", // pragma: allowlist secret
    webhook_timeout_s: "float",
    flush_interval_s: "float",
    flush_batch_size: "int",
  },
  ControlPlaneConfig: {
    backend: ["memory", "sqlite"],
    database_url: "str?",
    reap_interval_s: "int",
    reap_retention_s: "int",
  },
  SecurityConfig: {
    mode: ["strict", "dev"],
    dev_mode_acknowledged: "bool",
    csp: "str?",
    hsts: "str?",
    x_frame_options: "str?",
    x_content_type_options: "str?",
    referrer_policy: "str?",
    permissions_policy: "str?",
    block_private_connector_targets: "bool",
    metrics_require_auth: "bool",
    default_session_visibility: ["public", "operator", "private"],
  },
  TunnelConfig: {
    token_ttl_s: "int", // pragma: allowlist secret
    token_transport: ["query", "cookie", "both"], // pragma: allowlist secret
    cookie_secure: "bool",
    cookie_samesite: ["lax", "strict", "none"],
    ip_binding: "bool",
  },
  WebhooksConfig: {
    allow_loopback_destinations: "bool",
  },
  ProfileStoreConfig: {
    directory: "path",
  },
  ServerBindConfig: {
    host: "str",
    port: "int",
    public_base_url: "str",
    title: "str",
    node_id: "str",
    allowed_origins: "str[]",
    max_sessions: "int?",
  },
  PamConfig: {
    notify_socket: "str?",
    mode: ["notify", "capture"],
    auto_session: "bool",
    auto_session_command: "str",
    relay_url: "str?",
    relay_token: "str?", // pragma: allowlist secret
    capture_socket_dir: "str?",
    require_peer_uids: "int[]?",
  },
  GovernanceConfig: {
    policy_webhook_url: "str?",
    policy_webhook_secret: "str?", // pragma: allowlist secret
    policy_webhook_timeout_s: "float",
    discovery_provider: "str",
    registry_webhook_url: "str?",
    registry_webhook_secret: "str?", // pragma: allowlist secret
    registry_webhook_interval_s: "float",
    authz_webhook_url: "str?",
    authz_webhook_secret: "str?", // pragma: allowlist secret
    authz_webhook_timeout_s: "float",
    behavioral_audit_url: "str?",
    behavioral_audit_secret: "str?", // pragma: allowlist secret
    behavioral_audit_interval_s: "float",
    behavioral_max_cps: "float?",
    behavioral_min_jitter: "float?",
    behavioral_fail_open: "bool",
    telemetry_webhook_url: "str?",
    telemetry_webhook_secret: "str?", // pragma: allowlist secret
    telemetry_webhook_timeout_s: "float",
    external_connectors: "str[]",
  },
  GraphicalTargetConfig: {
    target_id: "str",
    tenant_id: "str",
    protocol: "str",
    target_address: "str",
    vm_name: "str?",
    name: "str",
    description: "str?",
    enabled: "bool",
    width: "int",
    height: "int",
    is_static: "bool",
    config: "dict",
  },
  SessionDefinition: {
    session_id: "str!",
    display_name: "str",
    connector_type: "str",
    connector_config: "dict",
    input_mode: ["hijack", "open"],
    auto_start: "bool",
    tags: "str[]",
    recording_enabled: "bool?",
    created_at: "datetime",
    owner: "str?",
    visibility: ["public", "operator", "private"],
    ephemeral: "bool",
    presence: "bool",
    auto_transfer_idle_s: "int",
    keystroke_queue: ["display", "replay"],
  },
} as const;

export const TOP_LEVEL_FIELD_SPEC = {
  environment: ["dev", "production"],
  server: "model:ServerBindConfig",
  auth: "model:AuthConfig",
  control_plane: "model:ControlPlaneConfig",
  ui: "model:UiConfig",
  recording: "model:RecordingConfig",
  profiles: "model:ProfileStoreConfig",
  security: "model:SecurityConfig",
  tunnel: "model:TunnelConfig",
  webhooks: "model:WebhooksConfig",
  pam: "model:PamConfig",
  governance: "model:GovernanceConfig",
  audit: "model:AuditConfig",
  sessions: "model:SessionDefinition[]",
  graphical_targets: "model:GraphicalTargetConfig[]",
  session_idle_timeout_s: "int",
  session_retention_s: "int",
  browser_rate_limit_per_sec: "float",
  worker_frame_on_invalid: ["drop", "reject"],
  max_connections_per_principal: "int",
  max_workers: "int",
} as const;
