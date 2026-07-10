//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package bootstrap wires a control-plane Config to the matching backend
// implementation. Port of provide.uterm.control.plane.bootstrap.
// bootstrap_control_plane.
//
// It lives in its own package (rather than in controlplane) to keep the
// backend packages importing only the shared vocabulary, avoiding an import
// cycle while still offering a single constructor. Memory-only consumers can
// import controlplane/memory directly and never pull in the SQLite driver.
package bootstrap

import (
	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

// New selects and constructs a control-plane Engine for the configured backend.
// An unknown backend yields a configuration error, matching the Python
// ValueError("unsupported control-plane backend: ...").
func New(config cp.Config) (cp.Engine, error) {
	switch config.Normalized().Backend {
	case cp.BackendMemory:
		return memory.New(config), nil
	case cp.BackendSQLite:
		return sqlite.New(config), nil
	default:
		return nil, cp.ConfigurationError(
			"unsupported control-plane backend: " + string(config.Backend))
	}
}
