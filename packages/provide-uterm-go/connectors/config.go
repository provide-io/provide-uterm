//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"fmt"
	"sort"
)

// validateKeys rejects connector_config keys outside the allowed set, mirroring
// each Python connector's `unknown = set(config) - _VALID_CONFIG_KEYS` guard.
func validateKeys(config map[string]any, kind string, allowed map[string]struct{}) error {
	var unknown []string
	for k := range config {
		if _, ok := allowed[k]; !ok {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) == 0 {
		return nil
	}
	sort.Strings(unknown)
	return fmt.Errorf("unknown %s connector_config keys: %v", kind, unknown)
}

// keySet builds an allowed-key set from a list.
func keySet(keys ...string) map[string]struct{} {
	m := make(map[string]struct{}, len(keys))
	for _, k := range keys {
		m[k] = struct{}{}
	}
	return m
}

// configStr reads a string config value with a fallback.
func configStr(config map[string]any, key, fallback string) string {
	if config == nil {
		return fallback
	}
	if v, ok := config[key].(string); ok && v != "" {
		return v
	}
	return fallback
}

// configInt reads an int config value (int/int64/float64) with a fallback.
func configInt(config map[string]any, key string, fallback int) int {
	if config == nil {
		return fallback
	}
	switch v := config[key].(type) {
	case int:
		return v
	case int64:
		return int(v)
	case float64:
		return int(v)
	}
	return fallback
}

// configBool reads a bool config value with a fallback.
func configBool(config map[string]any, key string, fallback bool) bool {
	if config == nil {
		return fallback
	}
	if v, ok := config[key].(bool); ok {
		return v
	}
	return fallback
}

// configStrList reads an argv-style string list. It accepts a []string, a
// []any of strings, or a single string (as a one-element argv). Returns nil
// when absent, so callers can apply their own default.
func configStrList(config map[string]any, key string) []string {
	if config == nil {
		return nil
	}
	switch v := config[key].(type) {
	case []string:
		return append([]string(nil), v...)
	case []any:
		out := make([]string, 0, len(v))
		for _, item := range v {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		if len(out) == 0 {
			return nil
		}
		return out
	case string:
		if v == "" {
			return nil
		}
		return []string{v}
	}
	return nil
}
