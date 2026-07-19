//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strconv"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// maxGraphicalTargetPage ports MaxGraphicalTargetPage.
const maxGraphicalTargetPage = 200

// registerGraphicalTargetRoutes wires the /api/graphical-targets REST surface.
// Port of UtermServer.GraphicalTargets.cs. The handlers resolve the principal
// internally (anonymous allowed) and gate on capability + tenant scope, so a
// missing tenant yields 403 rather than 401.
func (s *Server) registerGraphicalTargetRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/graphical-targets", s.handleListGraphicalTargets)
	mux.HandleFunc("POST /api/graphical-targets", s.handleCreateGraphicalTarget)
	mux.HandleFunc("GET /api/graphical-targets/{target_id}", s.handleGetGraphicalTarget)
	mux.HandleFunc("PUT /api/graphical-targets/{target_id}", s.handleUpdateGraphicalTarget)
	mux.HandleFunc("DELETE /api/graphical-targets/{target_id}", s.handleDeleteGraphicalTarget)
}

// resolveGraphicalScope ports TryResolveGraphicalScope: require the capability
// and derive the tenant scope from the principal's tenant id (never from client
// input). Both failures are a flat {"detail": "..."} 403.
func (s *Server) resolveGraphicalScope(
	w http.ResponseWriter, r *http.Request, capability string,
) (graphical.Scope, *serverauth.Principal, bool) {
	p := s.resolvePrincipal(r)
	if !s.deps.Authz.HasCapability(p, capability) {
		detailError(w, http.StatusForbidden, "graphical target access denied")
		return graphical.Scope{}, nil, false
	}
	tenant := ""
	if p.TenantID != nil {
		tenant = *p.TenantID
	}
	scope, ok := graphical.ScopeForTenant(tenant)
	if !ok {
		detailError(w, http.StatusForbidden, "graphical target access denied")
		return graphical.Scope{}, nil, false
	}
	return scope, p, true
}

func (s *Server) handleListGraphicalTargets(w http.ResponseWriter, r *http.Request) {
	scope, _, ok := s.resolveGraphicalScope(w, r, "graphical.target.read")
	if !ok {
		return
	}

	limit := 100
	offset := 0
	if raw := strings.TrimSpace(r.URL.Query().Get("limit")); raw != "" {
		v, err := strconv.Atoi(raw)
		if err != nil || v < 1 || v > maxGraphicalTargetPage {
			detailError(w, http.StatusUnprocessableEntity, "limit must be between 1 and 200")
			return
		}
		limit = v
	}
	if raw := strings.TrimSpace(r.URL.Query().Get("offset")); raw != "" {
		v, err := strconv.Atoi(raw)
		if err != nil || v < 0 {
			detailError(w, http.StatusUnprocessableEntity, "offset must be non-negative")
			return
		}
		offset = v
	}

	rows, err := s.deps.GraphicalTargets.List(scope)
	if err != nil {
		graphicalRouteError(w, err)
		return
	}

	total := len(rows)
	start := offset
	if start > total {
		start = total
	}
	end := start + limit
	if end > total {
		end = total
	}
	items := make([]*graphical.Definition, 0, end-start)
	for _, row := range rows[start:end] {
		items = append(items, row.PublicCopy())
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"items": items, "limit": limit, "offset": offset, "total": total,
	})
}

func (s *Server) handleGetGraphicalTarget(w http.ResponseWriter, r *http.Request) {
	scope, _, ok := s.resolveGraphicalScope(w, r, "graphical.target.read")
	if !ok {
		return
	}
	targetID := r.PathValue("target_id")
	target, err := s.deps.GraphicalTargets.Get(scope, targetID)
	if err != nil {
		graphicalRouteError(w, err)
		return
	}
	if target == nil {
		graphicalError(w, http.StatusNotFound, graphical.ErrNotFound, "graphical target not found")
		return
	}
	writeJSON(w, http.StatusOK, target.PublicCopy())
}

