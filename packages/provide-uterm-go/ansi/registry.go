//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ansi

import (
	"fmt"
	"sync"
)

// dialect pairs a registered dialect name with its handler.
type dialect struct {
	name    string
	handler func(string) string
}

var (
	registryMu sync.RWMutex
	registry   []dialect
)

// RegisterColorDialect registers a color token dialect handler.
//
// Handlers are called in registration order by NormalizeColors. name must be
// unique (e.g. "pipe_codes"); handler converts tokens to ANSI escapes.
//
// It returns an error if name is already registered.
func RegisterColorDialect(name string, handler func(string) string) error {
	registryMu.Lock()
	defer registryMu.Unlock()
	for _, d := range registry {
		if d.name == name {
			return fmt.Errorf("color dialect %q is already registered", name)
		}
	}
	registry = append(registry, dialect{name: name, handler: handler})
	return nil
}

// UnregisterColorDialect removes a previously registered dialect.
//
// It returns an error if name is not registered.
func UnregisterColorDialect(name string) error {
	registryMu.Lock()
	defer registryMu.Unlock()
	for i, d := range registry {
		if d.name == name {
			registry = append(registry[:i], registry[i+1:]...)
			return nil
		}
	}
	return fmt.Errorf("color dialect %q is not registered", name)
}

// RegisteredDialects returns the names of all registered dialects, in call
// order.
func RegisteredDialects() []string {
	registryMu.RLock()
	defer registryMu.RUnlock()
	names := make([]string, len(registry))
	for i, d := range registry {
		names[i] = d.name
	}
	return names
}

// NormalizeColors converts all registered BBS color token formats to
// standard ANSI escapes.
//
// It runs each registered dialect handler in order. Built-in dialects handle:
//
//   - {F###} / {B###} 256-color tokens
//   - {P#} / {T#} legacy BBS palette tokens
//   - ~N tilde codes
//   - |00-|23 pipe codes
//
// Additional dialects can be added via RegisterColorDialect.
func NormalizeColors(text string) string {
	registryMu.RLock()
	handlers := make([]func(string) string, len(registry))
	for i, d := range registry {
		handlers[i] = d.handler
	}
	registryMu.RUnlock()
	for _, h := range handlers {
		text = h(text)
	}
	return text
}

// PreviewANSI is an alias for NormalizeColors.
func PreviewANSI(text string) string {
	return NormalizeColors(text)
}
