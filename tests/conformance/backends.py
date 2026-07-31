#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Backend adapters for the conformance suite.

Each adapter exposes one normalized surface (`ConformanceBackend`) over the
real in-process API of one backend — the FastAPI server and the Cloudflare
worker — so the parity tests can drive both identically. No network: the
FastAPI side uses its in-process auth/lease/hub objects; the Cloudflare side
uses ``decode_jwt`` + ``HijackCoordinator`` + the SQLite-backed state store
that CF tests use.

The adapters are deliberately thin: they translate to whatever each backend
already does. If a parity test fails, that is a real behavioural divergence to
fix in the owning package — not here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

# A 32-byte+ HMAC secret shared by both backends for HS256 round-trips.
HS256_SECRET = "uterm-test-secret-32-byte-minimum-key"  # pragma: allowlist secret
# A throwaway RSA public key (PEM) used only to exercise the algorithm-confusion
# guard — never used to verify a token.
DUMMY_RSA_PUBLIC_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAKt6Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2\n"
    "Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2Q2cCAwEAAQ==\n"
    "-----END PUBLIC KEY-----\n"
)


@dataclass(frozen=True)
class AuthOutcome:
    """Normalized result of an auth decode across backends."""

    ok: bool
    subject_id: str | None
    roles: tuple[str, ...]


class ConformanceBackend(Protocol):
    """The common surface both backends must satisfy identically."""

    name: str

    # -- Auth -----------------------------------------------------------------
    async def decode(
        self,
        token: str,
        *,
        key: str,
        algorithms: tuple[str, ...],
        issuer: str | None = None,
        audience: str | None = None,
    ) -> AuthOutcome:
        """Decode/verify *token*; AuthOutcome(ok=False, ...) if rejected."""
        ...

    def auth_config_rejects(
        self,
        *,
        mode: str | None = None,
        algorithms: tuple[str, ...] | None = None,
        with_public_key: bool = False,
    ) -> bool:
        """Return True if building the auth config with these params raises.

        Used to assert both backends reject dev/none modes and HMAC+asymmetric
        algorithm-confusion configs at construction time.
        """
        ...

    # -- Hijack lease ---------------------------------------------------------
    def acquire_lease(self, worker_id: str, owner: str, ttl_s: int, *, now: float) -> bool: ...

    def lease_active(self, worker_id: str, *, now: float) -> bool: ...

    def release_lease(self, worker_id: str) -> None: ...

    # -- Events ---------------------------------------------------------------
    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any]) -> int:
        """Append an event; return its assigned sequence number."""
        ...

    async def list_events(self, worker_id: str) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Cloudflare backend
# ---------------------------------------------------------------------------


class CloudflareBackend:
    name = "cloudflare"

    def __init__(self) -> None:
        from provide.uterm.cloudflare.state.store import SqliteStateStore

        self._conn = sqlite3.connect(":memory:")
        # sqlite3 Cursor.execute satisfies the store's SqlExecutor at runtime
        # (this is how the CF tests build the store); the stdlib type is narrower.
        self._store = SqliteStateStore(self._conn.execute)  # type: ignore[arg-type]
        self._store.migrate()
        self._coordinators: dict[str, Any] = {}

    # -- Auth --
    async def decode(
        self,
        token: str,
        *,
        key: str,
        algorithms: tuple[str, ...],
        issuer: str | None = None,
        audience: str | None = None,
    ) -> AuthOutcome:
        from provide.uterm.cloudflare.auth.jwt import JwtValidationError, decode_jwt
        from provide.uterm.cloudflare.config import JwtConfig

        cfg = JwtConfig(
            mode="jwt",
            public_key_pem=key,
            algorithms=tuple(algorithms),
            issuer=issuer,
            audience=audience,
        )
        try:
            principal = await decode_jwt(token, cfg)
        except JwtValidationError:
            return AuthOutcome(ok=False, subject_id=None, roles=())
        return AuthOutcome(ok=True, subject_id=principal.subject_id, roles=tuple(principal.roles))

    def auth_config_rejects(
        self,
        *,
        mode: str | None = None,
        algorithms: tuple[str, ...] | None = None,
        with_public_key: bool = False,
    ) -> bool:
        from provide.uterm.cloudflare.config import CloudflareConfig

        env: dict[str, str] = {
            "AUTH_MODE": mode or "jwt",
            "JWT_ALGORITHMS": ",".join(algorithms) if algorithms else "RS256",
            "WORKER_BEARER_TOKEN": "x" * 32,
        }
        if with_public_key:
            env["JWT_PUBLIC_KEY_PEM"] = DUMMY_RSA_PUBLIC_PEM
        try:
            CloudflareConfig.from_env(_AttrEnv(env))
        except Exception:
            return True
        return False

    # -- Lease --
    def _coord(self, worker_id: str) -> Any:
        from provide.uterm.bridge.coordinator import HijackCoordinator

        return self._coordinators.setdefault(worker_id, HijackCoordinator())

    def acquire_lease(self, worker_id: str, owner: str, ttl_s: int, *, now: float) -> bool:
        return bool(self._coord(worker_id).acquire(owner, ttl_s, now=now).ok)

    def lease_active(self, worker_id: str, *, now: float) -> bool:
        return self._coord(worker_id)._active_session(now) is not None

    def release_lease(self, worker_id: str) -> None:
        coord = self._coord(worker_id)
        session = coord._session
        if session is not None:
            coord.release(session.hijack_id)

    # -- Events --
    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any]) -> int:
        return int(self._store.append_event(worker_id, event_type, data)["seq"])

    async def list_events(self, worker_id: str) -> list[dict[str, Any]]:
        return list(self._store.list_events_since(worker_id, 0, limit=1000))


