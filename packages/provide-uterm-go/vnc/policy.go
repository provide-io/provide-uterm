//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vnc

import "github.com/provide-io/provide-uterm/packages/provide-uterm-go/policy"

// PolicyEngine gates privileged input / control ops (spec/behavior.json).
// Alias of policy.Engine for call sites in the VNC package.
type PolicyEngine = policy.Engine

// StrictPolicyEngine implements the cross-language behavioral contract.
type StrictPolicyEngine = policy.Strict

// Re-export stable error strings.
const (
	ErrInsufficientRole = policy.ErrInsufficientRole
	ErrNoActiveLease    = policy.ErrNoActiveLease
	ErrSessionInactive  = policy.ErrSessionInactive
)