func (s *Server) handleCreateGraphicalTarget(w http.ResponseWriter, r *http.Request) {
	scope, p, ok := s.resolveGraphicalScope(w, r, "graphical.target.manage")
	if !ok {
		return
	}

	body, valid := decodeJSONBody(r)
	if !valid {
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrInvalidPayload, "invalid request body")
		return
	}
	if !graphicalKeysAllowed(body) {
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrInvalidPayload, "invalid request body")
		return
	}
	payload, hasTargetID, hasTenant, perr := parseGraphicalTargetBody(body)
	if perr != nil {
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrInvalidPayload, perr.Error())
		return
	}
	if hasTenant {
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrTenantManaged,
			"tenant_id is assigned from authenticated identity")
		return
	}
	if hasTargetID {
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrInvalidPayload,
			"target_id is server-assigned and cannot be supplied")
		return
	}

	payload.TenantID = ""
	if p.TenantID != nil {
		payload.TenantID = *p.TenantID
	}
	payload.TargetID = generateGraphicalTargetID()
	payload.IsSystem = false
	payload.CreatedBy = &p.SubjectID
	// CreatedAt is (re)assigned by the registry on Create.
	if strings.TrimSpace(payload.DisplayName) == "" {
		payload.DisplayName = "graphical-target"
	}

	created, err := s.deps.GraphicalTargets.Create(scope, payload)
	if err != nil {
		graphicalRouteError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, created.PublicCopy())
}

func (s *Server) handleUpdateGraphicalTarget(w http.ResponseWriter, r *http.Request) {
	scope, p, ok := s.resolveGraphicalScope(w, r, "graphical.target.manage")
	if !ok {
		return
	}
	targetID := r.PathValue("target_id")

	body, valid := decodeJSONBody(r)
	if !valid {
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrInvalidPayload, "invalid request body")
		return
	}
	if !graphicalKeysAllowed(body) {
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrInvalidPayload, "invalid request body")
		return
	}
	payload, _, hasTenant, perr := parseGraphicalTargetBody(body)
	if perr != nil {
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrInvalidPayload, perr.Error())
		return
	}
	if hasTenant {
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrTenantManaged,
			"tenant_id is assigned from authenticated identity")
		return
	}
	if payload.TargetID != "" && payload.TargetID != targetID {
		graphicalError(w, http.StatusConflict, graphical.ErrTargetIDMismatch, "target_id must match the request path")
		return
	}

	existing, err := s.deps.GraphicalTargets.Get(scope, targetID)
	if err != nil {
		graphicalRouteError(w, err)
		return
	}
	if existing == nil {
		graphicalError(w, http.StatusNotFound, graphical.ErrNotFound, "graphical target not found")
		return
	}

	payload.TargetID = targetID
	payload.TenantID = existing.TenantID
	payload.IsSystem = existing.IsSystem
	payload.UpdatedBy = &p.SubjectID
	// UpdatedAt is (re)assigned by the registry on Update.
	if strings.TrimSpace(payload.DisplayName) == "" {
		payload.DisplayName = existing.DisplayName
	}

	updated, err := s.deps.GraphicalTargets.Update(scope, payload)
	if err != nil {
		graphicalRouteError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, updated.PublicCopy())
}

