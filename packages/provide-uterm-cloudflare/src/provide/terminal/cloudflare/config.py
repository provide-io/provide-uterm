from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class JwtConfig:
    mode: str = "jwt"
    issuer: str | None = None
    audience: str | None = None
    algorithms: tuple[str, ...] = ("RS256",)
    public_key_pem: str | None = None
    jwks_url: str | None = None
    clock_skew_seconds: int = 30
    allow_query_token: bool = False
    # Parity with provide-uterm AuthConfig: configurable claim keys so that
    # IdP-specific tokens (Auth0, Okta, Azure AD) work without token transforms.
    jwt_roles_claim: str = "roles"
    jwt_scopes_claim: str = "scope"
    # Role to assign when the JWT contains no roles/scope claims.
    # Useful for Cloudflare Access JWTs which don't include roles by default.
    # Set JWT_DEFAULT_ROLE=operator to grant all CF Access users operator access.
    jwt_default_role: str = "viewer"
    # Optional mapping from group/claim values → terminal roles (admin/operator/viewer).
    # Set JWT_ROLE_MAP to a JSON object: e.g. '{"engineering":"admin","ops":"operator"}'.
    # When set with JWT_ROLES_CLAIM=groups, arbitrary CF Access group names map to roles.
    jwt_role_map: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class LimitsConfig:
    max_ws_message_bytes: int = 1_048_576
    max_input_chars: int = 10_000
    max_events_per_worker: int = 2_000
    max_buffer_bytes: int = 1_048_576


@dataclass(slots=True)
class UpstreamConfig:
    base_ws_url: str = ""
    connect_timeout_ms: int = 3_000
    heartbeat_s: int = 25
    max_backoff_s: int = 5


