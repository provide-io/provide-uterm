from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_HMAC_ALGS = frozenset({"HS256", "HS384", "HS512"})

# Minimum length for the worker bearer token. 32 chars ≈ ≥128 bits of entropy
# across the common encodings (raw bytes / hex / base64). CF is a separate
# package and must not import from provide-uterm-server, so this mirrors the
# FastAPI backend's _MIN_BEARER_TOKEN_CHARS locally.
_MIN_BEARER_TOKEN_CHARS = 32

# Known placeholder bearer tokens (exact match, lowercased). The conservative
# subset of the server's _PLACEHOLDER_AUTH_VALUES — short, generic words an
# operator might leave in by mistake.
_PLACEHOLDER_BEARER_VALUES = frozenset(
    {
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
    }
)

# Compound placeholder phrases (substring match). Kept compound to avoid false
# positives on legitimate high-entropy tokens that merely contain "token" etc.
_PLACEHOLDER_BEARER_MARKERS = (
    "change-me",
    "changeme",
    "placeholder",
    "replace-me",
    "replace-with",
    "replace_with",
)


def _reject_weak_bearer_token(value: str) -> None:
    """Fail closed on a placeholder or low-entropy worker bearer token.

    Applied UNCONDITIONALLY whenever WORKER_BEARER_TOKEN is set: a CF Worker is
    always internet-facing (no loopback concept), so the edge token must always
    clear the entropy/placeholder floor — stronger than the FastAPI backend's
    production-like gate, which is correct for the more-exposed edge.
    """
    text = str(value).strip()
    lowered = text.lower()
    if lowered in _PLACEHOLDER_BEARER_VALUES or any(m in lowered for m in _PLACEHOLDER_BEARER_MARKERS):
        raise ValueError(
            "WORKER_BEARER_TOKEN uses a known placeholder value. Set a high-entropy runtime "
            "token (e.g. `python -c 'import secrets; print(secrets.token_urlsafe(32))'`)."
        )
    if len(text) < _MIN_BEARER_TOKEN_CHARS:
        raise ValueError(
            f"WORKER_BEARER_TOKEN must be at least {_MIN_BEARER_TOKEN_CHARS} characters of "
            "high-entropy material. CF Workers are always internet-facing, so the worker "
            "bearer token is an edge auth boundary — use a long random token."
        )


def _looks_like_asymmetric_key(pem: str | None) -> bool:
    """Return True if *pem* carries an asymmetric public-key/certificate PEM marker.

    A raw HMAC shared secret has no PEM header, so it is not treated as an
    asymmetric key — only genuine PUBLIC KEY / CERTIFICATE blocks count.
    """
    if not pem:
        return False
    return "PUBLIC KEY" in pem or "BEGIN CERTIFICATE" in pem


def _reject_jwt_algorithm_confusion(
    algorithms: tuple[str, ...], public_key_pem: str | None, jwks_url: str | None
) -> None:
    """Fail closed on JWT algorithm-confusion configurations.

    If an HMAC algorithm (HS*) is configured together with an asymmetric
    algorithm (RS*/ES*/PS*), a JWKS URL, or a PEM that is an asymmetric public
    key, an attacker can forge an HS* token using the public key bytes as the
    HMAC secret. Reject such configs loudly at startup.
    """
    has_hmac = any(a in _HMAC_ALGS for a in algorithms)
    if not has_hmac:
        return
    has_asym_alg = any(a not in _HMAC_ALGS for a in algorithms)
    has_asym_key = bool(jwks_url) or _looks_like_asymmetric_key(public_key_pem)
    if has_asym_alg or has_asym_key:
        raise ValueError(
            "JWT_ALGORITHMS must not combine HMAC (HS*) with asymmetric algorithms "
            "or an asymmetric public key / JWKS URL (algorithm-confusion risk)"
        )