func (s *Server) handleDeleteGraphicalTarget(w http.ResponseWriter, r *http.Request) {
	scope, _, ok := s.resolveGraphicalScope(w, r, "graphical.target.manage")
	if !ok {
		return
	}
	targetID := r.PathValue("target_id")
	if err := s.deps.GraphicalTargets.Delete(scope, targetID); err != nil {
		graphicalRouteError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// graphicalKeysAllowed ports the payload-key allowlist check.
func graphicalKeysAllowed(body map[string]any) bool {
	for k := range body {
		if _, ok := graphical.PayloadKeys[k]; !ok {
			return false
		}
	}
	return true
}

// parseGraphicalTargetBody ports TryParseGraphicalTargetBody: read the typed
// fields, tracking whether target_id / tenant_id were present. Semantic checks
// (identifier, protocol, endpoint, dimensions) run later in the registry.
func parseGraphicalTargetBody(body map[string]any) (target *graphical.Definition, hasTargetID, hasTenant bool, err error) {
	t := graphical.NewDefinition()
	if t.DisplayName, err = getStringField(body, "display_name", ""); err != nil {
		return nil, false, false, err
	}
	if t.TargetID, err = getStringField(body, "target_id", ""); err != nil {
		return nil, false, false, err
	}
	if t.Protocol, err = getStringField(body, "protocol", graphical.ProtocolRfb); err != nil {
		return nil, false, false, err
	}
	if t.Endpoint, err = getStringPtrField(body, "endpoint"); err != nil {
		return nil, false, false, err
	}
	if t.Secret, err = getStringPtrField(body, "secret"); err != nil {
		return nil, false, false, err
	}
	if t.CaSecretRef, err = getStringPtrField(body, "ca_secret_ref"); err != nil {
		return nil, false, false, err
	}
	if t.ClientCertSecretRef, err = getStringPtrField(body, "client_cert_secret_ref"); err != nil {
		return nil, false, false, err
	}
	if t.ClientKeySecretRef, err = getStringPtrField(body, "client_key_secret_ref"); err != nil {
		return nil, false, false, err
	}
	if t.Width, err = getIntField(body, "width", 640); err != nil {
		return nil, false, false, err
	}
	if t.Height, err = getIntField(body, "height", 480); err != nil {
		return nil, false, false, err
	}

	if raw, present := body["tenant_id"]; present {
		hasTenant = true
		if sv, ok := raw.(string); ok {
			t.TenantID = sv
		}
	}
	_, hasTargetID = body["target_id"]
	return t, hasTargetID, hasTenant, nil
}

// getStringField ports GetString: absent/null → fallback; wrong type → error.
func getStringField(body map[string]any, key, fallback string) (string, error) {
	raw, ok := body[key]
	if !ok || raw == nil {
		return fallback, nil
	}
	sv, ok := raw.(string)
	if !ok {
		return "", &graphical.Error{Code: graphical.CodeInvalid, Message: key + " must be a string"}
	}
	return sv, nil
}

// getStringPtrField is getStringField for a nullable field (absent/null → nil).
func getStringPtrField(body map[string]any, key string) (*string, error) {
	raw, ok := body[key]
	if !ok || raw == nil {
		return nil, nil
	}
	sv, ok := raw.(string)
	if !ok {
		return nil, &graphical.Error{Code: graphical.CodeInvalid, Message: key + " must be a string"}
	}
	return &sv, nil
}

// getIntField ports GetInt: number or numeric string; else error.
func getIntField(body map[string]any, key string, fallback int) (int, error) {
	raw, ok := body[key]
	if !ok || raw == nil {
		return fallback, nil
	}
	switch v := raw.(type) {
	case float64:
		return int(v), nil
	case int:
		return v, nil
	case string:
		if n, err := strconv.Atoi(strings.TrimSpace(v)); err == nil {
			return n, nil
		}
	}
	return 0, &graphical.Error{Code: graphical.CodeInvalid, Message: key + " must be an integer"}
}

// generateGraphicalTargetID ports GenerateGraphicalTargetId ("gt-" + 12 hex).
func generateGraphicalTargetID() string {
	var b [6]byte
	_, _ = rand.Read(b[:])
	return "gt-" + hex.EncodeToString(b[:])
}

// graphicalError writes the {"detail":{"code","message"}} envelope used by the
// graphical-target routes (distinct from the flat detailError shape).
func graphicalError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]any{"detail": map[string]any{"code": code, "message": message}})
}

