//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import "testing"

func TestHasCatastrophicConstruct(t *testing.T) {
	catastrophic := []string{
		"(a+)+", "(a*)*", "(a+)*", "(a*)+", `(\w+)+`, "(ab+)+",
		`\1+`, `(\1)+`, `x(\2)*y`, "(a+?)+",
	}
	for _, p := range catastrophic {
		if !hasCatastrophicConstruct(p) {
			t.Errorf("expected %q flagged catastrophic", p)
		}
	}
	safe := []string{
		"", "abc", "a+", "(abc)+", "(ab)cd", `\d{3}`, "login: $", "(a)(b)(c)",
		`\(a+\)+`, // escaped parens are literals, inner + not a nested quantifier
		`a\*`,     // escaped quantifier
	}
	for _, p := range safe {
		if hasCatastrophicConstruct(p) {
			t.Errorf("expected %q NOT flagged", p)
		}
	}
}
