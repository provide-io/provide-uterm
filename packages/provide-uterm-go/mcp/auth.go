//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"context"
	"sort"
	"strings"
)

// Authorization chokepoint for MCP tool dispatch. Port of
// provide.uterm.ai.auth: every tool handler is wrapped by guard() before its
// body runs. The chokepoint resolves the calling principal (per-request context
// state, then the server's configured default), looks up the tool's required
// role in the policy table, and returns a structured authorization_denied dict
// (never an error across the wire) when the principal is under-privileged.

// McpPrincipal is the principal calling an MCP tool. Roles is a sorted,
// de-duplicated set of role names.
type McpPrincipal struct {
	SubjectID string
	Roles     []string
}

// newPrincipal builds a principal with a normalised (sorted, de-duplicated)
// role set, defaulting the subject to "anonymous".
func newPrincipal(subjectID string, roles ...string) McpPrincipal {
	if subjectID == "" {
		subjectID = "anonymous"
	}
	return McpPrincipal{SubjectID: subjectID, Roles: normaliseRoles(roles)}
}

// normaliseRoles returns a sorted, de-duplicated copy of roles.
func normaliseRoles(roles []string) []string {
	seen := make(map[string]struct{}, len(roles))
	out := make([]string, 0, len(roles))
	for _, r := range roles {
		if _, dup := seen[r]; dup {
			continue
		}
		seen[r] = struct{}{}
		out = append(out, r)
	}
	sort.Strings(out)
	return out
}

// hasAtLeast reports whether the principal holds a role at least minimum.
func (p McpPrincipal) hasAtLeast(minimum string) bool {
	for _, r := range p.Roles {
		if roleAtLeast(r, minimum) {
			return true
		}
	}
	return false
}

// principalFromHeaders builds a principal from X-Uterm-Principal / X-Uterm-Role
// headers (case-insensitive). Returns nil when neither header is present.
func principalFromHeaders(headers map[string]string) *McpPrincipal {
	if len(headers) == 0 {
		return nil
	}
	lowered := make(map[string]string, len(headers))
	for k, v := range headers {
		lowered[strings.ToLower(k)] = v
	}
	subject, hasSubject := lowered["x-uterm-principal"]
	role, hasRole := lowered["x-uterm-role"]
	if !hasSubject && !hasRole {
		return nil
	}
	roles := []string{"viewer"}
	if role != "" {
		roles = []string{role}
	}
	p := newPrincipal(subject, roles...)
	return &p
}

// principalCtxKey is the context key under which a per-request principal is
// stashed (e.g. by a transport that authenticated the caller).
type principalCtxKey struct{}

// WithPrincipal returns a child context carrying principal, so a handler
// resolves it instead of the server default. Exposed for transports/tests that
// authenticate the caller.
func WithPrincipal(ctx context.Context, p McpPrincipal) context.Context {
	return context.WithValue(ctx, principalCtxKey{}, p)
}

// resolvePrincipal returns the per-request principal from ctx, falling back to
// def.
func resolvePrincipal(ctx context.Context, def McpPrincipal) McpPrincipal {
	if ctx != nil {
		if p, ok := ctx.Value(principalCtxKey{}).(McpPrincipal); ok {
			return p
		}
	}
	return def
}

// AuthorizationContext bundles the state the chokepoint needs at every call. A
// single instance is created by CreateServer and closed over by the per-tool
// wrappers.
type AuthorizationContext struct {
	DefaultPrincipal McpPrincipal
}

// denyPayload renders an authorization denial as a tool-result dict, matching
// the shape of the rest of the MCP tool surface.
func denyPayload(tool, required string, principal McpPrincipal) map[string]any {
	return map[string]any{
		"success":         false,
		"error":           "authorization_denied",
		"tool":            tool,
		"required_role":   required,
		"principal":       principal.SubjectID,
		"principal_roles": append([]string(nil), principal.Roles...),
	}
}
