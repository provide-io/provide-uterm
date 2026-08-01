//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestRegisterWorkerNew(t *testing.T) {
	h, _ := newTestHub(t, nil)
	prev, err := h.Conn.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustEqual(t, err, nil, "no err")
	mustFalse(t, prev, "new worker not previously hijacked")
	mustTrue(t, h.registry.Contains("w1"), "worker registered")
}

func TestRegisterWorkerExpiredSessionCleared(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := NewWorkerTermState()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() - 1}
	h.registry.Put("w1", st)
	prev, _ := h.Conn.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustTrue(t, prev, "expired session -> prev hijacked")
	mustEqual(t, st.HijackSession, (*HijackSession)(nil), "expired session cleared")
}

func TestRegisterWorkerActiveRESTSessionRejectsReplacement(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := NewWorkerTermState()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 100}
	h.registry.Put("w1", st)
	prev, err := h.Conn.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustFalse(t, prev, "replacement rejected before publishing")
	var rejection *WebSocketRejection
	if !errors.As(err, &rejection) || rejection.Code != 1008 {
		t.Fatalf("active REST replacement error = %v, want policy rejection", err)
	}
	if st.HijackSession == nil {
		t.Fatal("active session must survive rejected replacement")
	}
	if st.WorkerWS != nil {
		t.Fatal("rejected replacement must not publish a worker")
	}
}

func TestRegisterWorkerOwnerWithoutSession(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := NewWorkerTermState()
	st.HijackOwner = newBrowserWS("o")
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp
	h.registry.Put("w1", st)
	prev, _ := h.Conn.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustTrue(t, prev, "dashboard owner w/o session -> prev hijacked")
	if st.HijackOwner != nil {
		t.Fatal("dashboard owner cleared on register")
	}
}

func TestRegisterWorkerCapacity(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.MaxWorkers = 1 })
	_, err := h.Conn.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustEqual(t, err, nil, "first ok")
	_, err = h.Conn.RegisterWorker(bg(), "w2", &fakeWorkerWS{})
	var rej *WebSocketRejection
	if !errors.As(err, &rej) || rej.Code != 1008 {
		t.Fatalf("expected 1008 rejection, got %v", err)
	}
	// Reconnecting an existing worker at cap is allowed.
	_, err = h.Conn.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustEqual(t, err, nil, "reconnect existing allowed at cap")
}

func TestIsActiveWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ws := &fakeWorkerWS{}
	registerWorkerState(h, "w1", ws)
	mustTrue(t, h.Conn.IsActiveWorker(bg(), "w1", ws), "active")
	mustFalse(t, h.Conn.IsActiveWorker(bg(), "w1", &fakeWorkerWS{}), "other ws not active")
	mustFalse(t, h.Conn.IsActiveWorker(bg(), "ghost", ws), "unknown worker")
}

func TestSetWorkerTunnelFlag(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := registerWorkerState(h, "w1", &fakeWorkerWS{})
	h.Conn.SetWorkerTunnelFlag(bg(), "w1", true)
	mustTrue(t, st.IsTunnelWorker, "flag set")
	h.Conn.SetWorkerTunnelFlag(bg(), "ghost", true) // no-op, no panic
}

func TestSetWorkerHello(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w1", &fakeWorkerWS{})
	pv := 3
	ok, _ := h.Conn.SetWorkerHello(bg(), "w1", InputModeOpen, &pv)
	mustTrue(t, ok, "hello applied")
	mustEqual(t, st.InputMode, InputModeOpen, "mode set")
	mustEqual(t, *st.ProtocolVersion, 3, "protocol version recorded")

	// Blocks switch to open while hijacked.
	st.InputMode = InputModeHijack
	st.HijackSession = &HijackSession{HijackID: "h", LeaseExpiresAt: clk.Monotonic() + 100}
	ok, _ = h.Conn.SetWorkerHello(bg(), "w1", InputModeOpen, nil)
	mustFalse(t, ok, "blocked while hijacked")

	// Unknown worker.
	ok, _ = h.Conn.SetWorkerHello(bg(), "ghost", InputModeHijack, nil)
	mustFalse(t, ok, "unknown worker")
}

