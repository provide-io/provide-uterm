//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import "strings"

var (
	anyKeyPhrases = []string{
		"press any key", "press a key", "hit any key", "strike any key",
		"<more>", "[more]", "-- more --",
	}
	singleKeyPhrases = []string{
		"(y/n)", "(yes/no)", "continue?", "quit?", "abort?", "retry?",
		"[y/n]", "(q)uit", "(a)bort",
	}
	multiKeyPhrases = []string{
		"enter", "type", "input", "name:", "password:", "username:",
		"choose:", "select:", "command:", "search:",
	}
)

// AutoDetectInputType heuristically detects the input type from prompt text,
// returning "any_key", "single_key", or "multi_key" (the default).
func AutoDetectInputType(screen string) string {
	lower := strings.ToLower(screen)
	if containsAny(lower, anyKeyPhrases) {
		return "any_key"
	}
	if containsAny(lower, singleKeyPhrases) {
		return "single_key"
	}
	if containsAny(lower, multiKeyPhrases) {
		return "multi_key"
	}
	return "multi_key"
}

func containsAny(s string, phrases []string) bool {
	for _, p := range phrases {
		if strings.Contains(s, p) {
			return true
		}
	}
	return false
}
