#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Input-hardening limits and policy toggles for the MCP tool surface.

Centralises the security tunables so no policy is hardcoded inline at the call
sites.
"""

from __future__ import annotations

# Keystroke byte cap for hijack_send (matches the sanitizer default so the two
# code paths cannot drift).
MAX_KEYSTROKE_BYTES: int = 4096
