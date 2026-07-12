#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Canonical RBAC role allow-list shared by every auth-mode resolver."""

from __future__ import annotations

from typing import Any

# Canonical RBAC role allow-list. Any role minted from an external/untrusted
# source (JWT claims, proxy headers, the webhook IDP) MUST be filtered to this
# set; anything outside it is dropped so a compromised issuer cannot inject a
# privileged role like ``superuser``/``root``.
KNOWN_ROLES = frozenset({"viewer", "operator", "admin"})
_KNOWN_ROLES = KNOWN_ROLES
# Fallback role applied when role filtering leaves nothing.
_DEFAULT_ROLE = "viewer"


def _filter_known_roles(roles: Any) -> frozenset[str]:
    """Filter an arbitrary roles iterable to the known allow-list.

    Cleans each entry (str, stripped, lower-cased), drops any role outside
    ``_KNOWN_ROLES``, and falls back to ``{_DEFAULT_ROLE}`` when the result is
    empty. Shared by the JWT, header and webhook-IDP role-resolution paths.
    """
    cleaned = {str(role).strip().lower() for role in roles if str(role).strip()}
    allowed = cleaned & _KNOWN_ROLES
    return frozenset(allowed) if allowed else frozenset({_DEFAULT_ROLE})
