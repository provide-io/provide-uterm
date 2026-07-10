//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import "testing"

func TestValidateSocketPath(t *testing.T) {
	if err := ValidateSocketPath("/run/ok.sock"); err != nil {
		t.Fatalf("valid path: %v", err)
	}
	assertErr(t, ValidateSocketPath("/tmp/ok\x00bad.sock"), "null byte")
	assertErr(t, ValidateSocketPath("relative/path.sock"), "absolute")
}
