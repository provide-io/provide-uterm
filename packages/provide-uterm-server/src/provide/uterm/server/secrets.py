#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Lazy, bounded resolution of environment and file secret references."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_core import core_schema

MAX_SECRET_BYTES = 1024 * 1024
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretResolutionError(RuntimeError):
    """A stable, deliberately redacted secret-resolution failure."""


@dataclass(frozen=True, slots=True)
class SecretReference:
    """An unresolved ``env:`` or ``file:`` reference safe for persistence."""

    scheme: str
    locator: str
    base_dir: Path | None = None

    @classmethod
    def parse(cls, value: str | SecretReference, *, base_dir: Path | None = None) -> SecretReference:
        if isinstance(value, cls):
            return value if base_dir is None else cls(value.scheme, value.locator, base_dir.resolve())
        if not isinstance(value, str) or ":" not in value:
            raise ValueError("invalid secret reference syntax")
        scheme, locator = value.split(":", 1)
        if scheme == "env":
            if not _ENV_NAME.fullmatch(locator):
                raise ValueError("invalid environment secret reference")
            return cls(scheme, locator)
        if scheme != "file" or not locator:
            raise ValueError("invalid secret reference syntax")
        path = Path(locator)
        if not path.is_absolute() and ".." in path.parts:
            raise ValueError("unsafe relative file secret reference")
        return cls(scheme, locator, base_dir.resolve() if base_dir is not None else None)

    def __str__(self) -> str:
        return f"{self.scheme}:{self.locator}"

    @property
    def value(self) -> str:
        """Return the persistable reference text, never resolved material."""
        return str(self)

    def resolve(self, *, max_bytes: int = MAX_SECRET_BYTES) -> bytes:
        """Resolve on demand, returning bytes and never embedding secret data in errors."""
        if max_bytes < 1:
            raise ValueError("maximum secret size must be positive")
        if self.scheme == "env":
            value = os.environ.get(self.locator)
            if value is None:
                raise SecretResolutionError("environment secret is unavailable")
            encoded = value.encode()
            if len(encoded) > max_bytes:
                raise SecretResolutionError("environment secret exceeds maximum size")
            return encoded
        return self._resolve_file(max_bytes)

    def _resolve_file(self, max_bytes: int) -> bytes:
        path = Path(self.locator)
        if not path.is_absolute():
            if self.base_dir is None:
                raise SecretResolutionError("relative file secret has no config directory")
            path = self.base_dir / path
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            if path.is_symlink():
                raise SecretResolutionError("file secret may not use symbolic links") from exc
            raise SecretResolutionError("file secret is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecretResolutionError("file secret must be a regular file")
            if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
                raise SecretResolutionError("file secret has unsafe ownership")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise SecretResolutionError("file secret has unsafe permissions")
            if metadata.st_size > max_bytes:
                raise SecretResolutionError("file secret exceeds maximum size")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            result = b"".join(chunks)
            if len(result) > max_bytes:
                raise SecretResolutionError("file secret exceeds maximum size")
            return result
        finally:
            os.close(descriptor)

    @classmethod
    def __get_pydantic_core_schema__(cls, _source: Any, _handler: Any) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls.parse,
            serialization=core_schema.plain_serializer_function_ser_schema(str, when_used="always"),
        )
