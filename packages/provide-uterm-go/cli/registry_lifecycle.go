//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/json"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
)

// StartSession opens the session's connector (telnet/ws get a live termsession;
// other types become running-but-not-connected). Unknown id → 404.
func (r *SessionRegistryImpl) StartSession(ctx context.Context, id string) (*server.SessionStatus, error) {
	r.mu.Lock()
	e, ok := r.entries[id]
	if !ok {
		r.mu.Unlock()
		return nil, server.ErrSessionNotFound
	}
	if e.session != nil && e.session.IsConnected() {
		st := r.snapshotStatus(e)
		r.mu.Unlock()
		return st, nil
	}
	def := e.def
	connect := r.connect
	r.mu.Unlock()

	sess, err := connect(ctx, def)

	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok = r.entries[id]
	if !ok {
		if sess != nil {
			_ = sess.Close(ctx)
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
	e.session = sess // may be nil for "no live connector" types
	e.lifecycle = "running"
	return r.snapshotStatus(e), nil
}

// StopSession tears down any live connector and marks the session stopped.
func (r *SessionRegistryImpl) StopSession(ctx context.Context, id string) (*server.SessionStatus, error) {
	r.mu.Lock()
	e, ok := r.entries[id]
	if !ok {
		r.mu.Unlock()
		return nil, server.ErrSessionNotFound
	}
	sess := e.session
	e.session = nil
	e.lifecycle = "stopped"
	now := float64(time.Now().UnixNano()) / 1e9
	e.stoppedAt = &now
	st := r.snapshotStatus(e)
	r.mu.Unlock()
	if sess != nil {
		_ = sess.Close(ctx)
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
	if e.session != nil {
		e.session.Emulator().Process([]byte("\x1b[2J\x1b[H"))
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
	connected := e.session != nil && e.session.IsConnected()
	return map[string]any{
		"session_id":     id,
		"connected":      connected,
		"connector_type": e.def.ConnectorType,
		"lifecycle":      e.lifecycle,
	}, nil
}

// LastSnapshot returns the latest emulator snapshot, or nil when no live
// connector exists.
func (r *SessionRegistryImpl) LastSnapshot(_ context.Context, id string) (map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok || e.session == nil {
		return nil, nil
	}
	snap := e.session.Snapshot()
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

// Events returns recent events (this minimal registry keeps none, so empty).
func (r *SessionRegistryImpl) Events(_ context.Context, id string, _ int) ([]map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.entries[id]; !ok {
		return nil, server.ErrSessionNotFound
	}
	return []map[string]any{}, nil
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
	if e.session == nil {
		return 0, 0, server.ErrNoRuntime
	}
	e.annSeq++
	ts := float64(time.Now().UnixNano()) / 1e9
	return ts, e.annSeq, nil
}

// defaultConnect builds a live connector for telnet/websocket sessions; every
// other connector type yields (nil, nil) — a running-but-not-connected status.
func defaultConnect(ctx context.Context, def serverconfig.SessionDefinition) (*termsession.TransportSession, error) {
	switch def.ConnectorType {
	case "telnet":
		host := configStr(def.ConnectorConfig, "host", "localhost")
		port := configInt(def.ConnectorConfig, "port", 23)
		return termsession.ConnectTelnet(ctx, host, port, termsession.TelnetOptions{})
	case "websocket":
		url := configStr(def.ConnectorConfig, "url", "")
		return termsession.ConnectWS(ctx, url, termsession.WSOptions{})
	default:
		return nil, nil
	}
}

// configStr reads a string connector-config value with a fallback.
func configStr(cc map[string]any, key, fallback string) string {
	if cc == nil {
		return fallback
	}
	if v, ok := cc[key].(string); ok && v != "" {
		return v
	}
	return fallback
}

// configInt reads an int connector-config value (int / int64 / float64) with a
// fallback.
func configInt(cc map[string]any, key string, fallback int) int {
	if cc == nil {
		return fallback
	}
	switch v := cc[key].(type) {
	case int:
		return v
	case int64:
		return int(v)
	case float64:
		return int(v)
	}
	return fallback
}
