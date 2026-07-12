// SPDX-License-Identifier: AGPL-3.0-or-later

package server

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

const maxGraphicalTargetPage = 200

func (s *Server) registerGraphicalTargetRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/graphical-targets", s.authenticated(s.handleListGraphicalTargets))
	mux.HandleFunc("POST /api/graphical-targets", s.authenticated(s.handleCreateGraphicalTarget))
	mux.HandleFunc("GET /api/graphical-targets/{target_id}", s.authenticated(s.handleGetGraphicalTarget))
	mux.HandleFunc("PUT /api/graphical-targets/{target_id}", s.authenticated(s.handleUpdateGraphicalTarget))
	mux.HandleFunc("DELETE /api/graphical-targets/{target_id}", s.authenticated(s.handleDeleteGraphicalTarget))
}

func (s *Server) graphicalScope(w http.ResponseWriter, r *http.Request, capability string) (TargetScope, bool) {
	p := principalOf(r)
	if p.TenantID == "" || !s.deps.Authz.HasCapability(p, capability) {
		detailError(w, http.StatusForbidden, "graphical target access denied")
		return TargetScope{}, false
	}
	scope, err := NewTenantTargetScope(p.TenantID)
	if err != nil {
		detailError(w, http.StatusForbidden, "graphical target access denied")
		return TargetScope{}, false
	}
	if s.deps.GraphicalTargets == nil {
		detailError(w, http.StatusServiceUnavailable, "graphical target service unavailable")
		return TargetScope{}, false
	}
	return scope, true
}

func publicGraphicalTarget(target serverconfig.GraphicalTargetDefinition) map[string]any {
	raw, _ := json.Marshal(target)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	delete(out, "ca_secret_ref")
	delete(out, "client_cert_secret_ref")
	delete(out, "client_key_secret_ref")
	return out
}

func decodeGraphicalTarget(r *http.Request) (serverconfig.GraphicalTargetDefinition, bool, error) {
	var raw map[string]json.RawMessage
	if err := json.NewDecoder(r.Body).Decode(&raw); err != nil {
		return serverconfig.GraphicalTargetDefinition{}, false, err
	}
	_, hasTenant := raw["tenant_id"]
	encoded, err := json.Marshal(raw)
	if err != nil {
		return serverconfig.GraphicalTargetDefinition{}, hasTenant, err
	}
	var target serverconfig.GraphicalTargetDefinition
	dec := json.NewDecoder(bytes.NewReader(encoded))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&target); err != nil {
		return target, hasTenant, err
	}
	return target, hasTenant, nil
}

func graphicalError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]any{"detail": map[string]string{"code": code, "message": message}})
}

func graphicalRouteError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, ErrGraphicalTargetAlreadyExists):
		graphicalError(w, http.StatusConflict, "graphical_target_exists", "graphical target already exists")
	case errors.Is(err, ErrGraphicalTargetImmutable):
		graphicalError(w, http.StatusConflict, "graphical_target_immutable", "static graphical target is immutable")
	case errors.Is(err, ErrGraphicalTargetTransaction):
		graphicalError(w, http.StatusConflict, "graphical_target_conflict", "graphical target transaction conflicted")
	case errors.Is(err, ErrGraphicalTargetInvalid):
		graphicalError(w, http.StatusUnprocessableEntity, "graphical_target_invalid", "graphical target definition is invalid")
	case errors.Is(err, ErrGraphicalTargetNotFound), errors.Is(err, ErrGraphicalTargetForbidden):
		graphicalError(w, http.StatusNotFound, "graphical_target_not_found", "graphical target not found")
	case errors.Is(err, ErrGraphicalTargetClosed):
		graphicalError(w, http.StatusServiceUnavailable, "graphical_target_unavailable", "graphical target service is unavailable")
	default:
		graphicalError(w, http.StatusServiceUnavailable, "graphical_target_backend_error", "graphical target backend failed")
	}
}

