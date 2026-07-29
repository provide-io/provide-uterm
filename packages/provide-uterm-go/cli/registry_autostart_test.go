//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// A configured-but-not-yet-started session is "stopped" — the reference's word
// for a session that has never been brought up (HostedSessionRuntime._state
// starts at "stopped"). "waiting" belongs to the control plane's record store,
// which is a different subsystem and never reaches this field.
func TestSeededSessionIsStoppedNotWaiting(t *testing.T) {
	r := newTestRegistry(t)
	st, err := r.GetSession(context.Background(), "provide-shell")
	if err != nil {
		t.Fatalf("GetSession: %v", err)
	}
	if st.LifecycleState != server.LifecycleStopped {
		t.Fatalf("seeded lifecycle_state = %q, want %q", st.LifecycleState, server.LifecycleStopped)
	}
	if st.Connected {
		t.Fatal("a seeded session has no connector, so it is not connected")
	}
}

// A connector that will not come up leaves the session "stopped", with the
// reason in last_error and the instant in stopped_at — which is where the
// reference's runtime ends up.
//
// HostedSessionRuntime._run (runtime.py, ~425-482) does set _state = "error" on
// a failed run, but only inside the retry loop: a permanent failure breaks out
// of it and the line after the loop assigns "stopped" and _stopped_at. So
// "error" is observable between attempts, never as the resting state of a start
// that gave up. What tells a session that failed apart from one nobody asked to
// run is last_error, not the state.
func TestFailedStartComesToRestStoppedWithTheReason(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	if _, err := r.CreateSession(ctx, map[string]any{"session_id": "bad", "connector_type": "boom"}); err != nil {
		t.Fatalf("create: %v", err)
	}
	st, err := r.StartSession(ctx, "bad")
	if err != nil {
		t.Fatalf("StartSession: %v", err)
	}
	if st.LifecycleState != server.LifecycleStopped {
		t.Fatalf("lifecycle_state = %q, want %q", st.LifecycleState, server.LifecycleStopped)
	}
	if st.LastError == nil || *st.LastError != "dial failed" {
		t.Fatalf("last_error = %v, want the connector's message", st.LastError)
	}
	// Without stopped_at, a client cannot tell this from a session that was
	// seeded and never touched — which is the one thing last_error is for.
	if st.StoppedAt == nil {
		t.Fatal("stopped_at is unset; the reference writes it when the run loop gives up")
	}
	if st.Connected {
		t.Fatal("a session that failed to start is not connected")
	}
}

// One session that will not come up must not stop the rest: the boot loop
// records the failure on that session and carries on.
func TestBootToleratesOneFailingSession(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	first := cfg.Sessions[0]
	broken := first
	broken.SessionID = "broken"
	broken.ConnectorType = "boom"
	broken.AutoStart = true
	later := first
	later.SessionID = "later"
	later.ConnectorType = "shell"
	later.AutoStart = true
	// "broken" is declared before "later" so a boot loop that aborted on the
	// first failure would leave "later" untouched.
	cfg.Sessions = []serverconfig.SessionDefinition{broken, later}
	r := newTestRegistry(t)
	r.entries = map[string]*sessionEntry{}
	r.order = nil
	for _, def := range cfg.Sessions {
		r.seed(def)
	}

	ctx := context.Background()
	r.StartAutoStartSessions(ctx)

	brokenStatus, err := r.GetSession(ctx, "broken")
	if err != nil {
		t.Fatalf("GetSession broken: %v", err)
	}
	if brokenStatus.LifecycleState != server.LifecycleStopped {
		t.Fatalf("broken lifecycle_state = %q, want %q", brokenStatus.LifecycleState, server.LifecycleStopped)
	}
	if brokenStatus.LastError == nil {
		t.Fatal("broken last_error is unset; it is the only thing that says this one failed")
	}
	laterStatus, err := r.GetSession(ctx, "later")
	if err != nil {
		t.Fatalf("GetSession later: %v", err)
	}
	if laterStatus.LifecycleState != server.LifecycleRunning || !laterStatus.Connected {
		t.Fatalf("later = %q/%v, want running/connected — one failure must not abort the batch",
			laterStatus.LifecycleState, laterStatus.Connected)
	}
}

// A session in mid-dial reports "starting": it has been asked to come up and
// the connector has not reported in yet. Without that name, a slow dial is
// indistinguishable from a session nobody ever asked for.
func TestStartReportsStartingWhileTheConnectorDials(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()
	dialing := make(chan struct{})
	release := make(chan struct{})
	r.connect = func(context.Context, serverconfig.SessionDefinition) (connectors.Connector, error) {
		close(dialing)
		<-release
		return newFakeConnector(), nil
	}

	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = r.StartSession(ctx, "provide-shell")
	}()

	<-dialing
	mid, err := r.GetSession(ctx, "provide-shell")
	if err != nil {
		t.Fatalf("GetSession mid-dial: %v", err)
	}
	if mid.LifecycleState != server.LifecycleStarting {
		t.Fatalf("mid-dial lifecycle_state = %q, want %q", mid.LifecycleState, server.LifecycleStarting)
	}
	close(release)
	<-done

	after, err := r.GetSession(ctx, "provide-shell")
	if err != nil {
		t.Fatalf("GetSession after dial: %v", err)
	}
	if after.LifecycleState != server.LifecycleRunning {
		t.Fatalf("post-dial lifecycle_state = %q, want %q", after.LifecycleState, server.LifecycleRunning)
	}
}