# ---------------------------------------------------------------------------
# FastAPI backend
# ---------------------------------------------------------------------------


class FastApiBackend:
    name = "fastapi"

    def __init__(self) -> None:
        from provide.uterm.server.bridge.hub import TermHub

        self._coordinators: dict[str, Any] = {}
        self._hub = TermHub()

    # -- Auth --
    async def decode(
        self,
        token: str,
        *,
        key: str,
        algorithms: tuple[str, ...],
        issuer: str | None = None,
        audience: str | None = None,
    ) -> AuthOutcome:
        from provide.uterm.server.auth import LocalIdentityProvider
        from provide.uterm.server.models import AuthConfig

        kwargs: dict[str, Any] = {
            "mode": "jwt",
            "jwt_public_key_pem": key,
            "jwt_algorithms": list(algorithms),
            "worker_bearer_token": "w" * 32,
        }
        # AuthConfig validates iss/aud as strings; only set them when provided.
        if issuer is not None:
            kwargs["jwt_issuer"] = issuer
        if audience is not None:
            kwargs["jwt_audience"] = audience
        cfg = AuthConfig(**kwargs)
        idp = LocalIdentityProvider(cfg)
        try:
            principal = idp._principal_from_jwt_token(token)
        except Exception:
            return AuthOutcome(ok=False, subject_id=None, roles=())
        return AuthOutcome(ok=True, subject_id=principal.subject_id, roles=tuple(principal.roles))

    def auth_config_rejects(
        self,
        *,
        mode: str | None = None,
        algorithms: tuple[str, ...] | None = None,
        with_public_key: bool = False,
    ) -> bool:
        from provide.uterm.server.app.auth import _validate_auth_config
        from provide.uterm.server.models import AuthConfig, ServerConfig

        auth = AuthConfig(
            mode=mode or "jwt",
            jwt_algorithms=list(algorithms) if algorithms else ["RS256"],
            jwt_public_key_pem=DUMMY_RSA_PUBLIC_PEM if with_public_key else None,
            worker_bearer_token="w" * 32,
        )
        try:
            _validate_auth_config(ServerConfig(auth=auth))
        except Exception:
            return True
        return False

    # -- Lease --
    def _coord(self, worker_id: str) -> Any:
        from provide.uterm.bridge.coordinator import HijackCoordinator

        return self._coordinators.setdefault(worker_id, HijackCoordinator())

    def acquire_lease(self, worker_id: str, owner: str, ttl_s: int, *, now: float) -> bool:
        return bool(self._coord(worker_id).acquire(owner, ttl_s, now=now).ok)

    def lease_active(self, worker_id: str, *, now: float) -> bool:
        return self._coord(worker_id)._active_session(now) is not None

    def release_lease(self, worker_id: str) -> None:
        coord = self._coord(worker_id)
        session = coord._session
        if session is not None:
            coord.release(session.hijack_id)

    # -- Events --
    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any]) -> int:
        await self._hub._get(worker_id)
        evt = await self._hub.append_event(worker_id, event_type, data)
        return int(evt["seq"])

    async def list_events(self, worker_id: str) -> list[dict[str, Any]]:
        return list(await self._hub.get_recent_events(worker_id, limit=1000))


class _AttrEnv:
    """Wrap a dict as an attribute-style env for ``CloudflareConfig.from_env``."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError:
            return None
