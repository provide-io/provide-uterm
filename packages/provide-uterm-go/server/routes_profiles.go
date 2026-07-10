//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// registerProfileRoutes wires the /api/profiles routes. Port of profiles.py.
func (s *Server) registerProfileRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/profiles", s.authenticated(s.handleListProfiles))
	mux.HandleFunc("POST /api/profiles", s.authenticated(s.handleCreateProfile))
	mux.HandleFunc("GET /api/profiles/{profile_id}", s.authenticated(s.handleGetProfile))
	mux.HandleFunc("PUT /api/profiles/{profile_id}", s.authenticated(s.handleUpdateProfile))
	mux.HandleFunc("DELETE /api/profiles/{profile_id}", s.authenticated(s.handleDeleteProfile))
	mux.HandleFunc("POST /api/profiles/{profile_id}/connect", s.authenticated(s.handleConnectProfile))
}

// profilesEnabled writes 503 and returns false when no store is configured.
func (s *Server) profilesEnabled(w http.ResponseWriter) bool {
	if s.deps.Profiles == nil {
		detailError(w, http.StatusServiceUnavailable, "profile store not available")
		return false
	}
	return true
}

func (s *Server) handleListProfiles(w http.ResponseWriter, r *http.Request) {
	if !s.profilesEnabled(w) {
		return
	}
	p := principalOf(r)
	var (
		profiles []serverconfig.ConnectionProfile
		err      error
	)
	if s.deps.Authz.IsAdmin(p) {
		profiles, err = s.deps.Profiles.ListProfiles(nil)
	} else {
		owner := p.SubjectID
		profiles, err = s.deps.Profiles.ListProfiles(&owner)
	}
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, profiles)
}

func (s *Server) handleGetProfile(w http.ResponseWriter, r *http.Request) {
	if !s.profilesEnabled(w) {
		return
	}
	id := r.PathValue("profile_id")
	if !requireID(w, "profile_id", id) {
		return
	}
	profile, err := s.deps.Profiles.GetProfile(id)
	if err != nil || profile == nil {
		detailError(w, http.StatusNotFound, "unknown profile: "+id)
		return
	}
	if !s.deps.Authz.CanReadProfile(principalOf(r), profile) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	writeJSON(w, http.StatusOK, profile)
}

func (s *Server) handleCreateProfile(w http.ResponseWriter, r *http.Request) {
	if !s.profilesEnabled(w) {
		return
	}
	p := principalOf(r)
	if !s.deps.Authz.CanCreateSession(p) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	body, _ := decodeJSONBody(r)
	now := s.clock.Wall()
	profile := serverconfig.ConnectionProfile{
		ProfileID:        "profile-" + randHex(12),
		Owner:            p.SubjectID,
		Name:             strDefault(strings.TrimSpace(stringField(body, "name")), "Unnamed"),
		ConnectorType:    strDefault(stringField(body, "connector_type"), "ssh"),
		Host:             optString(strings.TrimSpace(stringField(body, "host"))),
		Port:             optPort(body),
		Username:         optString(strings.TrimSpace(stringField(body, "username"))),
		Tags:             stringList(body["tags"]),
		InputMode:        strDefault(stringField(body, "input_mode"), "open"),
		RecordingEnabled: boolField(body, "recording_enabled", false),
		Visibility:       strDefault(stringField(body, "visibility"), "private"),
		CreatedAt:        now,
		UpdatedAt:        now,
	}
	created, err := s.deps.Profiles.CreateProfile(profile)
	if err != nil {
		detailError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, created)
}

