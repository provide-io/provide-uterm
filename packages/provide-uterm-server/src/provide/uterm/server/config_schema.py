#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Canonical configuration schema for the hosted terminal server application.

Mutation-enforced at killed==100 (see [tool.mutmut].source_paths). mutmut only
mutates this module's two undecorated module-level helpers (``_clean_path`` and the
``_require_secure_url`` SSRF guard) — it skips every ``@model_validator`` /
``@field_validator`` / ``@classmethod`` by design — so those two functions carry the
behaviour-pinning kill-tests in ``tests/server/test_models_mutation_killing.py``.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from provide.uterm.bridge.contracts import Visibility  # noqa: TC001 — Pydantic needs it at runtime
from provide.uterm.defaults import TerminalDefaults
from provide.uterm.server.bridge.ratelimit import MIN_RATE_PER_SEC

# CDN URLs for xterm.js and fonts loaded into the operator dashboard HTML.
XTERM_CDN_DEFAULT = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
FITADDON_CDN_DEFAULT = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
FONTS_CDN_DEFAULT = "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap"
SERVER_BUILTIN_CONNECTOR_TYPES = frozenset({"shell", "ssh", "telnet", "websocket", "ushell"})


def _clean_path(value: str, fallback: str) -> str:
    text = str(value or fallback).strip()
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


class ServerBaseModel(BaseModel):
    """Base class for mutable server models with strict validation."""

    model_config = ConfigDict(extra="forbid")


