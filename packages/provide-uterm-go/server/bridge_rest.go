//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"encoding/base64"
	"image/png"
	"net/http"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// registerBridgeRESTRoutes wires the REST hijack + worker-control routes (the
// surface the Go client.HijackClient targets). Port of bridge/routes/rest.py +
// rest_workerctl.py. Each route is authenticated then per-path authorized
// (hub_authz.py).
func (s *Server) registerBridgeRESTRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /worker/{worker_id}/hijack/acquire", s.authenticated(s.handleHijackAcquire))
	mux.HandleFunc("POST /worker/{worker_id}/hijack/{hijack_id}/heartbeat", s.authenticated(s.handleHijackHeartbeat))
	mux.HandleFunc("GET /worker/{worker_id}/hijack/{hijack_id}/snapshot", s.authenticated(s.handleHijackSnapshot))
	mux.HandleFunc("GET /worker/{worker_id}/hijack/{hijack_id}/events", s.authenticated(s.handleHijackEvents))
	mux.HandleFunc("POST /worker/{worker_id}/hijack/{hijack_id}/send", s.authenticated(s.handleHijackSend))
	mux.HandleFunc("POST /worker/{worker_id}/hijack/{hijack_id}/step", s.authenticated(s.handleHijackStep))
	mux.HandleFunc("POST /worker/{worker_id}/hijack/{hijack_id}/release", s.authenticated(s.handleHijackRelease))

	mux.HandleFunc("POST /worker/{worker_id}/gui/attach", s.authenticated(s.handleGUIAttach))
	mux.HandleFunc("GET /worker/{worker_id}/hijack/{hijack_id}/gui/screenshot", s.authenticated(s.handleHijackGUIScreenshot))
	mux.HandleFunc("POST /worker/{worker_id}/hijack/{hijack_id}/gui/click", s.authenticated(s.handleHijackGUIClick))
	mux.HandleFunc("POST /worker/{worker_id}/hijack/{hijack_id}/gui/type", s.authenticated(s.handleHijackGUIType))

	s.registerWorkerCtlRoutes(mux)
}

// hubAuthKind is a capability class for a hub route.
type hubAuthKind int

const (
	hubHijack hubAuthKind = iota // session.control.hijack
	hubMode                      // session.control.mode
	hubRead                      // session.read
	hubAdmin                     // require admin
)

// authorizeHubRoute resolves the worker's session definition and enforces the
// per-path capability. Port of hub_authz.require_hub_route_authz. It writes the
// error and returns false on denial; the worker id must already be pattern-valid.
func (s *Server) authorizeHubRoute(w http.ResponseWriter, r *http.Request, workerID string, kind hubAuthKind) bool {
	p := principalOf(r)
	if kind == hubAdmin {
		if !s.deps.Authz.IsAdmin(p) {
			detailError(w, http.StatusForbidden, "admin role required")
			return false
		}
		return true
	}
	def, ok := s.deps.Registry.GetDefinition(r.Context(), workerID)
	if !ok {
		detailError(w, http.StatusNotFound, "unknown session: "+workerID)
		return false
	}
	switch kind {
	case hubRead:
		if !s.deps.Authz.CanReadSession(p, def) {
			detailError(w, http.StatusForbidden, "insufficient privileges")
			return false
		}
	case hubMode:
		if !s.deps.Authz.CanMutateSession(p, def, "session.control.mode") {
			detailError(w, http.StatusForbidden, "insufficient privileges")
			return false
		}
	default: // hubHijack
		if !s.deps.Authz.CanMutateSession(p, def, "session.control.hijack") {
			detailError(w, http.StatusForbidden, "insufficient privileges")
			return false
		}
	}
	return true
}

// monoToWall converts a hub monotonic timestamp to wall-clock using the shared
// clock (Python _mono_to_wall).
func (s *Server) monoToWall(mono float64) float64 {
	return s.clock.Wall() + (mono - s.clock.Monotonic())
}

// bridgeParams validates the worker_id (+ optional hijack_id) path params,
// writing a 422 on mismatch. hijackID is "" when the route has no hijack id.
func bridgeParams(w http.ResponseWriter, r *http.Request, withHijack bool) (workerID, hijackID string, ok bool) {
	workerID = r.PathValue("worker_id")
	if !validID(workerID) {
		write422PathParam(w, "worker_id")
		return "", "", false
	}
	if withHijack {
		hijackID = r.PathValue("hijack_id")
		if !hijackIDPattern.MatchString(hijackID) {
			write422PathParam(w, "hijack_id")
			return "", "", false
		}
	}
	return workerID, hijackID, true
}

