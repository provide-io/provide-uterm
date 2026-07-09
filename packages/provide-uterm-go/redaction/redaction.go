//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package redaction provides reusable redaction helpers for terminal logs and
// captures. Port of provide.uterm.redaction.
package redaction

import "regexp"

// Redactor rewrites text, replacing sensitive spans.
type Redactor func(string) string

// MakeRedactor builds a text redactor from regex patterns. With no patterns
// the returned redactor is the identity function. An invalid pattern returns
// an error (Python raises re.error).
func MakeRedactor(patterns []string) (Redactor, error) {
	compiled := make([]*regexp.Regexp, 0, len(patterns))
	for _, pattern := range patterns {
		re, err := regexp.Compile(pattern)
		if err != nil {
			return nil, err
		}
		compiled = append(compiled, re)
	}
	if len(compiled) == 0 {
		return func(text string) string { return text }, nil
	}
	return func(text string) string {
		result := text
		for _, re := range compiled {
			result = re.ReplaceAllString(result, "[REDACTED]")
		}
		return result
	}, nil
}

// RedactText applies redactor to text, preserving identity when no redactor
// is configured.
func RedactText(text string, redactor Redactor) string {
	if redactor == nil {
		return text
	}
	return redactor(text)
}
