//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import "testing"

// TestSilentErrorMessage covers the silentError.Error stringer, which is never
// printed in the normal flow (Execute matches it via errors.As and skips the
// message), so it needs an explicit exercise.
func TestSilentErrorMessage(t *testing.T) {
	if got := errTampered.Error(); got != "" {
		t.Errorf("silentError.Error() = %q, want empty", got)
	}
}

// TestFmtHashNil covers the nil-pointer branch of fmtHash.
func TestFmtHashNil(t *testing.T) {
	if got := fmtHash(nil); got != "None" {
		t.Errorf("fmtHash(nil) = %q, want None", got)
	}
	h := "deadbeef"
	if got := fmtHash(&h); got != "deadbeef" {
		t.Errorf("fmtHash(&h) = %q", got)
	}
}
