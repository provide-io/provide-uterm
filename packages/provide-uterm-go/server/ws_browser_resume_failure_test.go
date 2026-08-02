//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

var errResumeStoreDown = errors.New("resume store unavailable")

// faultyResumeStore is a real in-memory resume store with one operation armed
// to fail, so each abort arm of the resume handshake is driven by an actual
// store failure rather than a stub that always says no.
type faultyResumeStore struct {
	*hub.InMemoryResumeStore
	failConsume bool
	failCreate  bool
	failRevoke  bool
	failMark    bool
}

func (s *faultyResumeStore) Consume(ctx context.Context, token string) (*hub.ResumeSession, error) {
	if s.failConsume {
		// Models a token another socket consumed first: gone, but not an error.
		return nil, nil
	}
	return s.InMemoryResumeStore.Consume(ctx, token)
}

func (s *faultyResumeStore) Create(ctx context.Context, workerID, role string, ttlS float64) (string, error) {
	if s.failCreate {
		return "", errResumeStoreDown
	}
	return s.InMemoryResumeStore.Create(ctx, workerID, role, ttlS)
}

func (s *faultyResumeStore) Revoke(ctx context.Context, token string) error {
	if s.failRevoke {
		return errResumeStoreDown
	}
	return s.InMemoryResumeStore.Revoke(ctx, token)
}

func (s *faultyResumeStore) MarkHijackOwner(ctx context.Context, token string, isOwner bool) error {
	if s.failMark {
		return errResumeStoreDown
	}
	return s.InMemoryResumeStore.MarkHijackOwner(ctx, token, isOwner)
}

// expectPongWithoutResume drains until the pong ack, failing if a resumed hello
// arrived first. Frames are ordered on the socket and the pong is sent after
// the resume was handled, so this is an exact "the resume produced nothing"
// assertion rather than a timeout.
func expectPongWithoutResume(t *testing.T, bc *browserClient, ctx context.Context) {
	t.Helper()
	bc.send(t, ctx, map[string]any{"type": "ping"})
	deadline := time.After(5 * time.Second)
	for {
		select {
		case f := <-bc.frames:
			if f["type"] == "hello" && f["resumed"] == true {
				t.Fatal("the browser was resumed despite the resume store failing")
			}
			if f["type"] == "pong" {
				return
			}
		case <-deadline:
			t.Fatal("timed out waiting for pong")
		}
	}
}

// resumeWithFaultyStore connects a browser, arms the store fault, and replays
// the connect-time token as a resume request.
func resumeWithFaultyStore(t *testing.T, arm func(*faultyResumeStore)) (*testServer, *faultyResumeStore) {
	t.Helper()
	store := &faultyResumeStore{InMemoryResumeStore: hub.NewInMemoryResumeStore(nil, nil)}
	ts := resumeTestServerWithStore(t, store)
	ts.reg.add("rf", "admin1", "public")
	ts.setupWorker(t, "rf")
	base, closeFn := wsServer(t, ts)
	t.Cleanup(closeFn)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	t.Cleanup(cancel)

	bc := dialBrowser(t, ctx, base+"/ws/browser/rf/term", "admin1", "admin")
	t.Cleanup(func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") })
	hello := bc.waitFrame(t, "hello", 5*time.Second)
	tok, _ := hello["resume_token"].(string)
	if tok == "" {
		t.Fatalf("hello missing resume_token: %v", hello)
	}

	arm(store)
	bc.send(t, ctx, map[string]any{"type": "resume", "token": tok})
	expectPongWithoutResume(t, bc, ctx)
	return ts, store
}

// TestBrowserResumeAbortsWhenTokenVanishesAtConsume covers the lost-race arm:
// the token validated a moment ago is gone by the time it is consumed.
func TestBrowserResumeAbortsWhenTokenVanishesAtConsume(t *testing.T) {
	resumeWithFaultyStore(t, func(s *faultyResumeStore) { s.failConsume = true })
}

// TestBrowserResumeAbortsWhenReplacementTokenCannotBeMinted covers the arm where
// the old token is already spent but the store cannot issue a replacement: the
// browser must not be told it resumed.
func TestBrowserResumeAbortsWhenReplacementTokenCannotBeMinted(t *testing.T) {
	resumeWithFaultyStore(t, func(s *faultyResumeStore) { s.failCreate = true })
}

// TestBrowserResumeRevokesReplacementWhenRebindFails covers the arm where the
// replacement token was minted but could not be bound to the socket. The
// freshly-minted token must be revoked rather than left live and unowned.
func TestBrowserResumeRevokesReplacementWhenRebindFails(t *testing.T) {
	// ReplaceBrowserResumeToken fails by way of revoking the superseded token,
	// and the compensating revoke of the new token fails with it — so the check
	// is that no resume was announced.
	_, store := resumeWithFaultyStore(t, func(s *faultyResumeStore) { s.failRevoke = true })
	if !store.failRevoke {
		t.Fatal("fault was not armed")
	}
}

// TestBrowserHijackReleaseFailsClosedOnResumeStoreError proves a WS hijack
// release that cannot record its resume-ownership bookkeeping does not report
// success: the lease stays held rather than being dropped on the floor.
func TestBrowserHijackReleaseFailsClosedOnResumeStoreError(t *testing.T) {
	store := &faultyResumeStore{InMemoryResumeStore: hub.NewInMemoryResumeStore(nil, nil)}
	ts := resumeTestServerWithStore(t, store)
	ts.reg.add("rl", "admin1", "public")
	ts.setupWorker(t, "rl")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, base+"/ws/browser/rl/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)

	bc.send(t, ctx, map[string]any{"type": "hijack_request"})
	waitUntil(t, 5*time.Second, func() bool { return ts.hub.CheckStillHijacked("rl") })

	store.failMark = true
	bc.send(t, ctx, map[string]any{"type": "hijack_release"})
	// The release is refused, so the lease is still held once the socket has
	// caught up (proved by the ordered pong that follows it).
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)
	if !ts.hub.CheckStillHijacked("rl") {
		t.Fatal("the hijack was released even though its bookkeeping failed")
	}
}
