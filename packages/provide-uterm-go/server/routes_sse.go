//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"net/http"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// sseHeartbeatInterval matches the Python _HEARTBEAT_S. A var so tests can
// observe a heartbeat without waiting 15 s.
var sseHeartbeatInterval = 15 * time.Second

// sseOpenHook runs after a share viewer's stream registers and before its
// cutoff is checked: a test seam for the revoke-in-between race.
var sseOpenHook = func(string) {}

// registerSSERoutes wires the SSE event stream. Port of sse.py.
func (s *Server) registerSSERoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/sessions/{session_id}/events/stream", s.authenticated(s.handleEventStream))
}

func (s *Server) handleEventStream(w http.ResponseWriter, r *http.Request) {
	id, ok := s.readableSession(w, r)
	if !ok {
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")

	flusher, canFlush := w.(http.Flusher)

	bus := s.deps.Hub.EventBus()
	if bus == nil {
		// No event bus configured → an empty stream that closes at once.
		w.WriteHeader(http.StatusOK)
		return
	}
	var eventTypes []string
	if et := r.URL.Query().Get("event_types"); et != "" {
		eventTypes = splitCSV(et)
	}
	var pattern *string
	if pat := r.URL.Query().Get("pattern"); pat != "" {
		pattern = &pat
	}
	// A share viewer's stream is registered so a revoke can end it, then checked
	// once registered: a revoke between the read check and here would otherwise
	// miss it.
	var revoked chan struct{}
	if p := principalOf(r); serverauth.IsShareViewer(p, id) {
		var release func()
		revoked, release = s.viewerStreams.register(id, p)
		defer release()
		sseOpenHook(id)
		if s.deps.Authz.ShareViewerRevoked(p, id) {
			w.WriteHeader(http.StatusOK)
			sseWrite(w, flusher, canFlush, []byte(viewerRevokedEvent))
			return
		}
	}
	sub, cancel, err := bus.Watch(id, eventTypes, pattern)
	if err != nil {
		detailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer cancel()
	w.WriteHeader(http.StatusOK)
	// Flush the headers immediately so the client's response is delivered before
	// the first event (SSE clients block on Do() until headers arrive).
	if canFlush {
		flusher.Flush()
	}

	ticker := time.NewTicker(sseHeartbeatInterval)
	defer ticker.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-revoked:
			sseWrite(w, flusher, canFlush, []byte(viewerRevokedEvent))
			return
		case <-ticker.C:
			if !sseWrite(w, flusher, canFlush, []byte(`{"type":"heartbeat"}`)) {
				return
			}
		case evt, open := <-sub.Queue:
			if !open || evt == nil {
				sseWrite(w, flusher, canFlush, []byte(`{"type":"worker_disconnected"}`))
				return
			}
			data, mErr := json.Marshal(evt)
			if mErr != nil {
				continue
			}
			if !sseWrite(w, flusher, canFlush, data) {
				return
			}
		}
	}
}

// sseWrite writes one `data: <payload>\n\n` SSE frame, flushing when possible.
// Returns false when the write fails (client gone).
func sseWrite(w http.ResponseWriter, flusher http.Flusher, canFlush bool, payload []byte) bool {
	if _, err := w.Write([]byte("data: ")); err != nil {
		return false
	}
	if _, err := w.Write(payload); err != nil {
		return false
	}
	if _, err := w.Write([]byte("\n\n")); err != nil {
		return false
	}
	if canFlush {
		flusher.Flush()
	}
	return true
}
