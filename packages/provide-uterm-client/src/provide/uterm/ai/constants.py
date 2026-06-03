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

# Maximum length of a user/LLM-supplied regex pattern. The length cap removes
# the cheap amplification path; it is paired with a structural denylist (see
# ``provide.uterm.ai.patterns.has_catastrophic_construct``) that rejects nested
# quantifiers and quantified backreferences — the classic catastrophic-
# backtracking shapes a short, under-the-cap pattern can still trigger. True
# backtracking-time bounds would still require a ``regex``/``re2`` engine, so a
# residual risk for exotic shapes (e.g. overlapping alternations) remains.
MAX_USER_PATTERN_LEN: int = 512

# Whether MCP-driven session_create may target private/internal hosts. Defaults
# to deny: an LLM should not be able to pivot to 169.254.169.254, RFC1918, or
# loopback. Operators that genuinely need internal targets must opt in.
ALLOW_PRIVATE_HOSTS: bool = False