class AuthConfig(ServerBaseModel):
    """Authentication and principal-bridging settings for the server app."""

    mode: str = "jwt"
    principal_header: str = "x-uterm-principal"
    role_header: str = "x-uterm-role"
    # Header/cookie/claim carrying the caller's tenant id for multi-tenancy.
    # The resolved tenant is validated against the tenant pattern and fails
    # closed (see ``server.auth``); it is never trusted from a request body.
    tenant_header: str = "x-uterm-tenant"
    principal_cookie: str = "uterm_principal"
    role_cookie: str = "uterm_role"
    tenant_cookie: str = "uterm_tenant"
    surface_cookie: str = "uterm_surface"
    token_cookie: str = "uterm_token"  # noqa: S105
    jwt_issuer: str = "provide-uterm"
    jwt_audience: str = "provide-uterm-server"
    jwt_jwks_url: str | None = None
    jwt_public_key_pem: str | None = None
    jwt_algorithms: list[str] = Field(default_factory=lambda: ["HS256"])
    clock_skew_seconds: int = 15
    jwt_roles_claim: str = "roles"
    jwt_scopes_claim: str = "scope"
    jwt_tenant_claim: str = "tenant_id"
    worker_bearer_token: str | None = None
    api_keys_enabled: bool = False
    header_mode_acknowledged: bool = False
    require_jwt_in_production: bool = False
    # Finding #4: when auth.mode='header', the server trusts X-Uterm-Role from
    # any caller.  When ``trusted_proxy_ips`` is non-empty, header-mode auth
    # is only honoured for connections whose source IP appears in the list;
    # other callers are downgraded to anonymous.  Required for non-loopback
    # binds (the startup validator rejects header mode on non-loopback hosts
    # unless this list is set).
    trusted_proxy_ips: list[str] = Field(default_factory=list)

    # Secret used to sign/verify identity control frames from upstream proxies.
    upstream_proxy_secret: str | None = None
    require_upstream_proxy_secret: bool = False

    identity_provider: Literal["local", "webhook"] = "local"
    delegate_roles: bool = True
    webhook_idp_url: str | None = None
    webhook_idp_secret: str | None = None
    webhook_idp_timeout_s: float = 2.0
    # Finding #7: webhook IdP failure mode.  Before this field, *any* exception
    # from the webhook (HTTP error, timeout, JSON parse failure, network down)
    # produced a synthetic ``viewer`` principal — the request silently passed
    # auth as an anonymous-equivalent viewer.  Default ``deny`` returns None
    # so the request fails authn (411).  ``viewer`` restores the old fail-open
    # behaviour for callers who explicitly want it.
    webhook_idp_on_failure: Literal["deny", "viewer"] = "deny"
    # 1f: require the webhook IdP to sign its RESPONSE (secure-by-default). When
    # True the provider verifies X-Uterm-Signature over the raw response bytes
    # so a MITM/compromised transport cannot forge a principal; needs a shared
    # secret (webhook_idp_secret). Set False to trust unsigned responses (legacy).
    webhook_idp_require_signed_response: bool = True
    # L9 layer 3: require the webhook IdP RESPONSE to echo the per-request nonce
    # (matching) — cryptographically binding the response to the request. The
    # always-on per-instance replay cache already blocks verbatim replay within
    # a single process; this flag adds the binding needed for HA / multi-node
    # deployments where the cache isn't shared. Default False keeps an IdP that
    # doesn't echo the nonce working (replay cache remains the defense).
    webhook_idp_require_response_nonce: bool = False
    # 1d: extra request headers the provider may forward to the external IdP, on
    # top of the always-forwarded auth credentials (authorization, x-api-key,
    # principal/role headers). Operator-extensible; matched case-insensitively.
    webhook_idp_forward_headers: list[str] = Field(default_factory=list)
    # 1d: extra request cookies forwarded to the external IdP, on top of the
    # always-forwarded token/principal/role cookies. Operator-extensible.
    webhook_idp_forward_cookies: list[str] = Field(default_factory=list)

    # When a worker has no registered SessionDefinition (ad-hoc), browser
    # observers are denied by default -- only a global admin may observe. Set
    # this True to restore the legacy behavior of honoring the principal's role
    # claim for unregistered workers.
    allow_adhoc_browser_observers: bool = False

    @model_validator(mode="after")
    def _validate_proxy_secret(self) -> AuthConfig:
        if self.require_upstream_proxy_secret and not str(self.upstream_proxy_secret or "").strip():
            raise ValueError("auth.upstream_proxy_secret is required when auth.require_upstream_proxy_secret=True")
        return self

    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> AuthConfig:
        _require_secure_url(self.webhook_idp_url, "auth.webhook_idp_url")
        _require_secure_url(self.jwt_jwks_url, "auth.jwt_jwks_url")
        return self

    @model_validator(mode="after")
    def _validate_webhook_idp_signing(self) -> AuthConfig:
        # 1f: verifying the IdP response signature is impossible without a shared
        # secret, so refuse the unsatisfiable combination at config-load time.
        if (
            self.identity_provider == "webhook"
            and self.webhook_idp_require_signed_response
            and not str(self.webhook_idp_secret or "").strip()
        ):
            raise ValueError(
                "requiring a signed IdP response needs auth.webhook_idp_secret; set the secret or "
                "set auth.webhook_idp_require_signed_response=false to disable verification"
            )
        return self


class AuditConfig(ServerBaseModel):
    """Tamper-evident WORM audit-chain settings (opt-in, default disabled)."""

    # Opt-in: enable the hash-chained, append-only WORM audit log sink.
    chain_enabled: bool = False
    # Append-only 0600 JSONL path the chain writes to; REQUIRED when enabled.
    chain_file: str | None = None

    @model_validator(mode="after")
    def _validate_chain_file(self) -> AuditConfig:
        # A chain with nowhere to write is a misconfiguration, not a silent no-op.
        if self.chain_enabled and not str(self.chain_file or "").strip():
            raise ValueError("audit.chain_enabled requires audit.chain_file (the append-only WORM log path)")
        return self


class UiConfig(ServerBaseModel):
    """UI mount paths for the server application."""

    app_path: str = "/app"
    assets_path: str = "/_terminal"
    xterm_cdn: str = XTERM_CDN_DEFAULT
    fitaddon_cdn: str = FITADDON_CDN_DEFAULT
    fonts_cdn: str = FONTS_CDN_DEFAULT
    xterm_cdn_integrity: str = ""
    fitaddon_cdn_integrity: str = ""

    @field_validator("app_path")
    @classmethod
    def _normalize_app_path(cls, value: str) -> str:
        return _clean_path(value, "/app")

    @field_validator("assets_path")
    @classmethod
    def _normalize_assets_path(cls, value: str) -> str:
        return _clean_path(value, "/_terminal")


