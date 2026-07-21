//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package policy holds pure role/lease/session gates shared with Python and C#.
// See spec/behavior.json and spec/behavior_vectors.json.
package policy

import "errors"

// Shared error strings — must match Python provide.uterm.bridge.policy and C# Policy.
const (
	ErrInsufficientRole = "forbidden: insufficient role"
	ErrNoActiveLease    = "forbidden: no active lease"
	ErrSessionInactive  = "forbidden: session inactive"
)

// Engine gates privileged input / control ops.
type Engine interface {
	CanInject(sessionID, leaseID, principalID, principalRole string) error
	CanPerform(op, role string, leaseOwned, sessionActive bool) error
}

// Strict implements the cross-language behavioral contract.
type Strict struct{}

var roleRank = map[string]int{
	"viewer":   0,
	"operator": 1,
	"admin":    2,
}

var opMinRole = map[string]string{
	"input_inject":   "operator",
	"hijack_step":    "operator",
	"hijack_release": "operator",
	"hijack_acquire": "operator",
}

func roleOK(role, minimum string) bool {
	rr, okR := roleRank[role]
	mr, okM := roleRank[minimum]
	return okR && okM && rr >= mr
}

// CanPerform evaluates op against role + lease + session preconditions.
func (p *Strict) CanPerform(op, role string, leaseOwned, sessionActive bool) error {
	minRole, ok := opMinRole[op]
	if !ok {
		return errors.New("forbidden: unknown operation " + op)
	}
	if !roleOK(role, minRole) {
		return errors.New(ErrInsufficientRole)
	}
	switch op {
	case "input_inject", "hijack_step":
		if !leaseOwned {
			return errors.New(ErrNoActiveLease)
		}
	}
	switch op {
	case "hijack_step", "hijack_acquire":
		if !sessionActive {
			return errors.New(ErrSessionInactive)
		}
	}
	return nil
}

// CanInject is the RFB/human-relay entrypoint (input_inject op).
func (p *Strict) CanInject(sessionID, leaseID, principalID, principalRole string) error {
	_ = sessionID
	return p.CanPerform("input_inject", principalRole, leaseID != "", true)
}
