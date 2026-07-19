#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""API key management -- creation, validation, and storage."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field

# Tenant ids are a bounded ASCII slug: an alphanumeric first character followed
# by up to 127 of ``[A-Za-z0-9_.-]``. Shared verbatim with the C#/Go ports and
# ``server.auth`` so a tenant validates identically across every surface.
_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Raised by the tenant-scoped ``create_for_tenant`` when the supplied tenant id
# is empty or fails the tenant pattern (mirrors the C# ArgumentException).
_INVALID_TENANT_MESSAGE = "tenant_id is required and must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"


def canonical_tenant_id(tenant_id: str | None) -> str | None:
    """Return the trimmed tenant id when valid, else ``None``.

    Empty/whitespace-only input is treated as absent (``None``); a non-empty
    value is returned only when it matches the tenant pattern, otherwise
    ``None``. A non-``None`` result is a valid, canonical tenant id.
    """
    text = (tenant_id or "").strip()
    if not text:
        return None
    return text if _TENANT_PATTERN.match(text) else None


@dataclass
class ApiKey:
    """A single API key record (never stores the raw key)."""

    key_id: str  # First 16 hex chars of key hash
    key_hash: str  # SHA-256 hex digest of the full key
    name: str  # Human-readable label
    # Tenant that owns the key. Flat ``create`` leaves it empty (legacy,
    # non-tenant keys); ``create_for_tenant`` sets a validated canonical id.
    tenant_id: str = ""
    scopes: frozenset[str] = frozenset()  # Route validation should enforce non-empty role scopes
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None  # None = never expires
    last_used_at: float | None = None
    revoked: bool = False


class ApiKeyStore:
    """In-memory API key registry with timing-safe validation."""

    def __init__(self) -> None:
        self._keys: dict[str, ApiKey] = {}  # key_id -> ApiKey

    def create(
        self,
        name: str,
        *,
        scopes: frozenset[str] = frozenset(),
        expires_in_s: int | None = None,
    ) -> tuple[str, ApiKey]:
        """Create a new API key. Returns ``(raw_key, api_key_record)``.

        The raw key is returned exactly once; it is never stored.
        """
        raw_key = secrets.token_urlsafe(32)
        key_hash = _hash_key(raw_key)
        key_id = key_hash[:16]
        expires_at = time.time() + expires_in_s if expires_in_s else None
        record = ApiKey(
            key_id=key_id,
            key_hash=key_hash,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
        )
        self._keys[key_id] = record
        return raw_key, record

    def create_for_tenant(
        self,
        tenant_id: str,
        name: str,
        *,
        scopes: frozenset[str] = frozenset(),
        expires_in_s: int | None = None,
    ) -> tuple[str, ApiKey]:
        """Create a key bound to a validated canonical ``tenant_id``.

        Returns ``(raw_key, api_key_record)`` exactly like :meth:`create`. An
        empty or malformed tenant id raises ``ValueError`` (mirrors the C#
        ``ArgumentException``).
        """
        tenant = canonical_tenant_id(tenant_id)
        if tenant is None:
            raise ValueError(_INVALID_TENANT_MESSAGE)
        raw_key, record = self.create(name, scopes=scopes, expires_in_s=expires_in_s)
        record.tenant_id = tenant
        return raw_key, record

    def validate(self, raw_key: str) -> ApiKey | None:
        """Validate a raw API key. Returns the key record or ``None``."""
        key_hash = _hash_key(raw_key)
        for record in self._keys.values():
            if record.revoked:
                continue
            if record.expires_at is not None and time.time() > record.expires_at:
                continue
            if secrets.compare_digest(record.key_hash, key_hash):
                record.last_used_at = time.time()
                return record
        return None

    def revoke(self, key_id: str) -> bool:
        """Revoke a key by ID. Returns ``True`` if found."""
        if key_id in self._keys:
            self._keys[key_id].revoked = True
            return True
        return False

    def list_keys(self) -> list[ApiKey]:
        """List all keys (never exposes the raw key or full hash)."""
        return list(self._keys.values())

    def list_keys_for_tenant(self, tenant_id: str) -> list[ApiKey]:
        """List the non-revoked keys owned by ``tenant_id``.

        An empty or invalid tenant id yields an empty list.
        """
        tenant = canonical_tenant_id(tenant_id)
        if tenant is None:
            return []
        return [record for record in self._keys.values() if not record.revoked and record.tenant_id == tenant]

    def revoke_for_tenant(self, key_id: str, tenant_id: str) -> bool:
        """Revoke ``key_id`` only when it belongs to ``tenant_id``.

        Returns ``False`` for an invalid tenant, an unknown key, or a key owned
        by a different tenant.
        """
        tenant = canonical_tenant_id(tenant_id)
        if tenant is None:
            return False
        record = self._keys.get(key_id)
        if record is None or record.tenant_id != tenant:
            return False
        record.revoked = True
        return True


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()
