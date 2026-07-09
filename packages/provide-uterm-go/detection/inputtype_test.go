//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import "testing"

func TestAutoDetectInputTypeAnyKey(t *testing.T) {
	for _, s := range []string{
		"Press any key to continue", "Press a key now", "Hit any key",
		"Strike any key", "<more> text", "[more] pages", "-- more --",
	} {
		if got := AutoDetectInputType(s); got != "any_key" {
			t.Errorf("AutoDetectInputType(%q) = %q, want any_key", s, got)
		}
	}
}

func TestAutoDetectInputTypeSingleKey(t *testing.T) {
	for _, s := range []string{
		"Continue? (y/n)", "Proceed (yes/no)", "Are you sure? Continue?",
		"Quit?", "Abort?", "Retry?", "Delete [y/n]", "(q)uit", "(a)bort",
	} {
		if got := AutoDetectInputType(s); got != "single_key" {
			t.Errorf("AutoDetectInputType(%q) = %q, want single_key", s, got)
		}
	}
}

func TestAutoDetectInputTypeMultiKey(t *testing.T) {
	for _, s := range []string{
		"Please enter your choice", "Type your message here", "Input required",
		"Name: ", "Password: ", "Username: ", "Choose: ", "Select: ",
		"Command: ", "Search: ",
	} {
		if got := AutoDetectInputType(s); got != "multi_key" {
			t.Errorf("AutoDetectInputType(%q) = %q, want multi_key", s, got)
		}
	}
}

func TestAutoDetectInputTypeDefault(t *testing.T) {
	if got := AutoDetectInputType("Some random text with no known prompt phrases"); got != "multi_key" {
		t.Errorf("got %q, want multi_key", got)
	}
}
