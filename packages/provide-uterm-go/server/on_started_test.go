//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// The boot hook runs to completion before the server starts answering, so the
// first client to be served sees a settled server rather than one mid-boot.
func TestOnStartedRunsBeforeServing(t *testing.T) {
	ran := make(chan struct{})
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.OnStarted = func(context.Context) { close(ran) }
	})
	ts.srv.runOnStarted(context.Background(), time.Second)
	select {
	case <-ran:
	default:
		t.Fatal("runOnStarted returned before the hook ran")
	}
}

// No hook is not an error, and costs nothing.
func TestOnStartedIsOptional(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.runOnStarted(context.Background(), time.Millisecond)
}

// A hook that overruns must not keep the server shut: one connector that will
// not dial cannot leave every route unanswered. The hook is left to finish in
// the background.
func TestOnStartedDoesNotBlockServingForever(t *testing.T) {
	release := make(chan struct{})
	defer close(release)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.OnStarted = func(context.Context) { <-release }
	})
	start := time.Now()
	ts.srv.runOnStarted(context.Background(), 10*time.Millisecond)
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("runOnStarted waited %v, want the timeout to cut it short", elapsed)
	}
}

// A cancelled context abandons the wait immediately — the caller is shutting
// down and has no use for a boot step.
func TestOnStartedStopsWaitingWhenCancelled(t *testing.T) {
	release := make(chan struct{})
	defer close(release)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.OnStarted = func(context.Context) { <-release }
	})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	start := time.Now()
	ts.srv.runOnStarted(ctx, time.Minute)
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Fatalf("runOnStarted waited %v after cancellation", elapsed)
	}
}
