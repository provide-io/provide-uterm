//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"fmt"
	"sort"
	"sync"
)

// Factory constructs a Connector from a session id, display name and connector
// config. It is the Go port of registry.SessionConnectorFactory.
type Factory func(sessionID, displayName string, config map[string]any) (Connector, error)

// wrap adapts a *transportConnector constructor into a Factory, converting a nil
// concrete pointer into a nil interface so a build error never yields a non-nil
// interface wrapping a nil pointer.
func wrap(fn func(string, string, map[string]any) (*transportConnector, error)) Factory {
	return func(sessionID, displayName string, config map[string]any) (Connector, error) {
		c, err := fn(sessionID, displayName, config)
		if err != nil {
			return nil, err
		}
		return c, nil
	}
}

// builtin holds the four canonical connector factories, keyed by connector_type.
// Order note: network connectors modern→legacy, then local — matching the Python
// _BUILTIN_CLASSES ordering.
var builtin = map[string]Factory{
	"websocket": wrap(newWebSocket),
	"ssh":       wrap(newSSH),
	"telnet":    wrap(newTelnet),
	"shell":     wrap(newShell),
}

var (
	registryMu sync.RWMutex
	registry   = map[string]Factory{}
)

// Register registers a custom connector factory under a type name (the Go port
// of register_connector), overriding any prior registration for that name.
func Register(name string, factory Factory) {
	registryMu.Lock()
	defer registryMu.Unlock()
	registry[name] = factory
}

// Build instantiates a connector by type name, preferring a custom-registered
// factory over the builtin one. It returns an error for unknown types, mirroring
// build_connector's ValueError.
func Build(sessionID, displayName, connectorType string, config map[string]any) (Connector, error) {
	registryMu.RLock()
	factory, ok := registry[connectorType]
	registryMu.RUnlock()
	if !ok {
		factory, ok = builtin[connectorType]
	}
	if !ok {
		return nil, fmt.Errorf("unsupported connector_type: %q", connectorType)
	}
	return factory(sessionID, displayName, config)
}

// RegisteredTypes returns the sorted set of connector type names Build accepts
// (the builtins plus any custom registrations).
func RegisteredTypes() []string {
	seen := map[string]struct{}{}
	for name := range builtin {
		seen[name] = struct{}{}
	}
	registryMu.RLock()
	for name := range registry {
		seen[name] = struct{}{}
	}
	registryMu.RUnlock()
	out := make([]string, 0, len(seen))
	for name := range seen {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}
