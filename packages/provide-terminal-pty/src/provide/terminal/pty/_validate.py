# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Input validation for PTY connector parameters.

All validators raise ValueError with a descriptive message on bad input.
Call these before any system call (fork, execve, PAM).
"""

from __future__ import annotations

import re

_MAX_PATH_LEN = 4096
_MAX_USERNAME_LEN = 255
_MAX_SERVICE_LEN = 255
_MAX_ENV_KEYS = 1000
_MAX_ENV_VALUE_LEN = 65536

# POSIX portable filename characters for usernames
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# PAM service names: letters, digits, hyphens, underscores
_SERVICE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_command(command: str) -> None:
    """Validate a command path for use with os.execve."""
    if not command:
        raise ValueError("command must not be empty")
    if "\x00" in command:
        raise ValueError("command contains null byte")
    if len(command) > _MAX_PATH_LEN:
        raise ValueError(f"command path too long (max {_MAX_PATH_LEN} chars)")
    if not command.startswith("/"):
        raise ValueError(
            f"command must be an absolute path (got {command!r}); "
            "relative paths and shell lookups are not allowed"
        )


def validate_username(username: str) -> None:
    """Validate an OS username."""
    if not username:
        raise ValueError("username must not be empty")
    if "\x00" in username:
        raise ValueError("username contains null byte")
    if len(username) > _MAX_USERNAME_LEN:
        raise ValueError(f"username too long (max {_MAX_USERNAME_LEN} chars)")
    if not _USERNAME_RE.match(username):
        raise ValueError(
            f"username {username!r} contains invalid character; "
            "only A-Z, a-z, 0-9, '.', '_', '-' are allowed"
        )


def validate_service_name(service: str) -> None:
    """Validate a PAM service name."""
    if not service:
        raise ValueError("PAM service name must not be empty")
    if "\x00" in service:
        raise ValueError("PAM service name contains null byte")
    if len(service) > _MAX_SERVICE_LEN:
        raise ValueError(f"PAM service name too long (max {_MAX_SERVICE_LEN} chars)")
    if not _SERVICE_RE.match(service):
        raise ValueError(
            f"PAM service name {service!r} contains invalid character; "
            "only A-Z, a-z, 0-9, '_', '-' are allowed"
        )


def validate_env(env: dict[str, str]) -> None:
    """Validate an environment dict for use with os.execve."""
    if len(env) > _MAX_ENV_KEYS:
        raise ValueError(f"env dict has too many keys (max {_MAX_ENV_KEYS})")
    for key, value in env.items():
        if "=" in key:
            raise ValueError(f"invalid key {key!r}: env keys must not contain '='")
        if "\x00" in key:
            raise ValueError(f"env key {key!r} contains null byte")
        if "\x00" in value:
            raise ValueError(f"env value for {key!r} contains null byte")
        if len(value) > _MAX_ENV_VALUE_LEN:
            raise ValueError(
                f"env value for {key!r} too long (max {_MAX_ENV_VALUE_LEN} chars)"
            )