func (s *Server) handleHijackAcquire(w http.ResponseWriter, r *http.Request) {
	workerID, _, ok := bridgeParams(w, r, false)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubHijack) {
		return
	}
	clientID := sourceIP(r)
	if !s.deps.Hub.AllowRESTAcquireFor(clientID) {
		s.deps.Hub.Metric("rest_acquire_rate_limited_total", 1)
		bridgeError(w, http.StatusTooManyRequests, "rate_limited")
		return
	}
	body, _ := decodeJSONBody(r)
	owner := strings.TrimSpace(stringField(body, "owner"))
	if owner == "" {
		owner = "operator"
	}
	leaseS := hubClampLease(intField(body, "lease_s", 90))
	ctx := r.Context()
	_, _ = s.deps.Hub.CleanupExpiredHijack(ctx, workerID)

	hijackID := newHijackID()
	wallNow := s.clock.Wall()
	monoNow := s.clock.Monotonic()

	okAcq, errKind, err := s.deps.Hub.TryAcquireRestHijack(ctx, workerID, owner, leaseS, hijackID, monoNow)
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if !okAcq {
		if errKind != "already_hijacked" {
			_, _ = s.deps.Hub.SendWorker(ctx, workerID, controlMsg("resume", owner, 0, wallNow, hijackID))
		}
		bridgeError(w, http.StatusConflict, acquireErrorMessage(errKind))
		return
	}
	s.deps.Hub.Metric("hijack_acquires_total", 1)
	ownerCopy := owner
	s.deps.Hub.NotifyHijackChanged(workerID, true, &ownerCopy)
	_, _ = s.deps.Hub.AppendEventData(ctx, workerID, "hijack_acquired",
		map[string]any{"hijack_id": hijackID, "owner": owner, "lease_s": leaseS})
	_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
	if acquired, aerr := s.deps.Hub.GetRestSession(ctx, workerID, hijackID); aerr == nil && acquired != nil {
		subj := principalOf(r).SubjectID
		acquired.AcquiredBy = &subj
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":               true,
		"worker_id":        workerID,
		"hijack_id":        hijackID,
		"lease_expires_at": wallNow + float64(leaseS),
		"owner":            owner,
	})
}

func (s *Server) handleHijackHeartbeat(w http.ResponseWriter, r *http.Request) {
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
	body, _ := decodeJSONBody(r)
	leaseS := hubClampLease(intField(body, "lease_s", 90))
	newExpires := s.deps.Hub.ExtendHijackLease(workerID, hijackID, hs.Owner, leaseS, s.clock.Monotonic())
	if newExpires == nil {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	_, _ = s.deps.Hub.AppendEventData(ctx, workerID, "hijack_heartbeat",
		map[string]any{"hijack_id": hijackID, "lease_s": leaseS})
	_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":               true,
		"worker_id":        workerID,
		"hijack_id":        hijackID,
		"lease_expires_at": s.monoToWall(*newExpires),
	})
}

func (s *Server) handleHijackSnapshot(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubRead) {
		return
	}
	ctx := r.Context()
	hs, _ := s.deps.Hub.GetRestSession(ctx, workerID, hijackID)
	if hs == nil {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	waitMS := queryInt(r, "wait_ms", 1500, 50, 10000)
	snapshot, _ := s.deps.Hub.WaitForSnapshot(ctx, workerID, waitMS)
	freshExpires := s.deps.Hub.GetFreshHijackExpiry(workerID, hijackID, hs.LeaseExpiresAt)
	leaseExpiresAt := s.monoToWall(freshExpires)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":               true,
		"worker_id":        workerID,
		"hijack_id":        hijackID,
		"snapshot":         snapshot,
		"prompt_id":        extractPromptID(snapshot),
		"lease_expires_at": leaseExpiresAt,
	})
}

