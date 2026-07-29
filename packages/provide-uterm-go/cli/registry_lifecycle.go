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
// telnet / websocket). Unknown id → 404.
//
// The states follow HostedSessionRuntime: "starting" for as long as the dial is
// in flight, then "running" — or, when the dial fails, "stopped" with the
// reason in last_error and the instant in stopped_at.
//
// "stopped", not "error", is where the reference comes to rest. Its _run loop
// (runtime.py, ~425-482) does assign _state = "error" on a failed run, but that
// is a state *between retry attempts*: a permanent failure breaks out of the
// loop and the line after it assigns "stopped" and _stopped_at, while a
// transient one sleeps a backoff and sets "starting" again at the top. Nothing
// ever rests at "error". A client tells a session that tried and failed apart
// from one nobody asked to run by reading last_error, not the state — which is
// why last_error and stopped_at are both written here.
//
// A dial failure is reported on the session rather than as a failed request, so
// one unreachable target does not turn into an HTTP error for the operator who
// asked for it — they get a session that says what went wrong.
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
	// Published before the lock is dropped, so a concurrent GET during a slow
	// dial sees "starting" rather than the state the session had before.
	e.lifecycle = server.LifecycleStarting
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
		// Where the reference's run loop lands when it gives up: stopped, with
		// the reason and the instant. See the doc comment above for why this is
		// not "error".
		e.lifecycle = server.LifecycleStopped
		now := float64(time.Now().UnixNano()) / 1e9
		e.stoppedAt = &now
		return r.snapshotStatus(e), nil
	}
	e.lastErr = nil
	e.conn = conn // may be nil for "no live connector" types
	e.lifecycle = server.LifecycleRunning
	// Attach the session to the hub, the second half of what the reference's
	// HostedSessionRuntime does on start: without a worker socket the hub has
	// nothing to pause, and every hijack acquire is refused "no_worker".
	r.startWorkerBridge(e)
	return r.snapshotStatus(e), nil
}

// StartAutoStartSessions brings up every session flagged auto_start, mirroring
// the Python registry bootstrap (registry.start_auto_start_sessions) and the C#
// port's boot step. A port that stores the flag and never acts on it reports a
// never-started session to every client while still echoing auto_start: true on
// the wire.
//
// Connector failures are recorded on each session by StartSession (stopped plus
// last_error) rather than aborting the batch, so one bad session never blocks
// the others.
//
// It runs once, when the server is listening — see server.Deps.OnStarted, which
// is the single place both `uterm server` and the live-conformance server boot
// through. NewSessionRegistry only seeds sessions as stopped; nothing spawns
// them until this runs.
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
	br := takeBridge(e)
	e.conn = nil
	e.lifecycle = server.LifecycleStopped
	now := float64(time.Now().UnixNano()) / 1e9
	e.stoppedAt = &now
	st := r.snapshotStatus(e)
	r.mu.Unlock()
	// Detach from the hub before the connector goes: a worker whose socket
	// outlived its terminal would still look leasable.
	stopBridge(br)
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
//
// The hub is updated too, and synchronously: it holds its own copy of the mode,
// and that is the copy the REST hijack acquire path reads. Leaving it to reach
// the hub over the worker socket would make a lease taken right after a mode
// change depend on how fast a WebSocket round-trip happened to be.
func (r *SessionRegistryImpl) SetMode(ctx context.Context, id, mode string) (*server.SessionStatus, error) {
	r.mu.Lock()
	e, ok := r.entries[id]
	if !ok {
		r.mu.Unlock()
		return nil, server.ErrSessionNotFound
	}
	if mode != "hijack" && mode != "open" {
		r.mu.Unlock()
		return nil, &server.SessionValidationError{Msg: "invalid input_mode: " + mode}
	}
	e.inputMode = mode
	e.def.InputMode = mode
	if e.conn != nil {
		_ = e.conn.SetMode(mode) // mode already validated above
	}
	st := r.snapshotStatus(e)
	r.mu.Unlock()
	r.syncHubInputMode(ctx, id, mode)
	return st, nil
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

// WatchSessionEvents long-polls the hub EventBus for session events (Python
// watch_session_events / C# EventBus.WatchAsync). Without a bus, falls back to
// the connector event ring (or empty) without waiting.
func (r *SessionRegistryImpl) WatchSessionEvents(ctx context.Context, id string, p server.WatchParams) (map[string]any, error) {
	r.mu.Lock()
	e, ok := r.entries[id]
	if !ok {
		r.mu.Unlock()
		return nil, server.ErrSessionNotFound
	}
	bus := r.eventBus
	conn := e.conn
	r.mu.Unlock()

	timeoutMS := p.TimeoutMS
	if timeoutMS <= 0 {
		timeoutMS = 5000
	}
	if timeoutMS < 100 {
		timeoutMS = 100
	}
	if timeoutMS > 30000 {
		timeoutMS = 30000
	}
	maxEvents := p.MaxEvents
	if maxEvents <= 0 {
		maxEvents = 50
	}
	if maxEvents > 200 {
		maxEvents = 200
	}

	if bus == nil {
		// No EventBus: return recent connector ring immediately (no long-poll).
		events := []map[string]any{}
		if conn != nil {
			events = conn.Events()
			if len(events) > maxEvents {
				events = events[len(events)-maxEvents:]
			}
		}
		return map[string]any{
			"session_id":    id,
			"events":        events,
			"dropped_count": 0,
			"timed_out":     len(events) == 0,
		}, nil
	}

	var pattern *string
	if p.Pattern != "" {
		pat := p.Pattern
		pattern = &pat
	}
	sub, cancel, err := bus.Watch(id, p.EventTypes, pattern)
	if err != nil {
		return nil, err
	}
	defer cancel()

	collected := make([]map[string]any, 0, maxEvents)
	// Bootstrap from connector ring so a live shell without a WS worker still
	// surfaces recent output on the first watch.
	if conn != nil {
		for _, ev := range conn.Events() {
			collected = append(collected, ev)
			if len(collected) >= maxEvents {
				return map[string]any{
					"session_id":    id,
					"events":        collected,
					"dropped_count": 0,
					"timed_out":     false,
				}, nil
			}
		}
	}
	timedOut := false
	deadline := time.Now().Add(time.Duration(timeoutMS) * time.Millisecond)
	for len(collected) < maxEvents {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			timedOut = true
			break
		}
		timer := time.NewTimer(remaining)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case evt, open := <-sub.Queue:
			timer.Stop()
			if !open || evt == nil {
				// worker-disconnected sentinel or closed queue
				return map[string]any{
					"session_id":    id,
					"events":        collected,
					"dropped_count": sub.Dropped(),
					"timed_out":     false,
				}, nil
			}
			collected = append(collected, evt)
		case <-timer.C:
			timedOut = true
		}
		if timedOut {
			break
		}
	}
	return map[string]any{
		"session_id":    id,
		"events":        collected,
		"dropped_count": sub.Dropped(),
		"timed_out":     timedOut,
	}, nil
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
