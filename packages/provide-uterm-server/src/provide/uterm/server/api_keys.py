#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""API key management -- creation, validation, and storage."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field


@dataclass
class ApiKey:
    """A single API key record (never stores the raw key)."""

    key_id: str  # First 16 hex chars of key hash
    key_hash: str  # SHA-256 hex digest of the full key
    name: str  # Human-readable label
    scopes: frozenset[str] = frozenset()  # Route validation should enforce non-empty role scopes
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None  # None = never expires
    last_used_at: float | None = None
    revoked: bool = False
    # Appended to preserve the public positional constructor used by embedders.
    tenant_id: str | None = None


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
        tenant_id: str | None = None,
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
            tenant_id=tenant_id,
            scopes=scopes,
            expires_at=expires_at,
        )
        self._keys[key_id] = record
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
        """List only records owned by one tenant; system records stay hidden."""
        return [record for record in self._keys.values() if record.tenant_id == tenant_id]

    def revoke_for_tenant(self, key_id: str, tenant_id: str) -> bool:
        """Atomically verify tenant ownership and revoke without a TOCTOU gap."""
        record = self._keys.get(key_id)
        if record is None or record.tenant_id != tenant_id:
            return False
        record.revoked = True
        return True


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()
