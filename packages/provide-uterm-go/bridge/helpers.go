//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"strconv"
	"time"
)

// minInt is the smallest int, used as an "unbounded" floor for safeInt.
const minInt = -int(^uint(0)>>1) - 1

// nowTS returns the current Unix time in seconds, matching Python's
// time.time().
func nowTS() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// safeInt coerces a decoded-JSON value to int, returning def on failure, nil,
// or a result below minVal. It is a port of the server helper _safe_int:
// JSON numbers arrive as float64 (truncated toward zero like Python int()),
// integers pass through, decimal strings are parsed, and booleans map to 1/0
// like Python's int(bool).
func safeInt(val any, def, minVal int) int {
	var result int
	switch v := val.(type) {
	case nil:
		return def
	case int:
		result = v
	case int64:
		result = int(v)
	case float64:
		result = int(v)
	case bool:
		if v {
			result = 1
		}
	case string:
		parsed, err := strconv.Atoi(v)
		if err != nil {
			return def
		}
		result = parsed
	default:
		return def
	}
	if result < minVal {
		return def
	}
	return result
}

// truthy reports the Python bool() truthiness of a decoded-JSON value.
func truthy(val any) bool {
	switch v := val.(type) {
	case nil:
		return false
	case bool:
		return v
	case int:
		return v != 0
	case int64:
		return v != 0
	case float64:
		return v != 0
	case string:
		return v != ""
	default:
		return true
	}
}

// snapString reads a string field from a snapshot map, returning def when the
// key is absent or not a string.
func snapString(m map[string]any, key, def string) string {
	if s, ok := m[key].(string); ok {
		return s
	}
	return def
}

// snapInt reads an int field from a snapshot map with the Python
// int(m.get(key, def) or def) semantics: a missing, nil, or falsy-zero value
// yields def. Unlike safeInt it applies no lower bound.
func snapInt(m map[string]any, key string, def int) int {
	v, ok := m[key]
	if !ok || v == nil {
		return def
	}
	result := safeInt(v, def, minInt)
	if result == 0 {
		return def
	}
	return result
}

// snapBool reads a bool field from a snapshot map with the Python
// bool(m.get(key, def)) semantics: a missing key yields def, otherwise the
// value's truthiness.
func snapBool(m map[string]any, key string, def bool) bool {
	v, ok := m[key]
	if !ok {
		return def
	}
	return truthy(v)
}

// snapCursor reads the cursor field from a snapshot map, defaulting to
// {"x":0,"y":0} when absent (mirroring the Python default).
func snapCursor(m map[string]any) any {
	if c, ok := m["cursor"]; ok && c != nil {
		return c
	}
	return map[string]any{"x": 0, "y": 0}
}
