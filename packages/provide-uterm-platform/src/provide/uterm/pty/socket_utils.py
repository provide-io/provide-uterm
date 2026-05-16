#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared socket helpers for the PTY transport layer."""

from __future__ import annotations


def validate_socket_path(path: str) -> None:
    """Reject Unix socket paths that aren't absolute or contain null bytes."""
    if "\x00" in path:
        raise ValueError("socket path contains null byte")
    if not path.startswith("/"):
        raise ValueError("socket path must be an absolute path")


__all__ = ["validate_socket_path"]
