//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"errors"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// newTestRegistry builds a registry with a scripted connect hook: connector_type
// "boom" errors, "none" yields no live connector, anything else yields a fake
// connected connector.
func newTestRegistry(t *testing.T) *SessionRegistryImpl {
	t.Helper()
	cfg := serverconfig.DefaultServerConfig()
	cfg.Recording.EnabledByDefault = true
	r := NewSessionRegistry(cfg)
	r.connect = func(_ context.Context, def serverconfig.SessionDefinition) (connectors.Connector, error) {
		switch def.ConnectorType {
		case "boom":
			return nil, errors.New("dial failed")
		case "none":
			return nil, nil
		default:
			return newFakeConnector(), nil
		}
	}
	return r
}

func TestRegistrySeedAndList(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	items := r.ListWithDefinitions(ctx)
	if len(items) != 1 {
		t.Fatalf("want 1 seeded session, got %d", len(items))
	}
	if items[0].Status.SessionID != "provide-shell" {
		t.Fatalf("unexpected seed: %s", items[0].Status.SessionID)
	}
	// Recording default propagates.
	if !items[0].Status.RecordingEnabled {
		t.Error("recording should default to enabled")
	}
	// Seeding a duplicate id is a no-op.
	r.seed(*items[0].Definition)
	if got := len(r.ListWithDefinitions(ctx)); got != 1 {
		t.Fatalf("duplicate seed changed count to %d", got)
	}
}

func TestStartAutoStartSessions(t *testing.T) {
	r := newTestRegistry(t) // DefaultServerConfig seeds provide-shell with auto_start=true
	ctx := context.Background()

	// Bug baseline: a freshly seeded auto_start session is waiting/disconnected
	// (NewSessionRegistry does not spawn it — the boot step is what does).
	st, err := r.GetSession(ctx, "provide-shell")
	if err != nil {
		t.Fatalf("GetSession: %v", err)
	}
	if st.LifecycleState != "waiting" || st.Connected {
		t.Fatalf("pre-boot want waiting/disconnected, got %s/%v", st.LifecycleState, st.Connected)
	}

	// A session without auto_start must be left untouched by the boot step.
	if _, err := r.CreateSession(ctx, map[string]any{"session_id": "manual", "connector_type": "shell"}); err != nil {
		t.Fatalf("create manual: %v", err)
	}

	r.StartAutoStartSessions(ctx)

	st, err = r.GetSession(ctx, "provide-shell")
	if err != nil {
		t.Fatalf("GetSession after boot: %v", err)
	}
	if st.LifecycleState != "running" || !st.Connected {
		t.Fatalf("auto_start session: want running/connected, got %s/%v", st.LifecycleState, st.Connected)
	}

	manual, err := r.GetSession(ctx, "manual")
	if err != nil {
		t.Fatalf("GetSession manual: %v", err)
	}
	if manual.LifecycleState != "waiting" || manual.Connected {
		t.Fatalf("manual session should stay waiting, got %s/%v", manual.LifecycleState, manual.Connected)
	}
}

func TestRegistryGetDefinitionAndSession(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	if _, ok := r.GetDefinition(ctx, "provide-shell"); !ok {
		t.Fatal("expected definition")
	}
	if _, ok := r.GetDefinition(ctx, "missing"); ok {
		t.Fatal("unexpected definition")
	}
	if _, err := r.GetSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("GetSession: %v", err)
	}
	if _, err := r.GetSession(ctx, "missing"); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("want ErrSessionNotFound, got %v", err)
	}
}

func TestCreateSessionInternalBypassesEgress(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	// A websocket session with no connector_config.url: the egress chokepoint
	// rejects it, but an inbound tunnel placeholder is server-minted and never
	// dialed, so CreateSessionInternal must skip that check.
	payload := map[string]any{
		"session_id":       "tunnel-x",
		"connector_type":   "websocket",
		"connector_config": map[string]any{"tunnel_type": "terminal"},
	}
	if _, err := r.CreateSession(ctx, payload); err == nil {
		t.Fatal("CreateSession should egress-reject a websocket session with no url")
	}
	st, err := r.CreateSessionInternal(ctx, payload)
	if err != nil {
		t.Fatalf("CreateSessionInternal: %v", err)
	}
	if st.SessionID != "tunnel-x" || st.LifecycleState != "waiting" {
		t.Fatalf("unexpected status: %+v", st)
	}
}

