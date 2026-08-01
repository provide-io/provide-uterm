//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"errors"
	"net/http"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/fanout"
)

const (
	unsupportedFanoutGovernance = "fanout governance is not supported by this server"
	// unavailableFanoutAuthorization matches the Python create_group wiring
	// gate's body so the cross-language contract canonicalizes it identically.
	unavailableFanoutAuthorization = "fan-out authorization is unavailable"
)

// registerFanoutRoutes wires the fan-out group CRUD + send + grant routes. Port
// of fanout/_routes.register_fanout_routes. Every route requires an
// global administrator. Error bodies use the {"error": ...} envelope of the
// Python JSONResponse handlers (bridgeError), not the {"detail": ...} /api shape.
func (s *Server) registerFanoutRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/fanout/groups", s.authenticated(s.fanoutAdmin(s.handleFanoutCreate)))
	mux.HandleFunc("GET /api/fanout/groups", s.authenticated(s.fanoutAdmin(s.handleFanoutList)))
	mux.HandleFunc("DELETE /api/fanout/groups/{group_id}", s.authenticated(s.fanoutAdmin(s.handleFanoutDelete)))
	mux.HandleFunc("POST /api/fanout/groups/{group_id}/send", s.authenticated(s.fanoutAdmin(s.handleFanoutSend)))
	mux.HandleFunc("POST /api/fanout/groups/{group_id}/grants", s.authenticated(s.fanoutAdmin(s.handleFanoutGrant)))
}

// fanoutAdmin enforces the global-admin boundary before a fan-out handler can
// parse a body or resolve a group. Session-scoped admin grants are deliberately
// insufficient for a multi-session operation.
func (s *Server) fanoutAdmin(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !s.deps.Authz.IsAdmin(principalOf(r)) {
			bridgeError(w, http.StatusForbidden, "admin role required")
			return
		}
		next(w, r)
	}
}

// handleFanoutCreate creates a fan-out group. Port of the POST /groups handler:
// it rejects (403) any known session the principal cannot read, then persists
// the group (400 on size/pattern validation failure).
func (s *Server) handleFanoutCreate(w http.ResponseWriter, r *http.Request) {
	// Fail closed before any member work: a controller whose authorization
	// dependency is unwired cannot judge access at all, so admitting on the
	// checks that happen to remain wired would be a silent downgrade.
	if !s.fanout.AuthorizationReady() {
		bridgeError(w, http.StatusForbidden, unavailableFanoutAuthorization)
		return
	}
	p := principalOf(r)
	body, _ := decodeJSONBody(r)
	workerIDs := stringList(body["worker_ids"])
	name := stringField(body, "name")

	// Strict by default: unknown members require an explicit dormant-member
	// opt-in, while known members always require current session access.
	for _, wid := range workerIDs {
		def, ok := s.deps.Registry.GetDefinition(r.Context(), wid)
		if !ok {
			if !s.cfg.FanoutAllowUnknownMembers {
				bridgeError(w, http.StatusBadRequest, "unknown fan-out session: "+wid)
				return
			}
			continue
		}
		if !s.deps.Authz.CanReadSession(p, def) {
			bridgeError(w, http.StatusForbidden, "forbidden: no read access to session "+wid)
			return
		}
	}

	threshold := 0.8
	if v, ok := floatField(body, "divergence_threshold"); ok {
		threshold = v
	}
	group := &fanout.Group{
		GroupID:             randHex(32),
		Name:                name,
		WorkerIDs:           workerIDs,
		CreatedBy:           p.SubjectID,
		CreatedAt:           s.clock.Wall(),
		Mode:                strDefault(stringField(body, "mode"), "parallel"),
		StopOnFirstError:    boolField(body, "stop_on_first_error", false),
		ErrorPattern:        stringField(body, "error_pattern"),
		QuiesceMS:           intField(body, "quiesce_ms", 500),
		MaxResponseMS:       intField(body, "max_response_ms", 10000),
		DivergenceThreshold: threshold,
	}
	groupID, err := s.fanout.CreateGroup(group, p.SubjectID)
	if err != nil {
		bridgeError(w, http.StatusBadRequest, err.Error())
		return
	}
	s.audit(r, "fanout.create_group", map[string]any{"group_id": groupID, "name": name})
	writeJSON(w, http.StatusOK, map[string]any{
		"group_id":      groupID,
		"name":          name,
		"session_count": len(workerIDs),
	})
}

