//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"image"
	"image/png"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc"
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
	mux.HandleFunc("POST /worker/{worker_id}/hijack/{hijack_id}/gui/key", s.authenticated(s.handleHijackGUIKey))
	mux.HandleFunc("POST /worker/{worker_id}/hijack/{hijack_id}/gui/drag", s.authenticated(s.handleHijackGUIDrag))
	// Browser human-relay WebSocket (ServeHumanRelay) — upgrade after authz gate.
	mux.HandleFunc("GET /worker/{worker_id}/hijack/{hijack_id}/gui/vnc", s.authenticated(s.handleHumanVnc))

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

// handleGUIAttach is the canonical registry-backed, tenant-isolated graphical
// attach. Port of C# UtermServer.Gui.cs HandleGuiAttach. The legacy path that
// dialed a litevirt gRPC target straight from the request body (no registry, no
// tenant isolation) is retired: attach now gates on the graphical.session.attach
// capability + session.control.hijack, reads only {target_id}, resolves the
// target through the tenant-scoped registry, and dispatches on its protocol.
func (s *Server) handleGUIAttach(w http.ResponseWriter, r *http.Request) {
	workerID, _, ok := bridgeParams(w, r, false)
	if !ok {
		return
	}
	// Gate 1: the graphical.session.attach capability (RBAC), independent of the
	// per-session hijack grant below.
	p := principalOf(r)
	if !s.deps.Authz.HasCapability(p, "graphical.session.attach") {
		detailError(w, http.StatusForbidden, "insufficient privileges")
		return
	}
	// Gate 2: session.control.hijack on this worker's session.
	if !s.authorizeHubRoute(w, r, workerID, hubHijack) {
		return
	}

	body, _ := decodeJSONBody(r)
	targetID := strings.TrimSpace(stringField(body, "target_id"))
	if targetID == "" {
		detailError(w, http.StatusUnprocessableEntity, "target_id is required for gui attach")
		return
	}

	// Tenant scope is derived from the authenticated principal, never client input.
	tenant := ""
	if p != nil && p.TenantID != nil {
		tenant = *p.TenantID
	}
	scope, scopeOK := graphical.ScopeForTenant(tenant)
	if !scopeOK {
		detailError(w, http.StatusForbidden, "graphical target access denied")
		return
	}

	target, err := s.deps.GraphicalTargets.Get(scope, targetID)
	if err != nil {
		graphicalRouteError(w, err)
		return
	}
	if target == nil {
		detailError(w, http.StatusNotFound, "target not found")
		return
	}

	session, closer, cancel, ready := s.buildGraphicalSession(w, r, target)
	if !ready {
		return
	}

	st := s.deps.Hub.Registry.SetDefault(workerID, hub.NewWorkerTermState())
	var mgr *GraphicalSessionManager
	if m, ok := st.GraphicalSession.(*GraphicalSessionManager); ok {
		mgr = m
	} else {
		mgr = NewGraphicalSessionManager()
		st.GraphicalSession = mgr
	}
	mgr.Replace(session, closer, cancel)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "target_id": targetID})
}

// buildGraphicalSession dispatches on the target protocol, returning the live
// session or writing the error response and returning ready=false. memory and
// litevirt are wired; rfb is 501 (this Go port ships no RFB GraphicalSession
// client — a documented gap mirroring C#'s 501 for litevirt).
func (s *Server) buildGraphicalSession(
	w http.ResponseWriter, r *http.Request, target *graphical.Definition,
) (gui.GraphicalSession, io.Closer, context.CancelFunc, bool) {
	protocol := strings.ToLower(strings.TrimSpace(target.Protocol))
	switch protocol {
	case graphical.ProtocolMemory:
		return gui.NewMemoryGraphicalSession(target.Width, target.Height), nil, nil, true
	case graphical.ProtocolLitevirt:
		return s.buildLitevirtSession(w, r, target)
	default:
		// rfb + anything else: unsupported in this port.
		detailError(w, http.StatusNotImplemented, "graphical protocol not supported: "+protocol)
		return nil, nil, nil, false
	}
}

