//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"fmt"
	"sort"
	"strings"
)

// checkUnknownKeys rejects config keys outside allowed, matching the Python
// "unknown config keys for <name>: [...]" ValueError (with sorted keys).
func checkUnknownKeys(name string, config map[string]any, allowed map[string]struct{}) error {
	var unknown []string
	for k := range config {
		if _, ok := allowed[k]; !ok {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return fmt.Errorf("unknown config keys for %s: %v", name, unknown)
	}
	return nil
}

// sortedModes returns the valid input modes in sorted order (for error text).
func sortedModes() []string {
	modes := make([]string, 0, len(validModes))
	for m := range validModes {
		modes = append(modes, m)
	}
	sort.Strings(modes)
	return modes
}

// coerceString requires v to be a string.
func coerceString(v any) (string, error) {
	s, ok := v.(string)
	if !ok {
		return "", fmt.Errorf("expected string, got %T", v)
	}
	return s, nil
}

// optString returns config[key] as a string, or "" when absent/nil/non-string
// (mirroring Python's config.get(key) truthiness for optional string fields).
func optString(config map[string]any, key string) (string, bool) {
	v, ok := config[key]
	if !ok || v == nil {
		return "", false
	}
	s, ok := v.(string)
	if !ok {
		return "", false
	}
	return s, true
}

// coerceStringList converts v (a []any or []string of strings) to []string.
// Port of list(config.get("args") or []).
func coerceStringList(v any) []string {
	switch t := v.(type) {
	case nil:
		return nil
	case []string:
		return append([]string(nil), t...)
	case []any:
		out := make([]string, 0, len(t))
		for _, e := range t {
			out = append(out, fmt.Sprintf("%v", e))
		}
		return out
	default:
		return nil
	}
}

// coerceEnv converts v (a map[string]any / map[string]string of strings) to
// map[string]string. Port of dict(config.get("env") or {}).
func coerceEnv(v any) (map[string]string, error) {
	switch t := v.(type) {
	case nil:
		return map[string]string{}, nil
	case map[string]string:
		out := make(map[string]string, len(t))
		for k, val := range t {
			out[k] = val
		}
		return out, nil
	case map[string]any:
		out := make(map[string]string, len(t))
		for k, val := range t {
			s, ok := val.(string)
			if !ok {
				return nil, fmt.Errorf("env value for %q must be a string, got %T", k, val)
			}
			out[k] = s
		}
		return out, nil
	default:
		return nil, fmt.Errorf("env must be a map of string→string, got %T", v)
	}
}

// coerceBool converts v to bool (nil → false). Port of bool(config.get("inject", False)).
func coerceBool(v any) bool {
	b, ok := v.(bool)
	return ok && b
}

// coerceIntOr converts v to int, falling back to def for nil/non-numeric.
// Accepts int and float64 (JSON numbers decode to float64). Port of
// int(config.get(key, default)).
func coerceIntOr(v any, def int) int {
	switch t := v.(type) {
	case nil:
		return def
	case int:
		return t
	case int64:
		return int(t)
	case float64:
		return int(t)
	default:
		return def
	}
}

// coerceIntPtr converts config[key] to *int, returning nil when absent/nil.
// Port of the run_as_uid / run_as_gid (int | None) fields.
func coerceIntPtr(config map[string]any, key string) (*int, error) {
	v, ok := config[key]
	if !ok || v == nil {
		return nil, nil //nolint:nilnil // (*int, error): nil,nil means "not provided"
	}
	switch t := v.(type) {
	case int:
		return &t, nil
	case int64:
		n := int(t)
		return &n, nil
	case float64:
		n := int(t)
		return &n, nil
	default:
		return nil, fmt.Errorf("%s must be an integer, got %T", key, v)
	}
}

// splitEnv splits "K=V" into (K, V, true), or ("","",false) when there is no '='.
func splitEnv(kv string) (string, string, bool) {
	i := strings.IndexByte(kv, '=')
	if i < 0 {
		return "", "", false
	}
	return kv[:i], kv[i+1:], true
}

// setDefault sets m[key]=val only when key is absent. Port of dict.setdefault.
func setDefault(m map[string]string, key, val string) {
	if _, ok := m[key]; !ok {
		m[key] = val
	}
}
