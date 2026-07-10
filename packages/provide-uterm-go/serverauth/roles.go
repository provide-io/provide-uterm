//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import "strings"

// knownRoles ports auth_roles._KNOWN_ROLES — the canonical RBAC allow-list.
// Any role minted from an external/untrusted source (JWT claims, proxy
// headers, the webhook IDP) MUST be filtered to this set.
var knownRoles = NewSet("viewer", "operator", "admin")

// defaultRole ports auth_roles._DEFAULT_ROLE.
const defaultRole = "viewer"

// FilterKnownRoles ports auth_roles._filter_known_roles: clean each entry
// (stripped, lower-cased), drop any role outside the allow-list, and fall back
// to {defaultRole} when the result is empty.
func FilterKnownRoles(roles []string) Set {
	cleaned := NewSet()
	for _, role := range roles {
		r := strings.TrimSpace(role)
		if r == "" {
			continue
		}
		cleaned[strings.ToLower(r)] = struct{}{}
	}
	allowed := NewSet()
	for r := range cleaned {
		if knownRoles.Has(r) {
			allowed[r] = struct{}{}
		}
	}
	if len(allowed) == 0 {
		return NewSet(defaultRole)
	}
	return allowed
}