func (s *Server) handleUpdateProfile(w http.ResponseWriter, r *http.Request) {
	if !s.profilesEnabled(w) {
		return
	}
	id := r.PathValue("profile_id")
	if !requireID(w, "profile_id", id) {
		return
	}
	profile, err := s.deps.Profiles.GetProfile(id)
	if err != nil || profile == nil {
		detailError(w, http.StatusNotFound, "unknown profile: "+id)
		return
	}
	if !s.deps.Authz.CanMutateProfile(principalOf(r), profile) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	body, _ := decodeJSONBody(r)
	updates := allowedProfileUpdates(body)
	updated, uerr := s.deps.Profiles.UpdateProfile(id, updates)
	if uerr != nil {
		detailError(w, http.StatusUnprocessableEntity, uerr.Error())
		return
	}
	if updated == nil {
		detailError(w, http.StatusNotFound, "unknown profile: "+id)
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) handleDeleteProfile(w http.ResponseWriter, r *http.Request) {
	if !s.profilesEnabled(w) {
		return
	}
	id := r.PathValue("profile_id")
	if !requireID(w, "profile_id", id) {
		return
	}
	profile, err := s.deps.Profiles.GetProfile(id)
	if err != nil || profile == nil {
		detailError(w, http.StatusNotFound, "unknown profile: "+id)
		return
	}
	if !s.deps.Authz.CanMutateProfile(principalOf(r), profile) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	_, _ = s.deps.Profiles.DeleteProfile(id)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleConnectProfile(w http.ResponseWriter, r *http.Request) {
	if !s.profilesEnabled(w) {
		return
	}
	id := r.PathValue("profile_id")
	if !requireID(w, "profile_id", id) {
		return
	}
	profile, err := s.deps.Profiles.GetProfile(id)
	if err != nil || profile == nil {
		detailError(w, http.StatusNotFound, "unknown profile: "+id)
		return
	}
	p := principalOf(r)
	if !s.deps.Authz.CanReadProfile(p, profile) || !s.deps.Authz.CanCreateSession(p) {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	body, _ := decodeJSONBody(r)
	connectorConfig := map[string]any{}
	if profile.Host != nil {
		connectorConfig["host"] = *profile.Host
	}
	if profile.Port != nil {
		connectorConfig["port"] = *profile.Port
	}
	if profile.Username != nil {
		connectorConfig["username"] = *profile.Username
	}
	if pw := stringField(body, "password"); pw != "" {
		connectorConfig["password"] = pw // pragma: allowlist secret
	}
	sessionID := "connect-" + randHex(12)
	payload := map[string]any{
		"session_id":        sessionID,
		"display_name":      profile.Name,
		"connector_type":    profile.ConnectorType,
		"connector_config":  connectorConfig,
		"input_mode":        profile.InputMode,
		"owner":             p.SubjectID,
		"visibility":        "private",
		"ephemeral":         true,
		"recording_enabled": profile.RecordingEnabled,
	}
	st, cerr := s.deps.Registry.CreateSession(r.Context(), payload)
	if cerr != nil {
		s.writeCreateError(w, cerr)
		return
	}
	writeJSON(w, http.StatusOK, s.sessionConnectResponse(st))
}

// sessionConnectResponse merges the created session's status with the derived
// dashboard URL, matching the quick-connect / profile-connect response shape.
func (s *Server) sessionConnectResponse(st *SessionStatus) map[string]any {
	out := map[string]any{
		"session_id":        st.SessionID,
		"url":               s.cfg.UI.AppPath + "/session/" + st.SessionID,
		"display_name":      st.DisplayName,
		"connector_type":    st.ConnectorType,
		"input_mode":        st.InputMode,
		"created_at":        st.CreatedAt,
		"owner":             st.Owner,
		"visibility":        st.Visibility,
		"tags":              st.Tags,
		"recording_enabled": st.RecordingEnabled,
		"auto_start":        st.AutoStart,
	}
	return out
}

// allowedProfileUpdates filters a PUT body to the profile update allow-list.
func allowedProfileUpdates(body map[string]any) map[string]any {
	updates := map[string]any{}
	for _, k := range []string{"name", "host", "port", "username", "tags", "input_mode", "recording_enabled", "visibility"} {
		if v, ok := body[k]; ok {
			updates[k] = v
		}
	}
	return updates
}

func randHex(n int) string {
	b := make([]byte, (n+1)/2)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)[:n]
}

func strDefault(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

func optString(v string) *string {
	if v == "" {
		return nil
	}
	return &v
}

func optPort(body map[string]any) *int {
	if v, ok := floatField(body, "port"); ok {
		p := int(v)
		return &p
	}
	return nil
}

func stringList(v any) []string {
	list, ok := v.([]any)
	if !ok {
		return []string{}
	}
	out := make([]string, 0, len(list))
	for _, item := range list {
		if sv := strings.TrimSpace(toString(item)); sv != "" {
			out = append(out, sv)
		}
	}
	return out
}
