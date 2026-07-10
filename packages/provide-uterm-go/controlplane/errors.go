//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package controlplane

import "errors"

// Error is the base error for control-plane bootstrap and transaction failures.
// Port of control.plane.errors.ControlPlaneError.
type Error struct {
	Kind string // "", "configuration", "capability", "conflict"
	Msg  string
}

func (e *Error) Error() string { return e.Msg }

// newError builds a control-plane Error of the given kind.
func newError(kind, msg string) *Error { return &Error{Kind: kind, Msg: msg} }

// ConfigurationError is raised when configuration is invalid or incomplete.
func ConfigurationError(msg string) *Error { return newError("configuration", msg) }

// CapabilityError is raised when a caller requests a capability the engine does
// not expose.
func CapabilityError(msg string) *Error { return newError("capability", msg) }

// ConflictError is raised on commit when a write conflicts with a concurrently
// committed transaction. Mirrors control.plane.errors.ControlPlaneConflictError:
// the SQLite backend produces this via BEGIN IMMEDIATE serialization, while the
// memory backend detects it optimistically at commit time so a lease-acquire
// race yields exactly one winner on both backends.
func ConflictError(msg string) *Error { return newError("conflict", msg) }

// IsConflict reports whether err is (or wraps) a control-plane conflict error.
func IsConflict(err error) bool {
	var e *Error
	return errors.As(err, &e) && e.Kind == "conflict"
}