@dataclass(slots=True)
class CloudflareConfig:
    environment: str = "development"
    log_level: str = "info"
    durable_object_class: str = "SessionRuntime"
    jwt: JwtConfig = field(default_factory=JwtConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    worker_bearer_token: str | None = None
    tunnel_token_ttl_s: int = 3600
    tunnel_token_transport: str = "both"  # noqa: S105 — "query", "cookie", or "both"
    tunnel_ip_binding: bool = False
    security_mode: str = "strict"
    security_csp: str | None = None
    security_hsts: str | None = None
    security_x_frame_options: str | None = None
    security_x_content_type_options: str | None = None
    security_referrer_policy: str | None = None
    security_permissions_policy: str | None = None
    deckmux_auto_transfer_idle_s: int = 30
    deckmux_keystroke_queue: str = "display"

    @classmethod
    def from_env(cls, env: Any) -> CloudflareConfig:
        vars_mapping = getattr(env, "vars", None)
        source: Any = vars_mapping if vars_mapping is not None else env

        def _get(name: str, default: str = "") -> str:
            val = getattr(source, name, None)
            if val is None and isinstance(source, dict):
                val = source.get(name)
            if val is None:
                return default
            return str(val)

        def _get_bool(name: str, default: bool) -> bool:
            raw = _get(name, "1" if default else "0").strip().lower()
            return raw in {"1", "true", "yes", "y", "on"}

        def _get_optional(name: str) -> str | None:
            """Return the env var value if set (including empty string), else None."""
            val = getattr(source, name, None)
            if val is None and isinstance(source, dict):
                val = source.get(name)
            if val is None:
                return None
            return str(val)

        environment = _get("ENVIRONMENT", "development")
        env_lower = environment.strip().lower()
        is_production = env_lower in {"production", "prod"}
        algorithms_raw = _get("JWT_ALGORITHMS", "RS256")
        algorithms = tuple(part.strip() for part in algorithms_raw.split(",") if part.strip())
        mode = _get("AUTH_MODE", "jwt").strip().lower() or "jwt"
        if mode not in {"jwt", "dev", "none"}:
            mode = "jwt"
        if is_production and mode in {"dev", "none"}:
            raise ValueError("AUTH_MODE must be 'jwt' in production environments")
        limits = LimitsConfig(
            max_ws_message_bytes=max(1024, int(_get("MAX_WS_MESSAGE_BYTES", "1048576"))),
            max_input_chars=max(100, int(_get("MAX_INPUT_CHARS", "10000"))),
            max_events_per_worker=max(100, int(_get("MAX_EVENTS_PER_WORKER", "2000"))),
            max_buffer_bytes=max(1024, int(_get("MAX_BUFFER_BYTES", "1048576"))),
        )
        upstream = UpstreamConfig(
            base_ws_url=_get("UPSTREAM_BASE_WS_URL", ""),
            connect_timeout_ms=max(100, int(_get("UPSTREAM_CONNECT_TIMEOUT_MS", "3000"))),
            heartbeat_s=max(1, int(_get("UPSTREAM_HEARTBEAT_S", "25"))),
            max_backoff_s=max(1, int(_get("UPSTREAM_MAX_BACKOFF_S", "5"))),
        )
        jwt_role_map: dict[str, str] = {}
        role_map_raw = _get("JWT_ROLE_MAP", "").strip()
        if role_map_raw:
            import json as _json
            import logging as _logging

            _log = _logging.getLogger(__name__)
            try:
                parsed = _json.loads(role_map_raw)
                if isinstance(parsed, dict):
                    jwt_role_map = {str(k): str(v) for k, v in parsed.items()}
                else:
                    _log.warning("JWT_ROLE_MAP is valid JSON but not an object — ignored: %s", type(parsed).__name__)
            except (ValueError, TypeError):
                _log.warning("JWT_ROLE_MAP contains invalid JSON — ignored: %r", role_map_raw[:200])
        jwt = JwtConfig(
            mode=mode,
            issuer=_get("JWT_ISSUER") or None,
            audience=_get("JWT_AUDIENCE") or None,
            algorithms=algorithms or ("RS256",),
            public_key_pem=_get("JWT_PUBLIC_KEY_PEM") or None,
            jwks_url=_get("JWT_JWKS_URL") or None,
            clock_skew_seconds=max(0, int(_get("JWT_CLOCK_SKEW_SECONDS", "30"))),
            allow_query_token=_get_bool("AUTH_ALLOW_QUERY_TOKEN", default=not is_production),
            jwt_roles_claim=_get("JWT_ROLES_CLAIM", "roles") or "roles",
            jwt_scopes_claim=_get("JWT_SCOPES_CLAIM", "scope") or "scope",
            jwt_default_role=_get("JWT_DEFAULT_ROLE", "viewer") or "viewer",
            jwt_role_map=jwt_role_map,
        )
        worker_bearer_token = _get("WORKER_BEARER_TOKEN") or None
        if mode == "jwt" and not worker_bearer_token:
            raise ValueError("WORKER_BEARER_TOKEN is required when AUTH_MODE='jwt'")
        security_mode = _get("SECURITY_MODE", "strict").strip().lower() or "strict"
        if security_mode not in {"strict", "dev"}:
            security_mode = "strict"
        return cls(
            environment=environment,
            log_level=_get("LOG_LEVEL", "info"),
            durable_object_class=_get("DO_CLASS_NAME", "SessionRuntime"),
            jwt=jwt,
            limits=limits,
            upstream=upstream,
            worker_bearer_token=worker_bearer_token,
            tunnel_token_ttl_s=max(60, int(_get("TUNNEL_TOKEN_TTL_S", "3600"))),
            tunnel_token_transport=_get("TUNNEL_TOKEN_TRANSPORT", "both") or "both",
            tunnel_ip_binding=_get_bool("TUNNEL_IP_BINDING", default=False),
            security_mode=security_mode,
            security_csp=_get_optional("SECURITY_CSP"),
            security_hsts=_get_optional("SECURITY_HSTS"),
            security_x_frame_options=_get_optional("SECURITY_X_FRAME_OPTIONS"),
            security_x_content_type_options=_get_optional("SECURITY_X_CONTENT_TYPE_OPTIONS"),
            security_referrer_policy=_get_optional("SECURITY_REFERRER_POLICY"),
            security_permissions_policy=_get_optional("SECURITY_PERMISSIONS_POLICY"),
            deckmux_auto_transfer_idle_s=max(1, int(_get("DECKMUX_AUTO_TRANSFER_IDLE_S", "30"))),
            deckmux_keystroke_queue=_get("DECKMUX_KEYSTROKE_QUEUE", "display") or "display",
        )
