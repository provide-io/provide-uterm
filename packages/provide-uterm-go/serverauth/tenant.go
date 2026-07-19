//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"regexp"
	"strings"
)

// tenantPattern ports the tenant-id validation regex shared by ApiKeyStore and
// LocalIdentityProvider in the C# port (^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$).
var tenantPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)

// CanonicalTenantID ports the C# CanonicalTenantId helper: trim the value, treat
// empty as absent (nil), and reject anything that does not match the tenant
// pattern (also nil). A non-nil result is a valid, canonical tenant id.
func CanonicalTenantID(tenantID string) *string {
	v := strings.TrimSpace(tenantID)
	if v == "" {
		return nil
	}
	if !tenantPattern.MatchString(v) {
		return nil
	}
	return &v
}
