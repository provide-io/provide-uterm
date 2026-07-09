//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"fmt"
	"strings"
)

// DetectorPatternCompileError is returned by NewPromptDetector in strict mode
// when one or more patterns fail to compile. Faithful to the Python exception
// of the same name.
type DetectorPatternCompileError struct {
	Failures []map[string]any
	message  string
}

func (e *DetectorPatternCompileError) Error() string { return e.message }

// compilePatterns compiles every pattern's positive regex (MULTILINE) and its
// optional negative regex (MULTILINE|IGNORECASE). Failures are recorded; in
// strict mode a non-empty failure list yields a *DetectorPatternCompileError.
func (d *PromptDetector) compilePatterns() ([]compiledPattern, error) {
	compiled := make([]compiledPattern, 0, len(d.patterns))
	var failed []map[string]any

	for _, pattern := range d.patterns {
		id := patternID(pattern)
		regexVal, hasRegex := pattern["regex"]
		if !hasRegex {
			failed = append(failed, map[string]any{"id": id, "error": "Missing key: 'regex'"})
			continue
		}
		regexStr := asString(regexVal)
		re, err := compilePyRegex(regexStr, "m")
		if err != nil {
			failed = append(failed, map[string]any{"id": id, "regex": regexStr, "error": err.Error()})
			continue
		}
		cp := compiledPattern{re: re, pat: pattern}
		if negStr, ok := resolveNegativeRegex(pattern); ok && negStr != "" {
			if negRe, nerr := compilePyRegex(negStr, "mi"); nerr == nil {
				cp.neg = negRe
				cp.negStr = negStr
			}
		}
		compiled = append(compiled, cp)
	}

	if len(failed) > 0 {
		d.compileFailures = failed
		if d.strict {
			return nil, newCompileError(failed)
		}
	}
	return compiled, nil
}

// swapPatterns atomically replaces the detector's patterns, rolling back on a
// strict-mode compile failure so the detector never holds a poisoned set.
func (d *PromptDetector) swapPatterns(candidate []Pattern) error {
	saved := d.patterns
	d.patterns = candidate
	compiled, err := d.compilePatterns()
	if err != nil {
		d.patterns = saved // roll back before returning
		return err
	}
	d.setCompiled(compiled)
	return nil
}

func newCompileError(failed []map[string]any) *DetectorPatternCompileError {
	parts := make([]string, 0, len(failed))
	for _, f := range failed {
		parts = append(parts, fmt.Sprintf("%s: %s", asString(f["id"]), asString(f["error"])))
	}
	msg := fmt.Sprintf("%d pattern(s) failed to compile in strict mode: %s",
		len(failed), strings.Join(parts, ", "))
	return &DetectorPatternCompileError{Failures: failed, message: msg}
}

// patternID mirrors pattern.get("id", "unknown").
func patternID(pattern Pattern) string {
	if v, ok := pattern["id"]; ok {
		if s, sok := v.(string); sok {
			return s
		}
	}
	return "unknown"
}
