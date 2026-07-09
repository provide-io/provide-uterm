//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package detection is a behavior-faithful Go port of the Python module
// provide.uterm.detection: rule schemas + JSON loading, cursor-aware prompt
// detection, key/value extraction, declarative flow execution, screen
// buffering, and screen saving.
//
// Patterns and snapshots are represented as map[string]any (mirroring the
// Python dicts) so the wire-format and matching semantics stay identical to
// the reference implementation. Python regexes are compiled under Go's RE2
// engine; see translatePyRegex for the (small) set of translations required.
package detection

import (
	"strconv"
	"strings"
	"unicode"
)

// isPySpace reports whether r matches Python's str.isspace character set:
// Go's unicode.IsSpace plus the information separators U+001C-U+001F. Kept
// in sync with the identical helper in the screen package.
func isPySpace(r rune) bool {
	return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
}

// pyStrip mirrors Python's str.strip() with no arguments.
func pyStrip(s string) string {
	return strings.TrimFunc(s, isPySpace)
}

// pyTruthy mirrors Python's bool(x) truthiness for the value kinds that can
// appear in a decoded JSON snapshot/pattern (nil, bool, numbers, strings,
// slices, maps).
func pyTruthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int:
		return x != 0
	case int64:
		return x != 0
	case float64:
		return x != 0
	case []any:
		return len(x) != 0
	case map[string]any:
		return len(x) != 0
	default:
		return true
	}
}

// pyIntOr0 mirrors Python's “int(value or 0)“ guarded by a try/except that
// falls back to 0. Falsy values collapse to 0; truthy numeric/string values
// are converted (truncating floats, parsing integer strings); anything that
// cannot be converted yields 0.
func pyIntOr0(v any) int {
	if !pyTruthy(v) {
		return 0
	}
	switch x := v.(type) {
	case bool:
		return 1 // truthy bool is True -> int(True) == 1
	case int:
		return x
	case int64:
		return int(x)
	case float64:
		return int(x) // Python int(float) truncates toward zero
	case string:
		// Python int("5") parses base-10 integers; int("5.0") raises -> 0.
		n, err := strconv.Atoi(strings.TrimSpace(x))
		if err != nil {
			return 0
		}
		return n
	default:
		return 0
	}
}

// asString returns the string form of a pattern/snapshot field, treating a
// missing (nil) value as the empty string. Non-string values are coerced via
// their Go default formatting only when necessary; pattern ids and regexes
// are always strings in practice.
func asString(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// patternString reads key from a pattern map, returning def when the key is
// absent or not a string. Mirrors Python's dict.get(key, default).
func patternString(pattern map[string]any, key, def string) string {
	if v, ok := pattern[key]; ok {
		if s, sok := v.(string); sok {
			return s
		}
	}
	return def
}

// patternBoolDefaultTrue reads a boolean-ish key defaulting to True when
// absent, then collapses to Python truthiness. Mirrors
// “bool(pattern.get(key, True))“.
func patternBoolDefaultTrue(pattern map[string]any, key string) bool {
	v, ok := pattern[key]
	if !ok {
		return true
	}
	return pyTruthy(v)
}
