//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"sort"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// registerAPIKeyRoutes wires the /api/keys admin routes. Port of api_keys.py.
func (s *Server) registerAPIKeyRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/keys", s.authenticated(s.handleCreateAPIKey))
	mux.HandleFunc("GET /api/keys", s.authenticated(s.handleListAPIKeys))
	mux.HandleFunc("DELETE /api/keys/{key_id}", s.authenticated(s.handleRevokeAPIKey))
}

// requireAPIKeyAdmin enforces admin + api-keys-enabled, matching the shared
// authorization block of api_keys.py.
func (s *Server) requireAPIKeyAdmin(w http.ResponseWriter, r *http.Request) bool {
	if !s.deps.Authz.IsAdmin(principalOf(r)) {
		detailError(w, http.StatusForbidden, "admin role required")
		return false
	}
	if !s.cfg.Auth.APIKeysEnabled || s.deps.APIKeys == nil { // pragma: allowlist secret
		detailError(w, http.StatusForbidden, "API key management is disabled")
		return false
	}
	return true
}

var allowedKeyScopes = map[string]struct{}{"viewer": {}, "operator": {}, "admin": {}}

func (s *Server) handleCreateAPIKey(w http.ResponseWriter, r *http.Request) {
	if !s.requireAPIKeyAdmin(w, r) {
		return
	}
	body, ok := decodeJSONBody(r)
	if !ok {
		detailError(w, http.StatusUnprocessableEntity, "invalid request body")
		return
	}
	name := strings.TrimSpace(stringField(body, "name"))
	if name == "" {
		detailError(w, http.StatusUnprocessableEntity, "name is required")
		return
	}
	rawScopes, hasScopes := body["scopes"]
	if !hasScopes {
		detailError(w, http.StatusUnprocessableEntity, "scopes is required")
		return
	}
	list, isList := rawScopes.([]any)
	if !isList {
		detailError(w, http.StatusUnprocessableEntity, "scopes must be a list of role scopes")
		return
	}
	scopeSet := serverauth.NewSet()
	var invalid []string
	for _, item := range list {
		sc := strings.TrimSpace(toString(item))
		if sc == "" {
			continue
		}
		if _, okScope := allowedKeyScopes[sc]; !okScope {
			invalid = append(invalid, sc)
			continue
		}
		scopeSet[sc] = struct{}{}
	}
	if len(scopeSet) == 0 && len(invalid) == 0 {
		detailError(w, http.StatusUnprocessableEntity, "scopes must include at least one role scope")
		return
	}
	if len(invalid) > 0 {
		sort.Strings(invalid)
		detailError(w, http.StatusUnprocessableEntity,
			"invalid role scopes: "+strings.Join(invalid, ", ")+" (allowed: admin, operator, viewer)")
		return
	}
	if _, ok := body["tenant_id"]; ok {
		detailError(w, http.StatusUnprocessableEntity, "tenant_id is server-assigned and cannot be supplied")
		return
	}
	var expiresIn *int
	if raw, present := floatField(body, "expires_in_s"); present {
		if raw < 60 {
			detailError(w, http.StatusUnprocessableEntity, "expires_in_s must be >= 60")
			return
		}
		v := int(raw)
		expiresIn = &v
	}
	// Tenant is derived from the authenticated principal, never client input: a
	// tenant-scoped admin mints keys bound to their own tenant (isolated); a
	// system admin (no tenant) mints tenant-less system keys.
	tenant := principalTenant(r)
	var rawKey string
	var record *serverauth.ApiKey
	if tenant != "" {
		key, rec, err := s.deps.APIKeys.CreateForTenant(tenant, name, scopeSet, expiresIn)
		if err != nil {
			detailError(w, http.StatusUnprocessableEntity, err.Error())
			return
		}
		rawKey, record = key, rec
	} else {
		rawKey, record = s.deps.APIKeys.Create(name, scopeSet, expiresIn)
	}
	s.audit(r, "api_key.create", map[string]any{"key_id": record.KeyID, "name": name, "tenant_id": record.TenantID})
	writeJSON(w, http.StatusOK, map[string]any{
		"key":        rawKey,
		"key_id":     record.KeyID,
		"name":       record.Name,
		"tenant_id":  record.TenantID,
		"scopes":     record.Scopes.Sorted(),
		"created_at": record.CreatedAt,
		"expires_at": record.ExpiresAt,
	})
}

// principalTenant returns the authenticated principal's tenant id, or "" for a
// system (untenanted) principal.
func principalTenant(r *http.Request) string {
	p := principalOf(r)
	if p != nil && p.TenantID != nil {
		return *p.TenantID
	}
	return ""
}

func (s *Server) handleListAPIKeys(w http.ResponseWriter, r *http.Request) {
	if !s.requireAPIKeyAdmin(w, r) {
		return
	}
	// A tenant admin sees only their tenant's (non-revoked) keys; a system admin
	// sees every key.
	tenant := principalTenant(r)
	var keys []*serverauth.ApiKey
	if tenant != "" {
		keys = s.deps.APIKeys.ListKeysForTenant(tenant)
	} else {
		keys = s.deps.APIKeys.ListKeys()
	}
	out := make([]map[string]any, 0, len(keys))
	for _, k := range keys {
		out = append(out, map[string]any{
			"key_id":       k.KeyID,
			"name":         k.Name,
			"tenant_id":    k.TenantID,
			"scopes":       k.Scopes.Sorted(),
			"created_at":   k.CreatedAt,
			"expires_at":   k.ExpiresAt,
			"last_used_at": k.LastUsedAt,
			"revoked":      k.Revoked,
		})
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) handleRevokeAPIKey(w http.ResponseWriter, r *http.Request) {
	if !s.requireAPIKeyAdmin(w, r) {
		return
	}
	keyID := r.PathValue("key_id")
	// A tenant admin can revoke only keys owned by their tenant (a cross-tenant
	// key reads back as unknown); a system admin can revoke any key.
	tenant := principalTenant(r)
	var revoked bool
	if tenant != "" {
		revoked = s.deps.APIKeys.RevokeForTenant(keyID, tenant)
	} else {
		revoked = s.deps.APIKeys.Revoke(keyID)
	}
	if !revoked {
		detailError(w, http.StatusNotFound, "unknown key: "+keyID)
		return
	}
	s.audit(r, "api_key.revoke", map[string]any{"key_id": keyID})
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "key_id": keyID})
}

// toString coerces a JSON scalar to its string form (used for scope items).
func toString(v any) string {
	if sv, ok := v.(string); ok {
		return sv
	}
	return ""
}