@dataclass(slots=True)
class JwtConfig:
    mode: str = "jwt"
    issuer: str | None = None
    audience: str | None = None
    algorithms: tuple[str, ...] = ("RS256",)
    public_key_pem: str | None = None
    jwks_url: str | None = None
    clock_skew_seconds: int = 30
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
    # Opt-in: grant the ``admin`` role to CF Access service-token JWTs (tokens that
    # carry a ``common_name`` claim and no human ``email`` claim). Defaults to False
    # so a service-token-shaped JWT is never silently elevated to admin; set
    # JWT_SERVICE_TOKEN_ADMIN=1 only when service tokens are trusted automation.
    jwt_service_token_admin: bool = False


@dataclass(slots=True)
class LimitsConfig:
    max_ws_message_bytes: int = 1_048_576
    max_input_chars: int = 10_000
    max_events_per_worker: int = 2_000
    max_buffer_bytes: int = 1_048_576
    # Tier-A backpressure (see docs/ard-cloudflare-backpressure.md). When a
    # browser's un-ACKed inflight bytes exceed the high-water mark the producer is
    # paused (XOFF); it resumes once inflight falls below the low-water mark. A
    # browser silent for longer than the grace window is excluded from the
    # decision so a stuck client cannot pause the producer forever.
    backpressure_high_water_bytes: int = 4_194_304
    backpressure_low_water_bytes: int = 1_048_576
    backpressure_ack_grace_s: float = 10.0


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
    tunnel_token_transport: str = "cookie"  # noqa: S105 — legacy env knob; tunnel auth is cookie-only
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
        algorithms_raw = _get("JWT_ALGORITHMS", "RS256")
        algorithms = tuple(part.strip() for part in algorithms_raw.split(",") if part.strip())
        mode = _get("AUTH_MODE", "jwt").strip().lower() or "jwt"
        # dev/none modes are removed: the worker is always internet-facing, so an
        # open-access mode is an admin bypass regardless of ENVIRONMENT. Matches the
        # FastAPI server, which dropped server-side dev/none modes.
        if mode != "jwt":
            raise ValueError(
                "AUTH_MODE must be 'jwt' (dev/none modes are removed; the worker is always internet-facing)"
            )
        limits = LimitsConfig(
            max_ws_message_bytes=max(1024, int(_get("MAX_WS_MESSAGE_BYTES", "1048576"))),
            max_input_chars=max(100, int(_get("MAX_INPUT_CHARS", "10000"))),
            max_events_per_worker=max(100, int(_get("MAX_EVENTS_PER_WORKER", "2000"))),
            max_buffer_bytes=max(1024, int(_get("MAX_BUFFER_BYTES", "1048576"))),
            backpressure_high_water_bytes=max(1024, int(_get("BACKPRESSURE_HIGH_WATER_BYTES", "4194304"))),
            backpressure_low_water_bytes=max(0, int(_get("BACKPRESSURE_LOW_WATER_BYTES", "1048576"))),
            backpressure_ack_grace_s=max(0.0, float(_get("BACKPRESSURE_ACK_GRACE_S", "10"))),
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
            jwt_roles_claim=_get("JWT_ROLES_CLAIM", "roles") or "roles",
            jwt_scopes_claim=_get("JWT_SCOPES_CLAIM", "scope") or "scope",
            jwt_default_role=_get("JWT_DEFAULT_ROLE", "viewer") or "viewer",
            jwt_role_map=jwt_role_map,
            jwt_service_token_admin=_get_bool("JWT_SERVICE_TOKEN_ADMIN", default=False),
        )
        _reject_jwt_algorithm_confusion(jwt.algorithms, jwt.public_key_pem, jwt.jwks_url)
        worker_bearer_token = _get("WORKER_BEARER_TOKEN") or None
        if mode == "jwt" and not worker_bearer_token:
            raise ValueError("WORKER_BEARER_TOKEN is required when AUTH_MODE='jwt'")
        # A CF Worker is always internet-facing, so the worker bearer token is an
        # edge auth boundary that must always clear the entropy/placeholder floor.
        # The presence check above already guarantees a token in jwt mode (the
        # only mode reachable here), so the falsy branch is unreachable.
        if worker_bearer_token:  # pragma: no branch
            _reject_weak_bearer_token(worker_bearer_token)
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
            tunnel_token_transport=_get("TUNNEL_TOKEN_TRANSPORT", "cookie") or "cookie",
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
