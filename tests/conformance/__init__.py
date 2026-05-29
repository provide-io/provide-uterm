#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI ↔ Cloudflare parity conformance suite.

Runs the same scenarios against both the FastAPI ``TermHub`` server and the
Cloudflare ``SessionRuntime`` Durable Object so security/behaviour parity is
enforced going forward — the structural guard against the "CF port silently
diverged from server hardening" class of bug.
"""
