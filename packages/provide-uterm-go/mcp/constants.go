//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

// Input-hardening limits and policy toggles for the MCP tool surface. Port of
// provide.uterm.ai.constants — centralises the security tunables so no policy
// is hardcoded inline at the call sites.
const (
	// MaxKeystrokeBytes is the keystroke byte cap for hijack_send (matches the
	// sanitizer default so the two code paths cannot drift).
	MaxKeystrokeBytes = 4096

	// MaxUserPatternLen is the maximum length of a user/LLM-supplied regex
	// pattern. The length cap removes the cheap amplification path; it is paired
	// with a structural denylist (hasCatastrophicConstruct) that rejects nested
	// quantifiers and quantified backreferences.
	MaxUserPatternLen = 512
)

// AllowPrivateHosts controls whether MCP-driven session_create may target
// private/internal hosts. Defaults to deny: an LLM should not be able to pivot
// to 169.254.169.254, RFC1918, or loopback. It is a package var (not a const)
// to mirror the Python module-level policy toggle.
var AllowPrivateHosts = false
