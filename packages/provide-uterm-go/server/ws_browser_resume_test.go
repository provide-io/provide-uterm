//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// resumeTestServer builds a testServer whose hub has an in-memory resume store
// so hello minting + browserResume are live.
func resumeTestServer(t *testing.T) *testServer {
	t.Helper()
	store := hub.NewInMemoryResumeStore(nil, nil)
	return newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock:       deps.Clock,
			OnMetric:    deps.Metrics.Inc,
			Logger:      deps.Logger,
			ResumeStore: store,
		})
	})
}

// TestBrowserResumeHappyPath consumes a resume token minted at connect and
// receives a second hello with resumed=true + a fresh token.
func TestBrowserResumeHappyPath(t *testing.T) {
	ts := resumeTestServer(t)
	ts.reg.add("r1", "admin1", "public")
	ts.setupWorker(t, "r1")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, base+"/ws/browser/r1/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	hello := bc.waitFrame(t, "hello", 5*time.Second)
	tok, _ := hello["resume_token"].(string)
	if tok == "" {
		t.Fatalf("hello missing resume_token: %v", hello)
	}
	if hello["resume_supported"] != true {
		t.Fatalf("resume_supported = %v", hello["resume_supported"])
	}

	bc.send(t, ctx, map[string]any{"type": "resume", "token": tok})
	resumed := bc.waitFrameWhere(t, "hello", 5*time.Second, func(f map[string]any) bool {
		return f["resumed"] == true
	})
	newTok, _ := resumed["resume_token"].(string)
	if newTok == "" || newTok == tok {
		t.Fatalf("expected fresh resume token, got %q (old %q)", newTok, tok)
	}
	// Token is single-use — replaying the old one is a silent no-op (no second resumed hello).
	bc.send(t, ctx, map[string]any{"type": "resume", "token": tok})
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)
}

func TestBrowserResumeRestoresDisconnectedCurrentOwner(t *testing.T) {
	ts := resumeTestServer(t)
	ts.reg.add("resume-owner", "admin1", "public")
	ts.setupWorker(t, "resume-owner")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	original := dialBrowser(t, ctx, base+"/ws/browser/resume-owner/term", "admin1", "admin")
	hello := original.waitFrame(t, "hello", 5*time.Second)
	token, _ := hello["resume_token"].(string)
	original.send(t, ctx, map[string]any{"type": "hijack_request"})
	original.waitFrameWhere(t, "hijack_state", 5*time.Second, func(f map[string]any) bool {
		return f["owner"] == "me"
	})
	ownedSession, err := ts.hub.ResumeStore().Get(context.Background(), token)
	if err != nil || ownedSession == nil || !ownedSession.WasHijackOwner {
		t.Fatalf("active token ownership = session:%+v err:%v", ownedSession, err)
	}
	_ = original.conn.Close(websocket.StatusNormalClosure, "")
	waitUntil(t, 5*time.Second, func() bool { return !ts.hub.CheckStillHijacked("resume-owner") })
	stored, err := ts.hub.ResumeStore().Get(context.Background(), token)
	if err != nil || stored == nil || !stored.WasHijackOwner {
		t.Fatalf("disconnect token ownership = session:%+v err:%v", stored, err)
	}

	reconnected := dialBrowser(t, ctx, base+"/ws/browser/resume-owner/term", "admin1", "admin")
	defer func() { _ = reconnected.conn.Close(websocket.StatusNormalClosure, "") }()
	reconnected.waitFrame(t, "hello", 5*time.Second)
	reconnected.send(t, ctx, map[string]any{"type": "resume", "token": token})
	resumed := reconnected.waitFrameWhere(t, "hello", 5*time.Second, func(f map[string]any) bool {
		return f["resumed"] == true
	})
	if resumed["hijacked_by_me"] != true {
		t.Fatalf("resumed owner hello = %v", resumed)
	}
}

func TestBrowserResumeImmediateReconnectWaitsForDisconnectBookkeeping(t *testing.T) {
	ts := resumeTestServer(t)
	ts.reg.add("resume-immediate", "admin1", "public")
	ts.setupWorker(t, "resume-immediate")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	original := dialBrowser(t, ctx, base+"/ws/browser/resume-immediate/term", "admin1", "admin")
	hello := original.waitFrame(t, "hello", 5*time.Second)
	token, _ := hello["resume_token"].(string)
	original.send(t, ctx, map[string]any{"type": "hijack_request"})
	original.waitFrameWhere(t, "hijack_state", 5*time.Second, func(f map[string]any) bool { return f["owner"] == "me" })
	_ = original.conn.Close(websocket.StatusNormalClosure, "")

	reconnected := dialBrowser(t, ctx, base+"/ws/browser/resume-immediate/term", "admin1", "admin")
	defer func() { _ = reconnected.conn.Close(websocket.StatusNormalClosure, "") }()
	reconnected.waitFrame(t, "hello", 5*time.Second)
	reconnected.send(t, ctx, map[string]any{"type": "resume", "token": token})
	resumed := reconnected.waitFrameWhere(t, "hello", 5*time.Second, func(f map[string]any) bool { return f["resumed"] == true })
	if resumed["hijacked_by_me"] != true {
		t.Fatalf("immediate resume did not restore ownership: %v", resumed)
	}
}

