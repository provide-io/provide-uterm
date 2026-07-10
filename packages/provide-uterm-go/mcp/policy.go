//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

// Authorization policy table for the MCP tool layer. Port of
// provide.uterm.ai.policy: the single source of truth mapping every MCP tool
// name to the minimum role required to invoke it.

// Role ladder (matches provide.uterm.server.authorization):
//   - "viewer"   — read-only inspection.
//   - "operator" — session lifecycle, input mode, broadcast, annotation.
//   - "admin"    — destructive / wide-blast-radius operations.

// roleRank is the total ordering on roles (admin > operator > viewer); higher
// value ⇒ more privilege. Unknown roles rank below viewer.
var roleRank = map[string]int{"viewer": 0, "operator": 1, "admin": 2}

// rankOf returns the numeric rank for role; unknown roles rank below viewer.
func rankOf(role string) int {
	if r, ok := roleRank[role]; ok {
		return r
	}
	return -1
}

// roleAtLeast reports whether actual is at least as privileged as minimum.
func roleAtLeast(actual, minimum string) bool {
	return rankOf(actual) >= rankOf(minimum)
}

// toolRequiredRoles maps each MCP tool name to its minimum role. This is the
// single source of truth; a tool absent from this table is refused by the
// authorization chokepoint rather than silently exposed.
var toolRequiredRoles = map[string]string{
	// Hijack lifecycle — exclusive worker takeover.
	"hijack_begin":     "admin",
	"hijack_heartbeat": "admin",
	"hijack_read":      "operator",
	"hijack_send":      "admin",
	"hijack_step":      "admin",
	"hijack_release":   "admin",
	// Session read-only inspection.
	"session_list":   "viewer",
	"session_status": "viewer",
	"session_read":   "viewer",
	"server_health":  "viewer",
	// Session lifecycle / mode changes.
	"session_connect":    "operator",
	"session_disconnect": "operator",
	"session_set_mode":   "operator",
	// Real-time event streams (read-only).
	"session_watch":     "viewer",
	"session_subscribe": "viewer",
	// Annotations are operator-tier (write to recording timeline).
	"session_annotate": "operator",
	// Fanout group creation is operator (groups are configuration);
	// broadcasting input is admin (wide blast radius).
	"fanout_group_create": "operator",
	"fanout_send":         "admin",
	// Arbitrary connector spawn / worker mode forcing / worker disconnect.
	"session_create":    "admin",
	"worker_input_mode": "admin",
	"worker_disconnect": "admin",
}

// requiredRole returns the minimum role required to invoke tool. The second
// result is false when the tool has no registered policy entry — callers treat
// that as a programming error (every registered MCP tool must have a policy).
func requiredRole(tool string) (string, bool) {
	role, ok := toolRequiredRoles[tool]
	return role, ok
}

// allowedConnectorTypes is the session_create connector spawn allowlist. Only
// well-known connectors are permitted via the MCP layer. Port of
// provide.uterm.ai.policy.ALLOWED_CONNECTOR_TYPES.
var allowedConnectorTypes = map[string]struct{}{
	"shell":     {},
	"telnet":    {},
	"ssh":       {},
	"ws":        {},
	"websocket": {},
	"pty":       {},
}

// isAllowedConnector reports whether connectorType is on the spawn allowlist.
func isAllowedConnector(connectorType string) bool {
	_, ok := allowedConnectorTypes[connectorType]
	return ok
}
