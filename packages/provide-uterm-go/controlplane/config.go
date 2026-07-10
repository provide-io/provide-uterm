//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package controlplane

// Backend selects a control-plane storage engine. Port of the Python
// “ControlPlaneBackend = Literal["memory", "sqlite"]“.
type Backend string

const (
	// BackendMemory is the volatile in-memory backend.
	BackendMemory Backend = "memory"
	// BackendSQLite is the durable SQLite-file backend.
	BackendSQLite Backend = "sqlite"
)

// EngineCapabilities are the engine feature flags discovered at bootstrap time.
// Port of control.plane.capability.EngineCapabilities.
type EngineCapabilities struct {
	SupportsTransactions bool
	SupportsMigrations   bool
	SupportsRetries      bool
}

// DefaultCapabilities returns the portable defaults (all true), matching the
// Python dataclass defaults.
func DefaultCapabilities() EngineCapabilities {
	return EngineCapabilities{
		SupportsTransactions: true,
		SupportsMigrations:   true,
		SupportsRetries:      true,
	}
}

// Config is the bootstrap configuration for control-plane backends. Port of
// control.plane.types.ControlPlaneConfig.
type Config struct {
	Backend      Backend
	DatabaseURL  string
	Capabilities EngineCapabilities
}

// DefaultConfig returns the Python default: memory backend, ":memory:" URL,
// portable capabilities.
func DefaultConfig() Config {
	return Config{
		Backend:      BackendMemory,
		DatabaseURL:  ":memory:",
		Capabilities: DefaultCapabilities(),
	}
}

// withDefaults fills zero-value fields with the Python defaults so callers may
// pass a partially-populated Config (e.g. only DatabaseURL set).
func (c Config) withDefaults() Config {
	if c.Backend == "" {
		c.Backend = BackendMemory
	}
	if c.DatabaseURL == "" {
		c.DatabaseURL = ":memory:"
	}
	if c.Capabilities == (EngineCapabilities{}) {
		c.Capabilities = DefaultCapabilities()
	}
	return c
}

// Normalized returns the Config with defaults applied. Exported so backend
// constructors can share the same normalization the bootstrap uses.
func (c Config) Normalized() Config { return c.withDefaults() }
