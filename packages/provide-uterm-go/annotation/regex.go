//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import (
	"fmt"
	"regexp"
)

// RE2TranslateError is returned when a Python-syntax regex cannot be compiled
// under Go's RE2 engine. It names the offending pattern so callers can surface
// a precise diagnostic instead of silently dropping the rule.
//
// The common causes are constructs RE2 does not support: lookaround
// ((?=...), (?!...), (?<=...), (?<!...)) and backreferences (\1, (?P=name)).
// Every built-in annotation rule is RE2-compatible; this path only matters for
// custom rules supplied by callers.
type RE2TranslateError struct {
	// Pattern is the Python regex string that failed to compile.
	Pattern string
	// Err is the underlying regexp compile error.
	Err error
}

func (e *RE2TranslateError) Error() string {
	return fmt.Sprintf("regex %q is not RE2-compatible: %v", e.Pattern, e.Err)
}

func (e *RE2TranslateError) Unwrap() error { return e.Err }

// CompilePattern compiles a Python-syntax regex under RE2. Inline flags such as
// "(?i)" already parse under RE2, so the pattern is passed through unchanged.
// A residual compile failure is wrapped in a *RE2TranslateError.
func CompilePattern(pattern string) (*regexp.Regexp, error) {
	re, err := regexp.Compile(pattern)
	if err != nil {
		return nil, &RE2TranslateError{Pattern: pattern, Err: err}
	}
	return re, nil
}

// mustCompile compiles a known-good built-in pattern, panicking on failure. It
// is only used for the BuiltinRules, whose patterns are verified RE2-compatible
// by the test suite.
func mustCompile(pattern string) *regexp.Regexp {
	re, err := CompilePattern(pattern)
	if err != nil {
		panic(err)
	}
	return re
}