// graphicalRouteError ports GraphicalRouteError: map a registry *graphical.Error
// onto its HTTP status + error code.
func graphicalRouteError(w http.ResponseWriter, err error) {
	ge, ok := err.(*graphical.Error)
	if !ok {
		graphicalError(w, http.StatusServiceUnavailable, graphical.ErrBackend, "graphical target backend failed")
		return
	}
	switch ge.Code {
	case graphical.CodeAlreadyExists:
		graphicalError(w, http.StatusConflict, graphical.ErrAlreadyExists, "graphical target already exists")
	case graphical.CodeImmutable:
		graphicalError(w, http.StatusConflict, graphical.ErrImmutable, "static graphical target is immutable")
	case graphical.CodeConflict:
		graphicalError(w, http.StatusConflict, graphical.ErrConflict, "graphical target transaction conflicted")
	case graphical.CodeInvalid:
		graphicalError(w, http.StatusUnprocessableEntity, graphical.ErrInvalidPayload, "graphical target definition is invalid")
	case graphical.CodeNotFound, graphical.CodeForbidden:
		graphicalError(w, http.StatusNotFound, graphical.ErrNotFound, "graphical target not found")
	case graphical.CodeClosed:
		graphicalError(w, http.StatusServiceUnavailable, graphical.ErrUnavailable, "graphical target service is unavailable")
	default:
		graphicalError(w, http.StatusServiceUnavailable, graphical.ErrBackend, "graphical target backend failed")
	}
}

// SeedGraphicalTargets ports UtermServer.SeedGraphicalTargets: build an
// in-memory registry seeded with the enabled config targets as immutable
// system/static entries. Port of ToGraphicalTargetDefinition for the conversion.
func SeedGraphicalTargets(cfg *serverconfig.UtermServerConfig) (graphical.Registry, error) {
	registry := graphical.NewInMemoryRegistry()
	for i := range cfg.GraphicalTargets {
		target := cfg.GraphicalTargets[i]
		if !target.Enabled {
			continue
		}
		entry, err := toGraphicalDefinition(target)
		if err != nil {
			return nil, err
		}
		if err := registry.AddStatic(entry); err != nil {
			return nil, err
		}
	}
	return registry, nil
}

// toGraphicalDefinition ports ToGraphicalTargetDefinition.
func toGraphicalDefinition(target serverconfig.GraphicalTargetConfig) (*graphical.Definition, error) {
	protocol := strings.ToLower(strings.TrimSpace(target.Protocol))
	if protocol == "" {
		protocol = graphical.ProtocolRfb
	}
	if protocol != graphical.ProtocolMemory && protocol != graphical.ProtocolRfb {
		return nil, &graphical.Error{Code: graphical.CodeInvalid, Message: "unsupported graphical target protocol: " + target.Protocol}
	}

	endpoint := strings.TrimSpace(target.TargetAddress)
	if protocol == graphical.ProtocolRfb && endpoint == "" {
		return nil, &graphical.Error{
			Code:    graphical.CodeInvalid,
			Message: "graphical target requires target_address for rfb protocol: " + target.TargetID,
		}
	}

	targetID := strings.TrimSpace(target.TargetID)
	if targetID == "" {
		targetID = generateGraphicalTargetID()
	}

	var endpointPtr *string
	if protocol == graphical.ProtocolRfb {
		host, port, err := graphical.ParseRfbEndpoint(&endpoint)
		if err != nil {
			return nil, err
		}
		ep := host + ":" + strconv.Itoa(port)
		endpointPtr = &ep
	}

	display := target.Name
	if strings.TrimSpace(display) == "" {
		display = targetID
	}

	def := &graphical.Definition{
		TargetID:    targetID,
		TenantID:    strings.TrimSpace(target.TenantID),
		DisplayName: display,
		Protocol:    protocol,
		Endpoint:    endpointPtr,
		Width:       clampDimension(target.Width, 640),
		Height:      clampDimension(target.Height, 480),
		IsSystem:    true,
		IsStatic:    true,
	}
	return def, nil
}

// clampDimension ports the width/height <=0 → default, >8192 → 8192 clamp used
// only for the config-seeded targets (the REST path rejects out-of-range).
func clampDimension(v, def int) int {
	if v <= 0 {
		return def
	}
	if v > 8192 {
		return 8192
	}
	return v
}