func TestRegistryCreateValidation(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()

	if _, err := r.CreateSession(ctx, map[string]any{}); err == nil {
		t.Fatal("expected validation error for missing session_id")
	} else if _, ok := err.(*server.SessionValidationError); !ok {
		t.Fatalf("want SessionValidationError, got %T", err)
	}

	if _, err := r.CreateSession(ctx, map[string]any{"session_id": "bad id!"}); err == nil {
		t.Fatal("expected invalid id error")
	}

	st, err := r.CreateSession(ctx, map[string]any{"session_id": "s1", "connector_type": "telnet"})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if st.SessionID != "s1" || st.ConnectorType != "telnet" {
		t.Fatalf("unexpected status: %+v", st)
	}
	// Duplicate → conflict.
	if _, err := r.CreateSession(ctx, map[string]any{"session_id": "s1"}); err == nil {
		t.Fatal("expected conflict")
	} else if _, ok := err.(*server.SessionConflictError); !ok {
		t.Fatalf("want SessionConflictError, got %T", err)
	}
}

func TestRegistryCreateDefaults(t *testing.T) {
	r := newTestRegistry(t)
	st, err := r.CreateSession(context.Background(), map[string]any{
		"session_id":       "d1",
		"connector_config": map[string]any{"host": "h"},
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if st.ConnectorType != "shell" || st.InputMode != "open" || st.Visibility != "public" || st.DisplayName != "d1" {
		t.Fatalf("defaults not applied: %+v", st)
	}
}

func TestRegistryUpdate(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	if _, err := r.UpdateSession(ctx, "missing", nil); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("want not found, got %v", err)
	}
	st, err := r.UpdateSession(ctx, "provide-shell", map[string]any{
		"display_name": "Renamed", "input_mode": "hijack", "visibility": "private", "tags": []string{"x"},
	})
	if err != nil {
		t.Fatalf("update: %v", err)
	}
	if st.DisplayName != "Renamed" || st.InputMode != "hijack" || st.Visibility != "private" {
		t.Fatalf("update not applied: %+v", st)
	}
	if _, err := r.UpdateSession(ctx, "provide-shell", map[string]any{"input_mode": "bogus"}); err == nil {
		t.Fatal("expected input_mode validation error")
	}
}

func TestRegistryLifecycleTransitions(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()

	// Start a live (fake) connector.
	st, err := r.StartSession(ctx, "provide-shell")
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	if st.LifecycleState != "running" || !st.Connected {
		t.Fatalf("expected running+connected, got %+v", st)
	}
	// Starting again while connected is a no-op returning running.
	if st2, _ := r.StartSession(ctx, "provide-shell"); st2.LifecycleState != "running" {
		t.Fatalf("restart-idempotent failed: %+v", st2)
	}

	// Stop transitions to stopped and sets stopped_at.
	st, err = r.StopSession(ctx, "provide-shell")
	if err != nil {
		t.Fatalf("stop: %v", err)
	}
	if st.LifecycleState != "stopped" || st.Connected || st.StoppedAt == nil {
		t.Fatalf("expected stopped, got %+v", st)
	}

	// Restart brings it back.
	st, err = r.RestartSession(ctx, "provide-shell")
	if err != nil {
		t.Fatalf("restart: %v", err)
	}
	if st.LifecycleState != "running" {
		t.Fatalf("expected running after restart, got %+v", st)
	}
}

func TestRegistryStartErrorAndNoConnector(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()

	_, _ = r.CreateSession(ctx, map[string]any{"session_id": "boom", "connector_type": "boom"})
	st, err := r.StartSession(ctx, "boom")
	if err != nil {
		t.Fatalf("start boom: %v", err)
	}
	if st.LifecycleState != "stopped" || st.LastError == nil {
		t.Fatalf("expected stopped+lastError, got %+v", st)
	}

	_, _ = r.CreateSession(ctx, map[string]any{"session_id": "none", "connector_type": "none"})
	st, err = r.StartSession(ctx, "none")
	if err != nil {
		t.Fatalf("start none: %v", err)
	}
	if st.LifecycleState != "running" || st.Connected {
		t.Fatalf("expected running+not-connected, got %+v", st)
	}

	if _, err := r.StartSession(ctx, "missing"); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("want not found, got %v", err)
	}
}

