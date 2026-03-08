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
    allow_query_token: bool = True


@dataclass(slots=True)
class LimitsConfig:
    max_ws_message_bytes: int = 1_048_576
    max_input_chars: int = 10_000
    max_events_per_worker: int = 2_000


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
        )
        upstream = UpstreamConfig(
            base_ws_url=_get("UPSTREAM_BASE_WS_URL", ""),
            connect_timeout_ms=max(100, int(_get("UPSTREAM_CONNECT_TIMEOUT_MS", "3000"))),
            heartbeat_s=max(1, int(_get("UPSTREAM_HEARTBEAT_S", "25"))),
            max_backoff_s=max(1, int(_get("UPSTREAM_MAX_BACKOFF_S", "5"))),
        )
        jwt = JwtConfig(
            mode=mode,
            issuer=_get("JWT_ISSUER") or None,
            audience=_get("JWT_AUDIENCE") or None,
            algorithms=algorithms or ("RS256",),
            public_key_pem=_get("JWT_PUBLIC_KEY_PEM") or None,
            jwks_url=_get("JWT_JWKS_URL") or None,
            clock_skew_seconds=max(0, int(_get("JWT_CLOCK_SKEW_SECONDS", "30"))),
            allow_query_token=_get_bool("AUTH_ALLOW_QUERY_TOKEN", default=not is_production),
        )
        return cls(
            environment=environment,
            log_level=_get("LOG_LEVEL", "info"),
            durable_object_class=_get("DO_CLASS_NAME", "SessionRuntime"),
            jwt=jwt,
            limits=limits,
            upstream=upstream,
        )