class RecordingConfig(ServerBaseModel):
    """Session recording settings."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    enabled_by_default: bool = False
    directory: Path = Path(".uterm-recordings")
    max_bytes: int = 0  # 0 = unlimited
    retention_s: int = 0  # 0 = keep indefinitely
    control_channel_mode: Literal["exclude", "wire"] = "exclude"
    redact_sensitive: bool = True

    store_type: Literal["local", "memory", "null", "webhook"] = "local"
    webhook_url: str | None = None
    webhook_secret: str | None = None
    webhook_timeout_s: float = 2.0
    flush_interval_s: float = 5.0
    flush_batch_size: int = 100

    @field_validator("max_bytes")
    @classmethod
    def _validate_max_bytes(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"recording.max_bytes must be >= 0 (0 = unlimited), got: {value}")
        return value

    @field_validator("retention_s")
    @classmethod
    def _validate_retention_s(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"recording.retention_s must be >= 0 (0 = keep indefinitely), got: {value}")
        return value

    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> RecordingConfig:
        _require_secure_url(self.webhook_url, "recording.webhook_url")
        return self


class ControlPlaneConfig(ServerBaseModel):
    """Backend selection for the hosted server control plane."""

    backend: Literal["memory", "sqlite"] = "memory"
    database_url: str | None = None
    reap_interval_s: int = 3600  # how often the reaper runs (seconds)
    reap_retention_s: int = 604800  # keep soft-deleted/expired rows this long before physical delete (7 days)

    @field_validator("reap_interval_s")
    @classmethod
    def _validate_reap_interval_s(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"control_plane.reap_interval_s must be > 0, got: {value}")
        return value

    @field_validator("reap_retention_s")
    @classmethod
    def _validate_reap_retention_s(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                f"control_plane.reap_retention_s must be >= 0 (0 = reap as soon as past expiry), got: {value}"
            )
        return value

    @model_validator(mode="after")
    def _validate_database_url(self) -> ControlPlaneConfig:
        if self.backend == "sqlite" and not str(self.database_url or "").strip():
            raise ValueError("control_plane.database_url is required when control_plane.backend='sqlite'")
        return self


class SecurityConfig(ServerBaseModel):
    """Configurable security response headers."""

    mode: Literal["strict", "dev"] = "strict"
    # Escape hatch for intentionally serving the relaxed (dev) header set on a
    # non-loopback host. Mirrors AuthConfig.header_mode_acknowledged: the
    # startup validator rejects security.mode='dev' on a routable bind unless
    # this flag is set, so a dev config can't silently ship to prod.
    dev_mode_acknowledged: bool = False
    csp: str | None = None
    hsts: str | None = None
    x_frame_options: str | None = None
    x_content_type_options: str | None = None
    referrer_policy: str | None = None
    permissions_policy: str | None = None
    # When True, connector targets that resolve to private / loopback /
    # link-local / multicast / unspecified / reserved addresses are blocked.
    # Default False preserves the intended use-case: SSHing into internal
    # servers is the tool's purpose.  Enable in multi-tenant / hosted
    # deployments where tenants must not reach the operator's internal network.
    # Cloud-metadata IPs (169.254.169.254, 100.100.100.200, fd00:ec2::254) are
    # ALWAYS blocked regardless of this flag.
    block_private_connector_targets: bool = False
    # When True, /metrics and /metrics/prometheus require an authenticated
    # (non-anonymous) principal. Default False keeps them open for the usual
    # Prometheus scraping convention (protect at the network/proxy layer);
    # enable it to require auth when the endpoints are reachable from untrusted
    # networks.
    metrics_require_auth: bool = False
    # Visibility applied to a session created WITHOUT an explicit ``visibility``.
    # ``public`` (default, back-compat) means any authenticated viewer can list,
    # read, and subscribe to it; ``operator`` restricts it to the operator role
    # and ``private`` to the owner/admins. Set ``private`` for a "private unless
    # shared" posture so a low-trust viewer cannot enumerate every default
    # session. An explicit per-session ``visibility`` always overrides this.
    default_session_visibility: Visibility = "public"


class TunnelConfig(ServerBaseModel):
    """Tunnel sharing security settings."""

    token_ttl_s: int = 3600
    token_transport: Literal["query", "cookie", "both"] = "cookie"  # noqa: S105
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    ip_binding: bool = False

    @field_validator("token_ttl_s")
    @classmethod
    def _validate_ttl(cls, value: int) -> int:
        if value < 60:
            raise ValueError(f"tunnel.token_ttl_s must be >= 60, got: {value}")
        return value


class WebhooksConfig(ServerBaseModel):
    """Webhook delivery safety settings."""

    allow_loopback_destinations: bool = False


class ProfileStoreConfig(ServerBaseModel):
    """File-backed profile store settings."""

    directory: Path = Path(".uterm-profiles")


class ServerBindConfig(ServerBaseModel):
    """Bind and public URL settings."""

    host: str = TerminalDefaults.SERVER_HOST
    port: int = TerminalDefaults.SERVER_PORT
    public_base_url: str = ""
    title: str = "provide-uterm-server"
    node_id: str = "default"
    allowed_origins: list[str] = Field(default_factory=list)
    max_sessions: int | None = None

    @model_validator(mode="after")
    def _derive_public_base_url(self) -> ServerBindConfig:
        if not self.public_base_url:
            self.public_base_url = f"http://{self.host}:{self.port}"
        return self


# SessionDefinition lives in a sibling module to keep this file under 500 LOC.
# It is a pure Pydantic model (zero mutmut mutants); the mutation-bearing
# helpers (_clean_path, _require_secure_url) deliberately stay here so this
# module keeps a non-empty mutant set. The import is placed AFTER ServerBaseModel
# and SERVER_BUILTIN_CONNECTOR_TYPES (which the sibling imports back) and BEFORE
# UtermServerConfig (which references SessionDefinition). Re-exported below so
# ``from provide.uterm.server.config_schema import SessionDefinition`` resolves.
# `as SessionDefinition` marks this an explicit re-export (PEP 484) so strict
# mypy lets `from ...config_schema import SessionDefinition` resolve downstream.
from provide.uterm.server.config_schema_session import SessionDefinition as SessionDefinition  # noqa: E402


class PamConfig(ServerBaseModel):
    """PAM session bridge settings (requires provide-uterm-platform)."""

    notify_socket: str | None = None
    mode: Literal["notify", "capture"] = "notify"
    auto_session: bool = False
    auto_session_command: str = "/bin/bash"
    relay_url: str | None = None
    relay_token: str | None = None
    # Confine capture sockets to this dir (None → derive from notify_socket's parent dir)
    capture_socket_dir: str | None = None
    # Opt-in: only these peer euids may send notify events (None → don't enforce, log only)
    require_peer_uids: list[int] | None = None

    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> PamConfig:
        _require_secure_url(self.relay_url, "pam.relay_url")
        return self


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _require_secure_url(url: str | None, field_name: str) -> None:
    """Reject a cleartext ``http://`` outbound URL unless its host is loopback.

    ``https://`` is always allowed; ``http://`` is allowed only for loopback
    hosts (local dev). Any other scheme, or ``http://`` to a routable host,
    raises -- these channels carry HMAC secrets, auth headers, and the JWKS
    used to validate admin tokens, so cleartext to a remote host is unsafe.
    """
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme != "http":
        raise ValueError(f"{field_name} must use http(s)")
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return
    raise ValueError(f"{field_name} must use https:// (cleartext http:// is only allowed for loopback hosts)")


class GovernanceConfig(ServerBaseModel):
    """Configuration for external policy and telemetry hooks."""

    policy_webhook_url: str | None = None
    policy_webhook_secret: str | None = None
    policy_webhook_timeout_s: float = 2.0

    discovery_provider: str = "webhook"
    registry_webhook_url: str | None = None
    registry_webhook_secret: str | None = None
    registry_webhook_interval_s: float = 60.0

    authz_webhook_url: str | None = None
    authz_webhook_secret: str | None = None
    authz_webhook_timeout_s: float = 2.0

    behavioral_audit_url: str | None = None
    behavioral_audit_secret: str | None = None
    behavioral_audit_interval_s: float = 30.0
    behavioral_max_cps: float | None = None
    behavioral_min_jitter: float | None = None
    behavioral_fail_open: bool = False

    telemetry_webhook_url: str | None = None
    telemetry_webhook_secret: str | None = None
    telemetry_webhook_timeout_s: float = 2.0

    external_connectors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> GovernanceConfig:
        _require_secure_url(self.policy_webhook_url, "governance.policy_webhook_url")
        _require_secure_url(self.registry_webhook_url, "governance.registry_webhook_url")
        _require_secure_url(self.authz_webhook_url, "governance.authz_webhook_url")
        _require_secure_url(self.behavioral_audit_url, "governance.behavioral_audit_url")
        _require_secure_url(self.telemetry_webhook_url, "governance.telemetry_webhook_url")
        return self


class GraphicalTargetConfig(ServerBaseModel):
    """Config-file shape of a seeded graphical target (``[[graphical_targets]]``).

    Mirrors the C# ``ServerConfig.GraphicalTargetDefinition`` / Go
    ``GraphicalTargetConfig``. Seeded targets are always registered as immutable
    system/static entries at boot (``is_static`` here is informational). Note:
    ``min_role`` was intentionally dropped in the canonical — access is
    capability + tenant scope only.
    """

    target_id: str = ""
    tenant_id: str = ""
    protocol: str = "rfb"
    target_address: str = ""
    vm_name: str | None = None
    name: str = ""
    description: str | None = None
    enabled: bool = True
    width: int = 640
    height: int = 480
    is_static: bool = False
    # Generic protocol-specific parameters (``[graphical_targets.config]``),
    # e.g. the litevirt ``vm_name``. Folded into the seeded definition's config.
    config: dict[str, object] = Field(default_factory=dict)


class UtermServerConfig(ServerBaseModel):
    """Top-level application config for the standalone server."""

    # Declared deployment intent. Secure-by-default = production; drives the
    # production assertion (_validate_environment_profile) and the startup
    # security-posture self-report (compute_security_posture).
    environment: Literal["dev", "production"] = "production"
    server: ServerBindConfig = Field(default_factory=ServerBindConfig)
    auth: AuthConfig = Field(default_factory=lambda: AuthConfig(mode="dev_token"))
    control_plane: ControlPlaneConfig = Field(default_factory=ControlPlaneConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    profiles: ProfileStoreConfig = Field(default_factory=ProfileStoreConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig)
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)
    pam: PamConfig = Field(default_factory=PamConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    sessions: list[SessionDefinition] = Field(
        default_factory=lambda: [
            SessionDefinition(
                session_id="provide-shell",
                display_name="Provide Shell",
                connector_type="shell",
                input_mode="open",
                auto_start=True,
                tags=["shell", "reference"],
            )
        ]
    )
    # Graphical targets seeded as immutable system/static entries at boot.
    graphical_targets: list[GraphicalTargetConfig] = Field(default_factory=list)
    session_idle_timeout_s: int = 0
    session_retention_s: int = 0
    browser_rate_limit_per_sec: float = 300
    # Ceilings for the REST hijack API's token buckets (tokens/sec, burst =
    # one second of the same rate). Each is applied twice: once globally and
    # once per calling client, so a single client can never consume more than
    # its own share. ``acquire`` guards POST /hijack/acquire — the expensive,
    # state-changing lease grab; ``send`` is shared by the hijack send *and*
    # step endpoints, which are cheap keystroke-rate calls. Defaults are the
    # hub's built-in values, so an unset deployment is unchanged.
    rest_acquire_rate_limit_per_sec: float = 5
    rest_send_rate_limit_per_sec: float = 20
    # Finding #5d: how the worker WS recv loop handles a malformed inbound
    # control frame (one whose fields fail the frame builder's type
    # validation, e.g. snapshot ``cursor.x="abc"``).  ``drop`` (default)
    # isolates the bad frame — it is dropped, ``ws_worker_frame_invalid_total``
    # increments, and the worker session (plus every browser viewing it)
    # stays alive; one bad frame can no longer DoS the session.  ``reject``
    # sends a structured ``invalid_frame`` error frame and closes the worker
    # WS with code 1003.
    worker_frame_on_invalid: Literal["drop", "reject"] = "drop"
    # Maximum concurrent BROWSER WebSocket connections per authenticated
    # principal (identified by subject_id).  Workers and anonymous principals
    # are exempt; only concrete human principals are counted.  Prevents a
    # single authenticated user from exhausting server memory with thousands
    # of open browser tabs.
    max_connections_per_principal: int = 25
    # Generous GLOBAL cap on the number of distinct worker_ids the hub will
    # register. Unlike max_connections_per_principal (BROWSER-only), workers
    # share a static principal, so a per-principal cap would wrongly limit the
    # whole fleet. This bounds OOM from a single token holder opening thousands
    # of unique worker_id WS connections; real fleets are large, so the default
    # is high and only pathological floods hit it. Reconnects of an already-
    # registered worker_id are never counted against the cap.
    max_workers: int = 10000

    @field_validator("max_workers")
    @classmethod
    def _validate_max_workers(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"max_workers must be >= 1, got: {value}")
        return value

    @field_validator(
        "rest_acquire_rate_limit_per_sec",
        "rest_send_rate_limit_per_sec",
        # The browser ceiling shares this validator because it shares the
        # bucket: same TokenBucket, same burst-equals-rate rule, so the same
        # floor follows. It is in fact the more dangerous of the three —
        # `RateLimiter.__init__` clamps the REST rates, but the browser rate
        # reaches `TokenBucket` unclamped from `websockets_impl`, so a
        # configured 0 denied every browser message for the life of the
        # process.
        "browser_rate_limit_per_sec",
    )
    @classmethod
    def _validate_rate_limit(cls, value: float, info: ValidationInfo) -> float:
        """Refuse any rate that would not behave as the operator wrote it.

        Guards all three configured ceilings — the two REST hijack budgets and
        the browser one. They share a validator because they share a
        :class:`~provide.uterm.server.bridge.ratelimit.TokenBucket`, so the same
        burst rule and therefore the same floor applies to each.


        A rate limit is trusted once configured, so every value that
        cannot be honoured verbatim is refused rather than reinterpreted.

        **Not finite.**  ``inf`` passes every ``>=`` bound, so accepting it
        would silently mean "no limit at all" — the same fail-open that
        makes a trusted limit worse than none.  ``-inf`` and ``NaN`` go
        with it: none of the three is a rate anybody meant to write.

        **Below :data:`MIN_RATE_PER_SEC`.**  ``0`` is ambiguous — read as
        "unlimited" it disables the limit, read as "refuse everything" it
        bricks the surface it guards, and nothing in the file says which the
        operator meant.  The whole band under the floor is refused for the
        *second* of those reasons rather than for ambiguity: a token
        bucket's burst is one second of its rate, so a sub-1/s bucket
        never holds a whole token and denies every call forever.  ``0.5``
        is not "one call every two seconds", it is "never" — so it is
        refused exactly like ``0``.  Negatives go the same way, and the
        floor also keeps the limiter's own clamp from handing back a
        looser rate than was configured.

        Fractions at or above the floor are a real policy and are kept.

        The bound is written ``not value >= MIN`` rather than
        ``value < MIN`` so a NaN — which compares false against
        everything — falls into the refusal instead of sliding past a
        ``<`` test.  Do not "simplify" it.
        """
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be a finite number >= {MIN_RATE_PER_SEC}, got: {value}")
        if not value >= MIN_RATE_PER_SEC:
            raise ValueError(f"{info.field_name} must be >= {MIN_RATE_PER_SEC}, got: {value}")
        return value


SessionDefinition.model_rebuild()
UtermServerConfig.model_rebuild()