// handleFanoutList lists the groups visible to the caller. Port of GET /groups.
func (s *Server) handleFanoutList(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	groups := s.fanout.ListGroups(p.SubjectID)
	out := make([]map[string]any, 0, len(groups))
	for _, g := range groups {
		out = append(out, map[string]any{
			"group_id":      g.GroupID,
			"name":          g.Name,
			"session_count": len(g.WorkerIDs),
			"mode":          g.Mode,
		})
	}
	writeJSON(w, http.StatusOK, out)
}

// handleFanoutDelete deletes a group (creator only). Port of DELETE /groups/{id}.
func (s *Server) handleFanoutDelete(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	groupID := r.PathValue("group_id")
	existing := s.fanout.GetGroup(groupID, p.SubjectID)
	if existing == nil {
		bridgeError(w, http.StatusNotFound, "group not found")
		return
	}
	if existing.CreatedBy != p.SubjectID {
		bridgeError(w, http.StatusForbidden, "only the group creator can delete it")
		return
	}
	s.fanout.DeleteGroup(groupID, p.SubjectID)
	s.audit(r, "fanout.delete_group", map[string]any{"group_id": groupID})
	w.WriteHeader(http.StatusNoContent)
}

// handleFanoutSend broadcasts input to a group and returns per-session results.
// Port of POST /groups/{id}/send.
func (s *Server) handleFanoutSend(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	groupID := r.PathValue("group_id")
	group := s.fanout.GetGroup(groupID, p.SubjectID)
	if group == nil {
		bridgeError(w, http.StatusNotFound, "group not found")
		return
	}
	if s.fanoutGovernanceUnsupported() {
		bridgeError(w, http.StatusNotImplemented, unsupportedFanoutGovernance)
		return
	}
	body, _ := decodeJSONBody(r)
	data := stringField(body, "data")
	// Absent quiesce/max (0) fall back to the group defaults inside Send. The
	// controller resolves and authorizes the stored membership itself.
	result, err := s.fanout.Send(r.Context(), groupID, data, p,
		intField(body, "quiesce_ms", 0), intField(body, "max_response_ms", 0))
	if err != nil {
		if errors.Is(err, fanout.ErrAdminRequired) || errors.Is(err, fanout.ErrPrincipalRequired) {
			bridgeError(w, http.StatusForbidden, "admin role required")
		} else {
			bridgeError(w, http.StatusServiceUnavailable, err.Error())
		}
		return
	}
	s.audit(r, "fanout.send", map[string]any{"group_id": groupID, "send_id": result.SendID})
	writeJSON(w, http.StatusOK, map[string]any{
		"group_id":           result.GroupID,
		"send_id":            result.SendID,
		"command":            result.Command,
		"sent_at":            result.SentAt,
		"results":            result.ResultMaps(),
		"divergent_sessions": result.DivergentSessions,
		"failed_sessions":    result.FailedSessions,
	})
}

func (s *Server) fanoutGovernanceUnsupported() bool {
	return s.cfg.Governance.PolicyWebhookURL != nil && *s.cfg.Governance.PolicyWebhookURL != ""
}

// handleFanoutGrant grants another principal access to a group (creator only).
// Port of POST /groups/{id}/grants.
func (s *Server) handleFanoutGrant(w http.ResponseWriter, r *http.Request) {
	p := principalOf(r)
	groupID := r.PathValue("group_id")
	existing := s.fanout.GetGroup(groupID, p.SubjectID)
	if existing == nil {
		bridgeError(w, http.StatusNotFound, "group not found")
		return
	}
	if existing.CreatedBy != p.SubjectID {
		bridgeError(w, http.StatusForbidden, "only the group creator can grant access")
		return
	}
	body, _ := decodeJSONBody(r)
	grantee := stringField(body, "grantee")
	s.fanout.GrantAccess(groupID, grantee, p.SubjectID)
	s.audit(r, "fanout.grant_access", map[string]any{"group_id": groupID, "grantee": grantee})
	w.WriteHeader(http.StatusNoContent)
}
