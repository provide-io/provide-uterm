//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"net/http"
	"time"
)

// sseHeartbeatInterval matches the Python _HEARTBEAT_S.
const sseHeartbeatInterval = 15 * time.Second

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