func (s *Server) handleHijackEvents(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubRead) {
		return
	}
	ctx := r.Context()
	hs, _ := s.deps.Hub.GetRestSession(ctx, workerID, hijackID)
	if hs == nil {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	afterSeq := queryInt(r, "after_seq", 0, 0, 1<<30)
	limit := queryInt(r, "limit", 200, 1, 2000)
	data := s.deps.Hub.GetHijackEventsData(workerID, hijackID, hs, afterSeq, limit)
	rows, _ := data["rows"].([]map[string]any)
	freshExpires, _ := data["fresh_expires"].(float64)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":               true,
		"worker_id":        workerID,
		"hijack_id":        hijackID,
		"after_seq":        afterSeq,
		"latest_seq":       data["latest_seq"],
		"min_event_seq":    data["min_event_seq"],
		"has_more":         len(rows) >= limit,
		"events":           rows,
		"lease_expires_at": s.monoToWall(freshExpires),
	})
}

func (s *Server) handleGUIAttach(w http.ResponseWriter, r *http.Request) {
	workerID, _, ok := bridgeParams(w, r, false)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubMode) {
		return
	}
	body, _ := decodeJSONBody(r)
	target := stringField(body, "target_address")
	vmName := stringField(body, "vm_name")
	
	cc, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	
	client, err := vnc.NewLitevirtAIClient(r.Context(), cc, vmName)
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	
	st, err := s.deps.Hub.Registry.Require(workerID)
	if err != nil {
		detailError(w, http.StatusNotFound, "worker not found")
		return
	}
	st.GraphicalSession = client
	
	go func() {
		if err := client.RunHandshakeAndLoop(); err != nil {
			s.logger.Warn("gui loop exited", "error", err)
		}
	}()
	
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleHijackGUIScreenshot(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	if !s.authorizeHubRoute(w, r, workerID, hubRead) {
		return
	}
	ctx := r.Context()
	hs, _ := s.deps.Hub.GetRestSession(ctx, workerID, hijackID)
	if hs == nil {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return
	}
	st, err := s.deps.Hub.Registry.Require(workerID)
	if err != nil || st.GraphicalSession == nil {
		bridgeError(w, http.StatusNotFound, "No graphical session attached.")
		return
	}
	img, err := st.GraphicalSession.Screenshot()
	if err != nil {
		bridgeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	var buf bytes.Buffer
	_ = png.Encode(&buf, img)
	freshExpires := s.deps.Hub.GetFreshHijackExpiry(workerID, hijackID, hs.LeaseExpiresAt)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":               true,
		"worker_id":        workerID,
		"hijack_id":        hijackID,
		"screenshot":       base64.StdEncoding.EncodeToString(buf.Bytes()),
		"lease_expires_at": s.monoToWall(freshExpires),
	})
}

func (s *Server) requireGraphicalSession(w http.ResponseWriter, r *http.Request, workerID, hijackID string) (gui.GraphicalSession, bool) {
	if !s.authorizeHubRoute(w, r, workerID, hubHijack) {
		return nil, false
	}
	hs, _ := s.deps.Hub.GetRestSession(r.Context(), workerID, hijackID)
	if hs == nil {
		bridgeError(w, http.StatusNotFound, "Invalid or expired hijack session.")
		return nil, false
	}
	st, err := s.deps.Hub.Registry.Require(workerID)
	if err != nil || st.GraphicalSession == nil {
		bridgeError(w, http.StatusNotFound, "No graphical session attached.")
		return nil, false
	}
	return st.GraphicalSession, true
}

func (s *Server) handleHijackGUIClick(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	sess, ok := s.requireGraphicalSession(w, r, workerID, hijackID)
	if !ok {
		return
	}
	body, _ := decodeJSONBody(r)
	x := intField(body, "x", 0)
	y := intField(body, "y", 0)
	button := stringField(body, "button")
	var mask uint8
	switch button {
	case "left": mask = 1
	case "middle": mask = 2
	case "right": mask = 4
	}
	_ = sess.InjectPointer(x, y, mask)
	_ = sess.InjectPointer(x, y, 0)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleHijackGUIType(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	sess, ok := s.requireGraphicalSession(w, r, workerID, hijackID)
	if !ok {
		return
	}
	body, _ := decodeJSONBody(r)
	text := stringField(body, "text")
	for _, r := range text {
		// Basic uint32 translation for standard ASCII
		_ = sess.InjectKey(uint32(r), true)
		_ = sess.InjectKey(uint32(r), false)
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}