func (s *Server) handleListGraphicalTargets(w http.ResponseWriter, r *http.Request) {
	scope, ok := s.graphicalScope(w, r, "graphical.target.read")
	if !ok {
		return
	}
	limit := 100
	offset := 0
	var err error
	if raw := r.URL.Query().Get("limit"); raw != "" {
		limit, err = strconv.Atoi(raw)
	}
	if err != nil || limit < 1 || limit > maxGraphicalTargetPage {
		detailError(w, http.StatusUnprocessableEntity, "limit must be between 1 and 200")
		return
	}
	if raw := r.URL.Query().Get("offset"); raw != "" {
		offset, err = strconv.Atoi(raw)
	}
	if err != nil || offset < 0 {
		detailError(w, http.StatusUnprocessableEntity, "offset must be non-negative")
		return
	}
	targets, err := s.deps.GraphicalTargets.List(r.Context(), scope)
	if err != nil {
		graphicalRouteError(w, err)
		return
	}
	filtered := make([]serverconfig.GraphicalTargetDefinition, 0, len(targets))
	for _, target := range targets {
		if target.TenantID == nil || *target.TenantID != principalOf(r).TenantID {
			graphicalError(w, http.StatusNotFound, "graphical_target_not_found", "graphical target not found")
			return
		}
		filtered = append(filtered, target)
	}
	total := len(filtered)
	start := offset
	if start > total {
		start = total
	}
	end := start + limit
	if end > total {
		end = total
	}
	items := make([]map[string]any, 0, end-start)
	for _, target := range filtered[start:end] {
		items = append(items, publicGraphicalTarget(target))
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset, "total": total})
}

func (s *Server) handleGetGraphicalTarget(w http.ResponseWriter, r *http.Request) {
	scope, ok := s.graphicalScope(w, r, "graphical.target.read")
	if !ok {
		return
	}
	target, err := s.deps.GraphicalTargets.Get(r.Context(), scope, r.PathValue("target_id"))
	if err != nil {
		graphicalRouteError(w, err)
		return
	}
	if target == nil || target.TenantID == nil || *target.TenantID != principalOf(r).TenantID {
		detailError(w, http.StatusNotFound, "graphical target not found")
		return
	}
	writeJSON(w, http.StatusOK, publicGraphicalTarget(*target))
}

func (s *Server) handleCreateGraphicalTarget(w http.ResponseWriter, r *http.Request) {
	scope, ok := s.graphicalScope(w, r, "graphical.target.manage")
	if !ok {
		return
	}
	target, hasTenant, err := decodeGraphicalTarget(r)
	if err != nil {
		detailError(w, http.StatusUnprocessableEntity, "invalid request body")
		return
	}
	if hasTenant {
		graphicalError(w, http.StatusUnprocessableEntity, "tenant_managed", "tenant_id is assigned from authenticated identity")
		return
	}
	tenant := principalOf(r).TenantID
	target.TenantID = &tenant
	created, err := s.deps.GraphicalTargets.Create(r.Context(), scope, target)
	if err != nil {
		graphicalRouteError(w, err)
		return
	}
	s.audit(r, "graphical_target.create", map[string]any{"target_id": created.TargetID, "tenant_id": tenant})
	writeJSON(w, http.StatusCreated, publicGraphicalTarget(created))
}

func (s *Server) handleUpdateGraphicalTarget(w http.ResponseWriter, r *http.Request) {
	scope, ok := s.graphicalScope(w, r, "graphical.target.manage")
	if !ok {
		return
	}
	target, hasTenant, err := decodeGraphicalTarget(r)
	if err != nil {
		detailError(w, http.StatusUnprocessableEntity, "invalid request body")
		return
	}
	if hasTenant {
		graphicalError(w, http.StatusUnprocessableEntity, "tenant_managed", "tenant_id is assigned from authenticated identity")
		return
	}
	id := r.PathValue("target_id")
	if target.TargetID != "" && target.TargetID != id {
		graphicalError(w, http.StatusConflict, "target_id_mismatch", "target_id must match the request path")
		return
	}
	target.TargetID = id
	tenant := principalOf(r).TenantID
	target.TenantID = &tenant
	updated, err := s.deps.GraphicalTargets.Update(r.Context(), scope, target)
	if err != nil {
		graphicalRouteError(w, err)
		return
	}
	s.audit(r, "graphical_target.update", map[string]any{"target_id": id, "tenant_id": tenant})
	writeJSON(w, http.StatusOK, publicGraphicalTarget(updated))
}

func (s *Server) handleDeleteGraphicalTarget(w http.ResponseWriter, r *http.Request) {
	scope, ok := s.graphicalScope(w, r, "graphical.target.manage")
	if !ok {
		return
	}
	id := r.PathValue("target_id")
	if err := s.deps.GraphicalTargets.Delete(r.Context(), scope, id); err != nil {
		graphicalRouteError(w, err)
		return
	}
	s.audit(r, "graphical_target.delete", map[string]any{"target_id": id, "tenant_id": principalOf(r).TenantID})
	w.WriteHeader(http.StatusNoContent)
}
