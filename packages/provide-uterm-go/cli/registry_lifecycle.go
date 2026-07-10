//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/json"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// StartSession builds and starts the session's real connector (shell PTY / ssh /
// telnet / websocket). Unknown id → 404. A build/dial error is recorded as
// last_error and leaves the session stopped rather than failing the request.
func (r *SessionRegistryImpl) StartSession(ctx context.Context, id string) (*server.SessionStatus, error) {
	r.mu.Lock()
	e, ok := r.entries[id]
	if !ok {
		r.mu.Unlock()
		return nil, server.ErrSessionNotFound
	}
	if e.conn != nil && e.conn.IsConnected() {
		st := r.snapshotStatus(e)
		r.mu.Unlock()
		return st, nil
	}
	def := e.def
	connect := r.connect
	r.mu.Unlock()

	conn, err := connect(ctx, def)

	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok = r.entries[id]
	if !ok {
		if conn != nil {
			_ = conn.Stop(ctx)
		}
		return nil, server.ErrSessionNotFound
	}
	e.stoppedAt = nil
	if err != nil {
		msg := err.Error()
		e.lastErr = &msg
		e.lifecycle = "stopped"
		return r.snapshotStatus(e), nil
	}
	e.lastErr = nil
	e.conn = conn // may be nil for "no live connector" types
	e.lifecycle = "running"
	return r.snapshotStatus(e), nil
}

// StartAutoStartSessions starts every session flagged auto_start, mirroring the
// Python registry bootstrap (registry.start_auto_start_sessions). Connector
// failures are recorded as each session's last_error by StartSession rather than
// aborting the batch, so one bad session never blocks the others. It is invoked
// once at server boot (see runServer) — NewSessionRegistry only seeds sessions
// as waiting; nothing spawns them until this runs.
func (r *SessionRegistryImpl) StartAutoStartSessions(ctx context.Context) {
	r.mu.Lock()
	ids := make([]string, 0, len(r.order))
	for _, id := range r.order {
		if r.entries[id].def.AutoStart {
			ids = append(ids, id)
		}
	}
	r.mu.Unlock()
	for _, id := range ids {
		_, _ = r.StartSession(ctx, id)
	}
}

// StopSession tears down any live connector and marks the session stopped.
func (r *SessionRegistryImpl) StopSession(ctx context.Context, id string) (*server.SessionStatus, error) {
	r.mu.Lock()
	e, ok := r.entries[id]
	if !ok {
		r.mu.Unlock()
		return nil, server.ErrSessionNotFound
	}
	conn := e.conn
	e.conn = nil
	e.lifecycle = "stopped"
	now := float64(time.Now().UnixNano()) / 1e9
	e.stoppedAt = &now
	st := r.snapshotStatus(e)
	r.mu.Unlock()
	if conn != nil {
		_ = conn.Stop(ctx)
	}
	return st, nil
}

// RestartSession stops then starts the session.
func (r *SessionRegistryImpl) RestartSession(ctx context.Context, id string) (*server.SessionStatus, error) {
	if _, err := r.StopSession(ctx, id); err != nil {
		return nil, err
	}
	return r.StartSession(ctx, id)
}

// SetMode switches a session's input mode ("hijack"/"open"). Unknown id → 404.
func (r *SessionRegistryImpl) SetMode(_ context.Context, id, mode string) (*server.SessionStatus, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok {
		return nil, server.ErrSessionNotFound
	}
	if mode != "hijack" && mode != "open" {
		return nil, &server.SessionValidationError{Msg: "invalid input_mode: " + mode}
	}
	e.inputMode = mode
	e.def.InputMode = mode
	if e.conn != nil {
		_ = e.conn.SetMode(mode) // mode already validated above
	}
	return r.snapshotStatus(e), nil
}

// ClearSession clears the emulated screen when a live connector exists.
func (r *SessionRegistryImpl) ClearSession(_ context.Context, id string) (*server.SessionStatus, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok {
		return nil, server.ErrSessionNotFound
	}
	if e.conn != nil {
		_ = e.conn.Clear()
	}
	return r.snapshotStatus(e), nil
}

// AnalyzeSession returns an opaque analysis object. Unknown id → 404.
func (r *SessionRegistryImpl) AnalyzeSession(_ context.Context, id string) (map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok {
		return nil, server.ErrSessionNotFound
	}
	connected := e.conn != nil && e.conn.IsConnected()
	out := map[string]any{
		"session_id":     id,
		"connected":      connected,
		"connector_type": e.def.ConnectorType,
		"lifecycle":      e.lifecycle,
	}
	if e.conn != nil {
		out["analysis"] = e.conn.Analysis()
	}
	return out, nil
}

// LastSnapshot returns the latest emulator snapshot, or nil when no live
// connector exists.
func (r *SessionRegistryImpl) LastSnapshot(_ context.Context, id string) (map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok || e.conn == nil {
		return nil, nil
	}
	snap := e.conn.Snapshot()
	raw, err := json.Marshal(snap)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// Events returns up to limit recent raw-output events buffered by the live
// connector (empty when no connector is running). Unknown id → 404.
func (r *SessionRegistryImpl) Events(_ context.Context, id string, limit int) ([]map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok {
		return nil, server.ErrSessionNotFound
	}
	if e.conn == nil {
		return []map[string]any{}, nil
	}
	events := e.conn.Events()
	if limit > 0 && len(events) > limit {
		events = events[len(events)-limit:]
	}
	if events == nil {
		events = []map[string]any{}
	}
	return events, nil
}

// WatchSessionEvents long-polls for events. With no event source, it returns an
// immediate empty batch.
func (r *SessionRegistryImpl) WatchSessionEvents(_ context.Context, id string, _ server.WatchParams) (map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.entries[id]; !ok {
		return nil, server.ErrSessionNotFound
	}
	return map[string]any{"events": []map[string]any{}, "timed_out": true}, nil
}

// AnnotateSession records an operator annotation, returning (ts, seq). A session
// with no live runtime → ErrNoRuntime.
func (r *SessionRegistryImpl) AnnotateSession(_ context.Context, id string, _ server.Annotation) (float64, int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok {
		return 0, 0, server.ErrSessionNotFound
	}
	if e.conn == nil {
		return 0, 0, server.ErrNoRuntime
	}
	e.annSeq++
	ts := float64(time.Now().UnixNano()) / 1e9
	return ts, e.annSeq, nil
}

// defaultConnect builds the real connector for a session definition from the
// connectors package and starts it. Every builtin type (shell/ssh/telnet/
// websocket) yields a live connector; a build error (unknown type, bad config)
// or a dial error surfaces to StartSession as last_error.
func defaultConnect(ctx context.Context, def serverconfig.SessionDefinition) (connectors.Connector, error) {
	conn, err := connectors.Build(def.SessionID, def.DisplayName, def.ConnectorType, def.ConnectorConfig)
	if err != nil {
		return nil, err
	}
	if err := conn.Start(ctx); err != nil {
		return nil, err
	}
	return conn, nil
}
