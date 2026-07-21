//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vnc

import "testing"

func TestCanInjectAlias(t *testing.T) {
	p := &StrictPolicyEngine{}
	if err := p.CanInject("session123", "lease456", "bob", "viewer"); err == nil {
		t.Fatal("expected error for viewer role, got nil")
	}
	if err := p.CanInject("session123", "lease456", "bob", "operator"); err != nil {
		t.Fatalf("operator with lease should pass: %v", err)
	}
	if err := p.CanInject("session123", "", "bob", "operator"); err == nil {
		t.Fatal("expected error for empty lease")
	}
}
