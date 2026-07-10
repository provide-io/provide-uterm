//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

func (s *Server) handleHijackSend(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubHijack) {
		return
	}
	clientID := sourceIP(r)
	if !s.deps.Hub.AllowRESTSendFor(clientID) {
		s.deps.Hub.Metric("rest_send_rate_limited_total", 1)
		bridgeError(w, http.StatusTooManyRequests, "rate_limited")
		return
	}
	ctx := r.Context()
	hs, _ := s.deps.Hub.GetRestSession(ctx, workerID, hijackID)
	if hs == nil {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	body, _ := decodeJSONBody(r)
	keys := stringField(body, "keys")
	if keys == "" {
		bridgeError(w, http.StatusBadRequest, "keys must not be empty.")
		return
	}
	if len(keys) > s.deps.Hub.MaxInputChars() {
		bridgeError(w, http.StatusBadRequest,
			"keys too long: "+itoa(len(keys))+" > "+itoa(s.deps.Hub.MaxInputChars()))
		return
	}
	expectPromptID := stringField(body, "expect_prompt_id")
	expectRegex := stringField(body, "expect_regex")
	timeoutMS := clampInt(intField(body, "timeout_ms", 2000), 100, 30000)
	pollMS := clampInt(intField(body, "poll_interval_ms", 120), 50, 5000)
	matched, snapshot, reason, gerr := s.deps.Hub.WaitForGuard(ctx, workerID, expectPromptID, expectRegex, timeoutMS, pollMS)
	if gerr != nil {
		matched, reason = false, gerr.Error()
	}
	if !matched {
		if reason == "" {
			reason = "prompt_guard_not_satisfied"
		}
		writeJSON(w, http.StatusConflict, map[string]any{
			"error":             reason,
			"current_prompt_id": extractPromptID(snapshot),
		})
		return
	}
	if !s.deps.Hub.CheckHijackValid(workerID, hijackID) {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	sent, _ := s.deps.Hub.SendWorker(ctx, workerID, map[string]any{"type": "input", "data": keys, "ts": s.clock.Wall()})
	if !sent {
		bridgeError(w, http.StatusConflict, "No worker connected for this session.")
		return
	}
	_, _ = s.deps.Hub.AppendEventData(ctx, workerID, "hijack_send", map[string]any{
		"hijack_id":        hijackID,
		"keys":             truncate(keys, 120),
		"expect_prompt_id": expectPromptID,
		"expect_regex":     expectRegex,
	})
	freshExpires := s.deps.Hub.GetFreshHijackExpiry(workerID, hijackID, hs.LeaseExpiresAt)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":                true,
		"worker_id":         workerID,
		"hijack_id":         hijackID,
		"sent":              keys,
		"matched_prompt_id": extractPromptID(snapshot),
		"lease_expires_at":  s.monoToWall(freshExpires),
	})
}

func (s *Server) handleHijackStep(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubHijack) {
		return
	}
	if !s.deps.Hub.AllowRESTSendFor(sourceIP(r)) {
		s.deps.Hub.Metric("rest_step_rate_limited_total", 1)
		bridgeError(w, http.StatusTooManyRequests, "rate_limited")
		return
	}
	ctx := r.Context()
	hs, _ := s.deps.Hub.GetRestSession(ctx, workerID, hijackID)
	if hs == nil {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	if !s.deps.Hub.CheckHijackValid(workerID, hijackID) {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	sent, _ := s.deps.Hub.SendWorker(ctx, workerID, controlMsg("step", hs.Owner, 0, s.clock.Wall(), ""))
	if !sent {
		bridgeError(w, http.StatusConflict, "No worker connected for this session.")
		return
	}
	_, _ = s.deps.Hub.AppendEventData(ctx, workerID, "hijack_step", map[string]any{"hijack_id": hijackID})
	s.deps.Hub.Metric("hijack_steps_total", 1)
	freshExpires := s.deps.Hub.GetFreshHijackExpiry(workerID, hijackID, hs.LeaseExpiresAt)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":               true,
		"worker_id":        workerID,
		"hijack_id":        hijackID,
		"lease_expires_at": s.monoToWall(freshExpires),
	})
}

func (s *Server) handleHijackRelease(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubHijack) {
		return
	}
	ctx := r.Context()
	hs, _ := s.deps.Hub.GetRestSession(ctx, workerID, hijackID)
	if hs == nil {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	if !s.mayReleaseLease(r, workerID, hs) {
		bridgeError(w, http.StatusForbidden, "Not the lease owner.")
		return
	}
	released, shouldResume := s.deps.Hub.ReleaseRestHijack(workerID, hijackID)
	if !released {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	if shouldResume && s.deps.Hub.CheckStillHijacked(workerID) {
		shouldResume = false
	}
	if shouldResume {
		_, _ = s.deps.Hub.SendWorker(ctx, workerID, controlMsg("resume", hs.Owner, 0, s.clock.Wall(), ""))
	}
	s.deps.Hub.NotifyHijackChanged(workerID, false, nil)
	s.deps.Hub.Metric("hijack_releases_total", 1)
	_, _ = s.deps.Hub.AppendEventData(ctx, workerID, "hijack_released",
		map[string]any{"hijack_id": hijackID, "owner": hs.Owner})
	_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
	_ = s.deps.Hub.PruneIfIdle(ctx, workerID)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "worker_id": workerID, "hijack_id": hijackID})
}

// mayReleaseLease enforces REST lease-release ownership: the acquiring
// principal, the session owner, or a global admin. Port of _may_release_lease.
func (s *Server) mayReleaseLease(r *http.Request, workerID string, hs *hub.HijackSession) bool {
	requester := principalOf(r).SubjectID
	if hs.AcquiredBy == nil || *hs.AcquiredBy == requester {
		return true
	}
	if def, ok := s.deps.Registry.GetDefinition(r.Context(), workerID); ok {
		if def.Owner != nil && *def.Owner == requester {
			return true
		}
	}
	return s.deps.Authz.IsAdmin(principalOf(r))
}

// registerWorkerCtlRoutes wires the worker-control REST routes. Port of
// rest_workerctl.py.
func (s *Server) registerWorkerCtlRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /worker/{worker_id}/input_mode", s.authenticated(s.handleWorkerInputMode))
	mux.HandleFunc("POST /worker/{worker_id}/disconnect_worker", s.authenticated(s.handleDisconnectWorker))
}

func (s *Server) handleWorkerInputMode(w http.ResponseWriter, r *http.Request) {
	workerID, _, ok := bridgeParams(w, r, false)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubMode) {
		return
	}
	body, _ := decodeJSONBody(r)
	mode := stringField(body, "input_mode")
	if mode != "hijack" && mode != "open" {
		// Matches the Pydantic regex-422 for an invalid input_mode value.
		write422PathParam(w, "input_mode")
		return
	}
	okSet, errKind, err := s.deps.Hub.SetInputMode(r.Context(), workerID, mode)
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if !okSet {
		if errKind == "not_found" {
			bridgeError(w, http.StatusNotFound, "No worker registered.")
			return
		}
		bridgeError(w, http.StatusConflict, "Cannot switch to open while hijack is active.")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "input_mode": mode, "worker_id": workerID})
}

func (s *Server) handleDisconnectWorker(w http.ResponseWriter, r *http.Request) {
	workerID, _, ok := bridgeParams(w, r, false)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubAdmin) {
		return
	}
	okDisc, err := s.deps.Hub.DisconnectWorker(r.Context(), workerID)
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if !okDisc {
		bridgeError(w, http.StatusNotFound, "No worker connected.")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "worker_id": workerID})
}
