//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import "unicode/utf8"

// descriptionTruncate bounds how many code points of a raw match are embedded
// in a description.
const descriptionTruncate = 80

// fallbackPlaceholder is used in the fallback description so the raw match (a
// potential secret) is never embedded when a description template fails to
// format.
const fallbackPlaceholder = "<unavailable>"

// PatternDetector scans terminal event text against a set of DetectionRule
// objects. It is designed for the hot path: it returns nil immediately when
// text is empty and performs no work beyond the returned slice when no rules
// match. The detector is stateless and may be shared across streams.
type PatternDetector struct {
	rules []DetectionRule
}

// NewPatternDetector builds a detector over rules. A nil rules slice selects
// the built-in rule set (mirroring the Python default of rules=None), sharing
// the BuiltinRules backing array rather than copying it.
func NewPatternDetector(rules []DetectionRule) *PatternDetector {
	if rules == nil {
		rules = BuiltinRules
	}
	return &PatternDetector{rules: rules}
}

// Rules returns the detector's rule slice. Exposed for parity assertions with
// the Python "_rules is BUILTIN_RULES" test.
func (d *PatternDetector) Rules() []DetectionRule { return d.rules }

// Detect scans text against all rules that apply to eventType and returns a
// (possibly nil) slice of Annotations. At most one annotation is returned per
// category — the first rule whose pattern matches wins and later rules in that
// category are skipped.
func (d *PatternDetector) Detect(eventType, text string, seq int) []Annotation {
	anns, _ := d.Scan(eventType, text, seq)
	return anns
}

// Scan is like Detect but also returns the end offset (in code points) of the
// furthest match in text (0 when nothing matches). StreamingDetector uses the
// offset to carry only the window tail after the matched region.
func (d *PatternDetector) Scan(eventType, text string, seq int) ([]Annotation, int) {
	if text == "" {
		return nil, 0
	}

	var results []Annotation
	seen := make(map[string]struct{})
	maxEnd := 0

	for i := range d.rules {
		rule := d.rules[i]
		if _, ok := seen[rule.Category]; ok {
			continue
		}
		if !rule.AppliesTo(eventType) {
			continue
		}
		loc := rule.Pattern.FindStringIndex(text)
		if loc == nil {
			continue
		}

		seen[rule.Category] = struct{}{}
		endRunes := utf8.RuneCountInString(text[:loc[1]])
		if endRunes > maxEnd {
			maxEnd = endRunes
		}
		matchText := runeTruncate(text[loc[0]:loc[1]], descriptionTruncate)

		description, err := pyFormat(rule.DescriptionTemplate, map[string]string{
			"match":      matchText,
			"event_type": eventType,
		})
		if err != nil {
			fe, ok := err.(*pyFormatError)
			if !ok || fe.caught() {
				// A malformed template must not leak the raw match (a
				// potential secret) into the description, which flows to
				// telemetry/logs. Mirrors Python's except (KeyError, IndexError).
				description = rule.Label + ": " + fallbackPlaceholder
			} else {
				// A ValueError (unbalanced brace) is not caught by the Python
				// detector; propagate it as a panic, the Go analogue of the
				// exception escaping detect().
				panic(err)
			}
		}

		results = append(results, Annotation{
			Label:       rule.Label,
			Description: description,
			Severity:    rule.Severity,
			Source:      "detector",
			Principal:   "system",
			Span:        &AnnotationSpan{FromSeq: seq, ToSeq: seq},
		})
	}

	return results, maxEnd
}

// runeTruncate returns the first max code points of s (mirroring Python's
// slice s[:max] on a str).
func runeTruncate(s string, max int) string {
	if utf8.RuneCountInString(s) <= max {
		return s
	}
	rs := []rune(s)
	return string(rs[:max])
}
