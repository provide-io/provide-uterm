//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"strings"
	"testing"
)

// A configured session id is capped like a created one, so its recording can
// always be retired within the 255-byte filename limit.
func TestConfiguredSessionIDIsCapped(t *testing.T) {
	if _, err := validateSessionID(strings.Repeat("a", MaxSessionIDBytes)); err != nil {
		t.Fatalf("a %d-byte id was refused: %v", MaxSessionIDBytes, err)
	}
	_, err := validateSessionID(strings.Repeat("a", MaxSessionIDBytes+1))
	if err == nil || !strings.Contains(err.Error(), "at most 200 bytes") {
		t.Fatalf("a %d-byte id = %v, want refused", MaxSessionIDBytes+1, err)
	}
}