func TestUpdateLastSnapshot(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := registerWorkerState(h, "w1", &fakeWorkerWS{})
	h.Conn.UpdateLastSnapshot(bg(), "w1", map[string]any{"screen": "s"})
	mustEqual(t, st.LastSnapshot["screen"], "s", "snapshot stored")
	h.Conn.UpdateLastSnapshot(bg(), "ghost", map[string]any{"screen": "x"}) // no-op
}

func TestDeregisterWorker(t *testing.T) {
	h, clk := newTestHub(t, nil)
	ws := &fakeWorkerWS{}
	st := registerWorkerState(h, "w1", ws)
	st.HijackSession = &HijackSession{HijackID: "h", LeaseExpiresAt: clk.Monotonic() + 100}
	should, was := h.Conn.DeregisterWorker(bg(), "w1", ws)
	mustTrue(t, should, "should broadcast")
	mustTrue(t, was, "was hijacked")
	mustEqual(t, st.WorkerWS, WorkerWS(nil), "worker cleared")
	mustEqual(t, st.HijackSession, (*HijackSession)(nil), "session cleared")

	// A stale ws (not current) returns false,false.
	should, was = h.Conn.DeregisterWorker(bg(), "w1", ws)
	mustFalse(t, should, "stale ws no broadcast")
	mustFalse(t, was, "stale ws not hijacked")
}

func TestRegisterBrowserBasic(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	ws := newBrowserWS("b")
	state, err := h.Conn.RegisterBrowser(bg(), "w1", ws, "operator", false)
	mustEqual(t, err, nil, "no err")
	mustEqual(t, state["worker_online"], true, "worker online")
	mustEqual(t, state["input_mode"], "hijack", "input mode")
	if state["resume_token"] != nil {
		t.Fatal("no resume store -> nil token")
	}
	st := h.registry.Get("w1")
	mustEqual(t, st.Browsers[ws], "operator", "registered with role")
}

func TestRegisterBrowserDeferBroadcast(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ws := newBrowserWS("b")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", true)
	h.lock.Lock()
	pending := h.startupPendingBrowsers[ws]
	h.lock.Unlock()
	mustTrue(t, pending, "deferred broadcast marks startup pending")
	h.Conn.ActivateBrowserBroadcasts(bg(), "w1", ws)
	h.lock.Lock()
	pending = h.startupPendingBrowsers[ws]
	h.lock.Unlock()
	mustFalse(t, pending, "activation clears pending")
}

func TestRegisterBrowserResumeToken(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.ResumeStore = NewInMemoryResumeStore(c.Clock, seqTokenGen())
	})
	ws := newBrowserWS("b")
	state, _ := h.Conn.RegisterBrowser(bg(), "w1", ws, "operator", false)
	mustEqual(t, state["resume_token"], "tok1", "resume token minted")
	h.lock.Lock()
	tok := h.wsToResumeToken[ws]
	h.lock.Unlock()
	mustEqual(t, tok, "tok1", "ws->token mapped")
}

func TestRegisterBrowserPrincipalQuota(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.MaxConnectionsPerPrincipal = 1 })
	a := newBrowserWS("a")
	a.principal = &Principal{SubjectID: "alice"}
	b := newBrowserWS("b")
	b.principal = &Principal{SubjectID: "alice"}

	_, err := h.Conn.RegisterBrowser(bg(), "w1", a, "viewer", false)
	mustEqual(t, err, nil, "first alice ok")
	_, err = h.Conn.RegisterBrowser(bg(), "w1", b, "viewer", false)
	var rej *WebSocketRejection
	if !errors.As(err, &rej) || rej.Code != 1008 {
		t.Fatalf("expected 1008 quota rejection, got %v", err)
	}
	h.lock.Lock()
	count := h.principalBrowserCounts["alice"]
	h.lock.Unlock()
	mustEqual(t, count, 1, "count stays at 1 after rejection")
}

func TestRegisterBrowserAnonymousExempt(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.MaxConnectionsPerPrincipal = 1 })
	a := newBrowserWS("a")
	a.principal = &Principal{SubjectID: "anonymous"}
	b := newBrowserWS("b") // no principal
	_, err := h.Conn.RegisterBrowser(bg(), "w1", a, "viewer", false)
	mustEqual(t, err, nil, "anonymous exempt 1")
	_, err = h.Conn.RegisterBrowser(bg(), "w1", b, "viewer", false)
	mustEqual(t, err, nil, "no-principal exempt")
}

// failResumeStore errors on Create to exercise the quota rollback path.
type failResumeStore struct{}