func TestBrowserResumeStaleOwnerCannotStealCompetingOwner(t *testing.T) {
	ts := resumeTestServer(t)
	ts.reg.add("resume-competing", "admin1", "public")
	ts.setupWorker(t, "resume-competing")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	original := dialBrowser(t, ctx, base+"/ws/browser/resume-competing/term", "resume-user", "admin")
	hello := original.waitFrame(t, "hello", 5*time.Second)
	token, _ := hello["resume_token"].(string)
	original.send(t, ctx, map[string]any{"type": "hijack_request"})
	original.waitFrameWhere(t, "hijack_state", 5*time.Second, func(f map[string]any) bool {
		return f["owner"] == "me"
	})
	_ = original.conn.Close(websocket.StatusNormalClosure, "")
	waitUntil(t, 5*time.Second, func() bool { return !ts.hub.CheckStillHijacked("resume-competing") })

	competitor := dialBrowser(t, ctx, base+"/ws/browser/resume-competing/term", "new-owner", "admin")
	defer func() { _ = competitor.conn.Close(websocket.StatusNormalClosure, "") }()
	competitor.waitFrame(t, "hello", 5*time.Second)
	competitor.send(t, ctx, map[string]any{"type": "hijack_request"})
	competitor.waitFrameWhere(t, "hijack_state", 5*time.Second, func(f map[string]any) bool {
		return f["owner"] == "me"
	})

	stale := dialBrowser(t, ctx, base+"/ws/browser/resume-competing/term", "resume-user", "admin")
	defer func() { _ = stale.conn.Close(websocket.StatusNormalClosure, "") }()
	stale.waitFrame(t, "hello", 5*time.Second)
	stale.send(t, ctx, map[string]any{"type": "resume", "token": token})
	stale.send(t, ctx, map[string]any{"type": "ping"})
	resumed := false
	deadline := time.After(5 * time.Second)
	for {
		select {
		case frame := <-stale.frames:
			if frame["type"] == "hello" && frame["resumed"] == true {
				resumed = true
			}
			if frame["type"] == "pong" {
				if resumed {
					t.Fatal("stale owner resume unexpectedly succeeded")
				}
				state := ts.hub.Router.HijackStateMsgFor(context.Background(), "resume-competing", nil)
				if state.Owner == nil || *state.Owner != "other" {
					t.Fatalf("competing owner was not preserved: %+v", state)
				}
				return
			}
		case <-deadline:
			t.Fatal("timed out waiting for stale resume result")
		}
	}
}

// TestBrowserResumeEdgeCases covers silent early-return arms (empty token,
// unknown token, wrong worker id).
func TestBrowserResumeEdgeCases(t *testing.T) {
	ts := resumeTestServer(t)
	ts.reg.add("r2", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, base+"/ws/browser/r2/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)

	// Empty token / missing token / garbage — silent no-ops, socket stays live.
	bc.send(t, ctx, map[string]any{"type": "resume"})
	bc.send(t, ctx, map[string]any{"type": "resume", "token": ""})
	bc.send(t, ctx, map[string]any{"type": "resume", "token": "does-not-exist"})
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)
}

// TestBrowserResumeWithoutStore is a silent no-op when ResumeStore is nil.
func TestBrowserResumeWithoutStore(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("r3", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, base+"/ws/browser/r3/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	hello := bc.waitFrame(t, "hello", 5*time.Second)
	if hello["resume_supported"] != false {
		t.Fatalf("resume_supported without store = %v", hello["resume_supported"])
	}
	bc.send(t, ctx, map[string]any{"type": "resume", "token": "x"})
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)
}

// TestBrowserControlMessages covers hijack_step, snapshot_req, owner heartbeat,
// and DeckMux presence_update dispatch arms.
func TestBrowserControlMessages(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("ctl", "admin1", "public")
	ts.setupWorker(t, "ctl")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, base+"/ws/browser/ctl/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)

	// snapshot_req is fire-and-forget (no error response).
	bc.send(t, ctx, map[string]any{"type": "snapshot_req"})

	// Acquire hijack before owner-only controls, then step and heartbeat.
	bc.send(t, ctx, map[string]any{"type": "hijack_request"})
	bc.waitFrameWhere(t, "hijack_state", 5*time.Second, func(f map[string]any) bool {
		return f["hijacked"] == true
	})
	bc.send(t, ctx, map[string]any{"type": "hijack_step"})
	bc.send(t, ctx, map[string]any{"type": "heartbeat"})
	bc.waitFrame(t, "heartbeat_ack", 5*time.Second)

	// DeckMux presence_update (anonymous principal under normal mode still works
	// via per-connection AnonID when principal is set; admin principal is fine).
	bc.send(t, ctx, map[string]any{
		"type":   "presence_update",
		"fields": map[string]any{"typing": true},
	})
	// Release and confirm live.
	bc.send(t, ctx, map[string]any{"type": "hijack_release"})
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)
}

// TestBrowserWSTestMode covers the UTERM_TEST_MODE=1 open-admin path.
func TestBrowserWSTestMode(t *testing.T) {
	t.Setenv("UTERM_TEST_MODE", "1")
	// Ensure env is restored even if the package has parallel tests later.
	t.Cleanup(func() { _ = os.Unsetenv("UTERM_TEST_MODE") })

	ts := newTestServer(t, nil)
	// No session definition needed — test mode short-circuits role resolution.
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	// Dial without auth headers — test mode injects test-admin.
	conn, _, err := websocket.Dial(ctx, base+"/ws/browser/ghost-worker/term", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	conn.SetReadLimit(1 << 20)
	bc := &browserClient{conn: conn, frames: make(chan map[string]any, 64), data: make(chan string, 64)}
	go bc.readLoop(ctx)
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()

	hello := bc.waitFrame(t, "hello", 5*time.Second)
	if hello["role"] != "admin" {
		t.Fatalf("test mode role = %v", hello["role"])
	}
	// deckPrincipal returns nil under UTERM_TEST_MODE (AnonID path).
	bc.send(t, ctx, map[string]any{"type": "presence_update", "fields": map[string]any{"typing": false}})
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)
}