func TestRegistryStopRestartSetModeNotFound(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	if _, err := r.StopSession(ctx, "missing"); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("stop: %v", err)
	}
	if _, err := r.RestartSession(ctx, "missing"); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("restart: %v", err)
	}
	if _, err := r.SetMode(ctx, "missing", "open"); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("setmode: %v", err)
	}
}

func TestRegistrySetModeAndClear(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	if _, err := r.SetMode(ctx, "provide-shell", "bogus"); err == nil {
		t.Fatal("expected invalid mode error")
	}
	st, err := r.SetMode(ctx, "provide-shell", "hijack")
	if err != nil || st.InputMode != "hijack" {
		t.Fatalf("setmode: %v %+v", err, st)
	}
	// Clear with a live session.
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("start: %v", err)
	}
	if _, err := r.ClearSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("clear: %v", err)
	}
	if _, err := r.ClearSession(ctx, "missing"); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("clear missing: %v", err)
	}
}

func TestRegistryOpaqueAndAnnotate(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()

	if _, err := r.AnalyzeSession(ctx, "missing"); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("analyze: %v", err)
	}
	if a, err := r.AnalyzeSession(ctx, "provide-shell"); err != nil || a["session_id"] != "provide-shell" {
		t.Fatalf("analyze: %v %v", err, a)
	}

	// No live connector → nil snapshot, ErrNoRuntime on annotate.
	if snap, err := r.LastSnapshot(ctx, "provide-shell"); err != nil || snap != nil {
		t.Fatalf("snapshot want nil: %v %v", snap, err)
	}
	if _, _, err := r.AnnotateSession(ctx, "provide-shell", server.Annotation{}); !errors.Is(err, server.ErrNoRuntime) {
		t.Fatalf("annotate want ErrNoRuntime, got %v", err)
	}

	// With a live connector.
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("start: %v", err)
	}
	snap, err := r.LastSnapshot(ctx, "provide-shell")
	if err != nil || snap == nil {
		t.Fatalf("snapshot want map: %v %v", snap, err)
	}
	ts, seq, err := r.AnnotateSession(ctx, "provide-shell", server.Annotation{Label: "l"})
	if err != nil || seq != 1 || ts <= 0 {
		t.Fatalf("annotate: %v %d %v", ts, seq, err)
	}

	if _, err := r.Events(ctx, "provide-shell", 10); err != nil {
		t.Fatalf("events: %v", err)
	}
	if _, err := r.Events(ctx, "missing", 10); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("events missing: %v", err)
	}
	if _, err := r.WatchSessionEvents(ctx, "provide-shell", server.WatchParams{}); err != nil {
		t.Fatalf("watch: %v", err)
	}
	if _, err := r.WatchSessionEvents(ctx, "missing", server.WatchParams{}); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("watch missing: %v", err)
	}
	if _, _, err := r.AnnotateSession(ctx, "missing", server.Annotation{}); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("annotate missing: %v", err)
	}
}

func TestRegistryStartDeletedMidConnect(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	r := NewSessionRegistry(cfg)
	ctx := context.Background()
	// The connect hook deletes the entry before returning a live session,
	// simulating a concurrent delete: Start must not resurrect it and must
	// close the orphaned session.
	r.connect = func(_ context.Context, def serverconfig.SessionDefinition) (connectors.Connector, error) {
		_ = r.DeleteSession(ctx, def.SessionID)
		return newFakeConnector(), nil
	}
	if _, err := r.StartSession(ctx, "provide-shell"); !errors.Is(err, server.ErrSessionNotFound) {
		t.Fatalf("want ErrSessionNotFound after mid-connect delete, got %v", err)
	}
}

func TestRegistryDelete(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("start: %v", err)
	}
	if err := r.DeleteSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, ok := r.GetDefinition(ctx, "provide-shell"); ok {
		t.Fatal("expected deletion")
	}
	// Idempotent.
	if err := r.DeleteSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("delete idempotent: %v", err)
	}
}