func (failResumeStore) Create(context.Context, string, string, float64) (string, error) {
	return "", errors.New("db down")
}
func (failResumeStore) Get(context.Context, string) (*ResumeSession, error)     { return nil, nil }
func (failResumeStore) Consume(context.Context, string) (*ResumeSession, error) { return nil, nil }
func (failResumeStore) MarkHijackOwner(context.Context, string, bool) error     { return nil }
func (failResumeStore) Revoke(context.Context, string) error                    { return nil }

func TestRegisterBrowserRollbackOnResumeError(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.ResumeStore = failResumeStore{}
		c.MaxConnectionsPerPrincipal = 2
	})
	ws := newBrowserWS("b")
	ws.principal = &Principal{SubjectID: "bob"}
	_, err := h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", false)
	if err == nil {
		t.Fatal("expected resume store error")
	}
	h.lock.Lock()
	_, counted := h.principalBrowserCounts["bob"]
	h.lock.Unlock()
	mustFalse(t, counted, "quota rolled back on failure")
}

func TestCleanupBrowserDisconnectOwner(t *testing.T) {
	h, clk := newTestHub(t, func(c *TermHubConfig) {
		c.ResumeStore = NewInMemoryResumeStore(c.Clock, seqTokenGen())
	})
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("owner")
	state, _ := h.Conn.RegisterBrowser(bg(), "w1", ws, "operator", false)
	tok := state["resume_token"].(string)
	// Manually register worker + make ws the dashboard owner.
	st := h.registry.Get("w1")
	st.WorkerWS = worker
	st.HijackOwner = ws
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	res, err := h.Conn.CleanupBrowserDisconnect(bg(), "w1", ws, false)
	mustEqual(t, err, nil, "no err")
	mustEqual(t, res["was_owner"], true, "was owner")
	mustEqual(t, st.HijackOwner, BrowserConn(nil), "owner cleared")
	// Resume token marked as hijack owner (not revoked).
	sess, _ := h.resumeStore.Get(bg(), tok)
	if sess == nil || !sess.WasHijackOwner {
		t.Fatal("resume token should be marked hijack owner")
	}
}

func TestCleanupBrowserDisconnectOnWorkerEmpty(t *testing.T) {
	called := make(chan string, 1)
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OnWorkerEmpty = func(_ context.Context, wid string) error { called <- wid; return nil }
	})
	ws := newBrowserWS("b")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", false)
	_, err := h.Conn.CleanupBrowserDisconnect(bg(), "w1", ws, false)
	mustEqual(t, err, nil, "no err")
	// The callback runs in a tracked background goroutine; block on delivery.
	select {
	case wid := <-called:
		mustEqual(t, wid, "w1", "on_worker_empty fired for w1")
	case <-time.After(2 * time.Second):
		t.Fatal("on_worker_empty not fired when last browser left")
	}
	mustEqual(t, h.Shutdown() >= 0, true, "shutdown drains background tasks")
}

func TestRemoveDeadBrowsersFacade(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	owner := newBrowserWS("owner")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[owner] = "operator"
	st.HijackOwner = owner
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	changed, err := h.RemoveDeadBrowsers(bg(), "w1", []BrowserConn{owner})
	mustEqual(t, err, nil, "no err")
	mustTrue(t, changed, "removing dead owner changes hijack state")
	mustEqual(t, st.HijackOwner, BrowserConn(nil), "owner cleared")
	// A resume frame went to the worker.
	frame := decodeOneControl(t, worker.last())
	mustEqual(t, frame["action"], "resume", "worker got resume frame")
}

func TestRateLimitGates(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.RestAcquireRateLimitPerSec = 1
		c.RestSendRateLimitPerSec = 1
	})
	mustTrue(t, h.AllowRESTAcquireFor("c1"), "first acquire allowed")
	mustFalse(t, h.AllowRESTAcquireFor("c1"), "burst exhausted")
	mustTrue(t, h.AllowRESTSendFor("c1"), "first send allowed")
	mustFalse(t, h.AllowRESTSendFor("c1"), "send burst exhausted")
}

func TestWorkerTokenFacade(t *testing.T) {
	tok := "secret"
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.WorkerToken = &tok })
	mustEqual(t, *h.WorkerToken(), "secret", "worker token exposed")
	h2, _ := newTestHub(t, nil)
	if h2.WorkerToken() != nil {
		t.Fatal("default worker token nil")
	}
}
