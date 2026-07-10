//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"fmt"
	"strings"
)

func trimSpace(s string) string { return strings.TrimSpace(s) }

// asString coerces a decoded TOML/JSON scalar to its string form, mirroring
// Python's str() where the validators call it.
func asString(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	default:
		return fmt.Sprintf("%v", t)
	}
}

func strOr(v any, fallback string) string {
	if v == nil {
		return fallback
	}
	if s, ok := v.(string); ok {
		return s
	}
	return asString(v)
}

// asInt coerces a numeric decoded value (int64/float64/int) to int.
func asInt(v any) (int, bool) {
	switch t := v.(type) {
	case int:
		return t, true
	case int64:
		return int(t), true
	case float64:
		return int(t), true
	default:
		return 0, false
	}
}

// asStringSlice coerces a decoded list to []string.
func asStringSlice(v any) []string {
	items, ok := v.([]any)
	if !ok {
		if s, ok := v.([]string); ok {
			return s
		}
		return nil
	}
	out := make([]string, 0, len(items))
	for _, it := range items {
		out = append(out, asString(it))
	}
	return out
}