// buildLitevirtSession dials the target's gRPC endpoint and starts the headless
// AI VNC client, now driven entirely from the registry-backed target (endpoint +
// config.vm_name) rather than from client-supplied request fields.
func (s *Server) buildLitevirtSession(
	w http.ResponseWriter, r *http.Request, target *graphical.Definition,
) (gui.GraphicalSession, io.Closer, context.CancelFunc, bool) {
	cc, vmName, ok := s.dialLitevirtTarget(w, r, target)
	if !ok {
		return nil, nil, nil, false
	}

	ctx, cancel := context.WithCancel(context.Background())
	client, err := vnc.NewLitevirtAIClient(ctx, cc, vmName)
	if err != nil {
		cancel()
		_ = cc.Close()
		detailError(w, http.StatusInternalServerError, err.Error())
		return nil, nil, nil, false
	}
	done := make(chan error, 1)
	go func() {
		done <- client.RunHandshakeAndLoop()
	}()
	// Wait for RFB ServerInit readiness (or early failure) before returning ok.
	readyCtx, readyCancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer readyCancel()
	if err := client.WaitReady(readyCtx); err != nil {
		cancel()
		_ = cc.Close()
		// Drain the loop goroutine.
		select {
		case <-done:
		case <-time.After(time.Second):
		}
		detailError(w, http.StatusBadGateway, "gui handshake failed: "+err.Error())
		return nil, nil, nil, false
	}
	// Keep the loop running; cancel closes the stream. Connection close is
	// owned by GraphicalSessionManager via the returned closer.
	go func() {
		err := <-done
		if err != nil {
			s.logger.Warn("gui loop exited", "error", err)
		}
		cancel()
	}()
	return client, cc, cancel, true
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
	if err := png.Encode(&buf, img); err != nil {
		bridgeError(w, http.StatusInternalServerError, "screenshot encode failed")
		return
	}
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
	// Bind inject to the principal that acquired the lease (AcquiredBy),
	// not merely a non-empty hijack_id capability or display Owner string.
	p := principalOf(r)
	if p == nil || hs.AcquiredBy == nil || *hs.AcquiredBy != p.SubjectID {
		bridgeError(w, http.StatusForbidden, "hijack lease not owned by caller")
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
	case "left", "":
		mask = 1
	case "middle":
		mask = 2
	case "right":
		mask = 4
	default:
		detailError(w, http.StatusUnprocessableEntity, "invalid button: must be left, middle, or right")
		return
	}
	if err := sess.InjectPointer(x, y, mask); err != nil {
		bridgeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := sess.InjectPointer(x, y, 0); err != nil {
		bridgeError(w, http.StatusInternalServerError, err.Error())
		return
	}
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
		if err := sess.InjectKey(uint32(r), true); err != nil {
			bridgeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		if err := sess.InjectKey(uint32(r), false); err != nil {
			bridgeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleHijackGUIKey(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	sess, ok := s.requireGraphicalSession(w, r, workerID, hijackID)
	if !ok {
		return
	}
	body, _ := decodeJSONBody(r)
	keyName := stringField(body, "key_name")
	var sym uint32
	switch keyName {
	case "Enter":
		sym = 0xFF0D
	case "Tab":
		sym = 0xFF09
	case "Esc":
		sym = 0xFF1B
	case "Backspace":
		sym = 0xFF08
	case "Up":
		sym = 0xFF52
	case "Down":
		sym = 0xFF54
	case "Left":
		sym = 0xFF51
	case "Right":
		sym = 0xFF53
	default:
		sym = 0
	}
	if err := sess.InjectKey(sym, true); err != nil {
		bridgeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := sess.InjectKey(sym, false); err != nil {
		bridgeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleHijackGUIDrag(w http.ResponseWriter, r *http.Request) {
	workerID, hijackID, ok := bridgeParams(w, r, true)
	if !ok {
		return
	}
	sess, ok := s.requireGraphicalSession(w, r, workerID, hijackID)
	if !ok {
		return
	}
	body, _ := decodeJSONBody(r)
	startX := intField(body, "start_x", 0)
	startY := intField(body, "start_y", 0)
	endX := intField(body, "end_x", 0)
	endY := intField(body, "end_y", 0)
	if err := sess.InjectPointer(startX, startY, 1); err != nil {
		bridgeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := sess.InjectPointer(endX, endY, 1); err != nil {
		bridgeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := sess.InjectPointer(endX, endY, 0); err != nil {
		bridgeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

type GraphicalSessionManager struct {
	mu     sync.RWMutex
	sess   gui.GraphicalSession
	closer io.Closer
	cancel context.CancelFunc
}

func NewGraphicalSessionManager() *GraphicalSessionManager {
	return &GraphicalSessionManager{}
}

func (m *GraphicalSessionManager) Attach(sess gui.GraphicalSession, closer io.Closer, cancel context.CancelFunc) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sess = sess
	m.closer = closer
	m.cancel = cancel
}

func (m *GraphicalSessionManager) Replace(sess gui.GraphicalSession, closer io.Closer, cancel context.CancelFunc) {
	m.Close()
	m.Attach(sess, closer, cancel)
}

func (m *GraphicalSessionManager) Detach() {
	m.Close()
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sess = nil
	m.closer = nil
	m.cancel = nil
}

func (m *GraphicalSessionManager) Close() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.cancel != nil {
		m.cancel()
		m.cancel = nil
	}
	if m.closer != nil {
		m.closer.Close()
		m.closer = nil
	}
	return nil
}

func (m *GraphicalSessionManager) Screenshot() (image.Image, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.sess == nil {
		return nil, fmt.Errorf("no graphical session")
	}
	return m.sess.Screenshot()
}

func (m *GraphicalSessionManager) InjectPointer(x, y int, buttonMask uint8) error {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.sess == nil {
		return fmt.Errorf("no graphical session")
	}
	return m.sess.InjectPointer(x, y, buttonMask)
}

func (m *GraphicalSessionManager) InjectKey(keySym uint32, down bool) error {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.sess == nil {
		return fmt.Errorf("no graphical session")
	}
	return m.sess.InjectKey(keySym, down)
}
