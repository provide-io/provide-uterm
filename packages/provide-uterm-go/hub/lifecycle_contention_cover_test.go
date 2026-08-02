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

// runBlocked registers a worker, parks an input reservation on it, runs op in a
// goroutine, asserts op parked on the reservation, then releases it and returns
// op's result. Every step is channel-driven, so the ordering is exact rather
// than timed.
func runBlocked(t *testing.T, h *TermHub, workerID string, op func(ctx context.Context) any) any {
	t.Helper()
	release := blockPending(t, h, workerID)
	waitCtx, reached := WithReservationWaitBarrier(bg())
	done := make(chan any, 1)
	go func() { done <- op(waitCtx) }()
	select {
	case <-reached:
	case got := <-done:
		t.Fatalf("operation finished before the reservation wait: %v", got)
	case <-time.After(5 * time.Second):
		t.Fatal("operation never parked on the reservation")
	}
	release()
	select {
	case got := <-done:
		return got
	case <-time.After(5 * time.Second):
		t.Fatal("operation never resumed after the reservation released")
		return nil
	}
}

// cancelledCtx returns an already-cancelled context.
func cancelledCtx() context.Context {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	return ctx
}

func TestRegisterWorkerWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		_, err := h.RegisterWorkerWithTransport(ctx, "w", &fakeWorkerWS{}, false)
		return err
	})
	mustTrue(t, got == nil, "worker re-register after the reservation released")
}

func TestRegisterWorkerCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	defer blockPending(t, h, "w")()

	_, err := h.RegisterWorkerWithTransport(cancelledCtx(), "w", &fakeWorkerWS{}, false)
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestSetWorkerHelloWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		ok, _ := h.SetWorkerHello(ctx, "w", InputModeOpen, nil)
		return ok
	})
	mustTrue(t, got.(bool), "hello applied after the reservation released")
}

func TestSetWorkerHelloCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	defer blockPending(t, h, "w")()

	ok, err := h.SetWorkerHello(cancelledCtx(), "w", InputModeOpen, nil)
	mustFalse(t, ok, "hello applied on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestDeregisterWorkerWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		should, _ := h.DeregisterWorker(ctx, "w", worker)
		return should
	})
	mustTrue(t, got.(bool), "deregister after the reservation released")
}

func TestDeregisterWorkerCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	defer blockPending(t, h, "w")()

	should, wasHijacked := h.DeregisterWorker(cancelledCtx(), "w", worker)
	mustFalse(t, should, "deregister proceeded on a cancelled context")
	mustFalse(t, wasHijacked, "hijack reported on a cancelled deregister")
}

func TestSetInputModeWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		ok, _, _ := h.SetInputMode(ctx, "w", InputModeOpen)
		return ok
	})
	mustTrue(t, got.(bool), "input mode set after the reservation released")
}

func TestSetInputModeCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	defer blockPending(t, h, "w")()

	ok, _, err := h.SetInputMode(cancelledCtx(), "w", InputModeOpen)
	mustFalse(t, ok, "input mode set on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestTryReclaimHijackWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		return h.TryReclaimHijack(ctx, "w", newBrowserWS("b"))
	})
	mustTrue(t, got.(bool), "reclaim after the reservation released")
}

func TestTryReclaimHijackCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	defer blockPending(t, h, "w")()

	mustFalse(t, h.TryReclaimHijack(cancelledCtx(), "w", newBrowserWS("b")), "reclaimed on a cancelled context")
}

func TestDisconnectWorkerWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		disconnected, err := h.DisconnectWorker(ctx, "w")
		if err != nil {
			return err
		}
		return disconnected
	})
	mustTrue(t, got == any(true), "disconnect after the reservation released")
}

func TestDisconnectWorkerCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	defer blockPending(t, h, "w")()

	disconnected, err := h.DisconnectWorker(cancelledCtx(), "w")
	mustFalse(t, disconnected, "disconnected on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestForceReleaseHijackWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	ws := newBrowserWS("b")
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", ws); !ok {
		t.Fatalf("acquire: %s", reason)
	}

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		released, err := h.ForceReleaseHijack(ctx, "w")
		if err != nil {
			return err
		}
		return released
	})
	mustTrue(t, got == any(true), "force release after the reservation released")
	mustEqual(t, str(decodeOneControl(t, worker.last())["action"]), "resume", "worker frame action")
}

func TestForceReleaseHijackCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	defer blockPending(t, h, "w")()

	released, err := h.ForceReleaseHijack(cancelledCtx(), "w")
	mustFalse(t, released, "released on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestForceReleaseHijackPropagatesResumeStoreFailure(t *testing.T) {
	ws := newBrowserWS("b")
	h, markErr := newFailingResumeHub(t, ws)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	exp := h.clock.Monotonic() + 60
	h.lock.Lock()
	st.setDashboardOwner(ws, &exp)
	h.lock.Unlock()

	released, err := h.ForceReleaseHijack(bg(), "w")
	mustFalse(t, released, "released despite the resume store failing")
	mustTrue(t, errors.Is(err, markErr), "expected the store error to propagate")
}

func TestForceReleaseHijackPropagatesResumeSendFailure(t *testing.T) {
	h, clk := newTestHub(t, nil)
	sendErr := errors.New("resume send failed")
	st := registerWorkerState(h, "w", &fakeWorkerWS{failSend: sendErr})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", Owner: "op", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()

	released, err := h.ForceReleaseHijack(cancelledCtx(), "w")
	mustFalse(t, released, "release reported despite an undelivered resume")
	mustTrue(t, errors.Is(err, sendErr), "expected the send error to propagate")
}

func TestCleanupBrowserDisconnectWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		state, err := h.CleanupBrowserDisconnect(ctx, "w", b, true)
		if err != nil {
			return err
		}
		return state["was_owner"]
	})
	mustTrue(t, got == any(true), "disconnect cleanup after the reservation released")
}

func TestCleanupBrowserDisconnectCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	defer blockPending(t, h, "w")()

	state, err := h.CleanupBrowserDisconnect(cancelledCtx(), "w", b, true)
	mustTrue(t, state == nil, "state returned on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestCleanupBrowserDisconnectPropagatesResumeSendFailure(t *testing.T) {
	h, _ := newTestHub(t, nil)
	sendErr := errors.New("resume send failed")
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	worker.mu.Lock()
	worker.failSend = sendErr
	worker.mu.Unlock()

	state, err := h.CleanupBrowserDisconnect(cancelledCtx(), "w", b, true)
	mustTrue(t, state == nil, "state returned despite a failed resume")
	mustTrue(t, errors.Is(err, sendErr), "expected the send error to propagate")
}

func TestReplaceBrowserResumeTokenRevokesTheSupersededToken(t *testing.T) {
	store := NewInMemoryResumeStore(nil, nil)
	h, _ := newTestHub(t, func(cfg *TermHubConfig) { cfg.ResumeStore = store })
	ws := newBrowserWS("b")
	first, err := store.Create(bg(), "w", "admin", 300)
	mustTrue(t, err == nil, "create first token")
	second, err := store.Create(bg(), "w", "admin", 300)
	mustTrue(t, err == nil, "create second token")

	mustTrue(t, h.ReplaceBrowserResumeToken(bg(), ws, first) == nil, "bind first token")
	mustTrue(t, h.ReplaceBrowserResumeToken(bg(), ws, second) == nil, "replace with second token")

	got, err := store.Get(bg(), first)
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, got == nil, "the superseded token was not revoked")
	got, err = store.Get(bg(), second)
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, got != nil, "the replacement token was revoked")
	h.lock.Lock()
	bound := h.wsToResumeToken[ws]
	h.lock.Unlock()
	mustEqual(t, bound, second, "bound token")

	// Re-binding the same token is idempotent and must not revoke it.
	mustTrue(t, h.ReplaceBrowserResumeToken(bg(), ws, second) == nil, "rebind same token")
	got, err = store.Get(bg(), second)
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, got != nil, "re-binding revoked the live token")
}

func TestCleanupExpiredHijackWaitsForPendingInput(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() - 1}
	h.lock.Unlock()

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		cleaned, err := h.CleanupExpiredHijack(ctx, "w")
		if err != nil {
			return err
		}
		return cleaned
	})
	mustTrue(t, got == any(true), "expiry after the reservation released")
}

func TestCleanupExpiredHijackCancelledWhileWaiting(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() - 1}
	h.lock.Unlock()
	defer blockPending(t, h, "w")()

	cleaned, err := h.CleanupExpiredHijack(cancelledCtx(), "w")
	mustFalse(t, cleaned, "expiry ran on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestCleanupExpiredHijackPropagatesResumeStoreFailure(t *testing.T) {
	ws := newBrowserWS("b")
	h, markErr := newFailingResumeHub(t, ws)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	exp := h.clock.Monotonic() - 1
	h.lock.Lock()
	st.setDashboardOwner(ws, &exp)
	h.lock.Unlock()

	cleaned, err := h.CleanupExpiredHijack(bg(), "w")
	mustFalse(t, cleaned, "expiry reported despite the resume store failing")
	mustTrue(t, errors.Is(err, markErr), "expected the store error to propagate")
}

func TestCleanupExpiredHijackPropagatesResumeSendFailure(t *testing.T) {
	h, clk := newTestHub(t, nil)
	sendErr := errors.New("resume send failed")
	st := registerWorkerState(h, "w", &fakeWorkerWS{failSend: sendErr})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() - 1}
	h.lock.Unlock()

	cleaned, err := h.CleanupExpiredHijack(cancelledCtx(), "w")
	mustFalse(t, cleaned, "expiry reported despite an undelivered resume")
	mustTrue(t, errors.Is(err, sendErr), "expected the send error to propagate")
}

func TestRemoveDeadBrowsersWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		changed, err := h.RemoveDeadBrowsers(ctx, "w", []BrowserConn{b})
		if err != nil {
			return err
		}
		return changed
	})
	mustTrue(t, got == any(true), "dead-browser removal after the reservation released")
}

func TestRemoveDeadBrowsersCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	defer blockPending(t, h, "w")()

	changed, err := h.RemoveDeadBrowsers(cancelledCtx(), "w", []BrowserConn{b})
	mustFalse(t, changed, "removal ran on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestRemoveDeadBrowsersPropagatesResumeStoreFailure(t *testing.T) {
	markErr := errors.New("resume store down")
	store := &errResumeStore{InMemoryResumeStore: NewInMemoryResumeStore(nil, nil)}
	h, _ := newTestHub(t, func(cfg *TermHubConfig) { cfg.ResumeStore = store })
	registerWorkerState(h, "w", &fakeWorkerWS{})
	ws := newBrowserWS("b")
	registerActiveBrowser(t, h, "w", ws, "admin")
	// Arm the failure only once ownership is established, so the removal path
	// is the first caller to see the store fail.
	h.lock.Lock()
	h.wsToResumeToken[ws] = "tok"
	h.lock.Unlock()
	store.markErr = markErr

	changed, err := h.RemoveDeadBrowsers(bg(), "w", []BrowserConn{ws})
	mustFalse(t, changed, "removal reported despite the resume store failing")
	mustTrue(t, errors.Is(err, markErr), "expected the store error to propagate")
}

func TestRemoveDeadBrowsersDetachesResumeToken(t *testing.T) {
	store := NewInMemoryResumeStore(nil, nil)
	h, _ := newTestHub(t, func(cfg *TermHubConfig) { cfg.ResumeStore = store })
	registerWorkerState(h, "w", &fakeWorkerWS{})
	ws := newBrowserWS("b")
	registerActiveBrowser(t, h, "w", ws, "admin")
	token, err := store.Create(bg(), "w", "admin", 300)
	mustTrue(t, err == nil, "create token")
	mustTrue(t, h.ReplaceBrowserResumeToken(bg(), ws, token) == nil, "bind token")

	changed, err := h.RemoveDeadBrowsers(bg(), "w", []BrowserConn{ws})
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, changed, "dashboard ownership change not reported")
	// The token is detached, so a reconnecting socket no longer waits on it.
	mustTrue(t, h.WaitResumeTokenReady(bg(), token, newBrowserWS("new")) == nil, "token still held")
}

func TestTryAcquireRestHijackWaitsForPendingInput(t *testing.T) {
	h, clk := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	got := runBlocked(t, h, "w", func(ctx context.Context) any {
		ok, reason, err := h.TryAcquireRestHijack(ctx, "w", "op", 60, "hj", clk.Monotonic())
		if err != nil {
			return err
		}
		if !ok {
			return reason
		}
		return true
	})
	mustTrue(t, got == any(true), "REST acquire after the reservation released")
}

func TestTryAcquireRestHijackCancelledWhileWaiting(t *testing.T) {
	h, clk := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	defer blockPending(t, h, "w")()

	ok, _, err := h.TryAcquireRestHijack(cancelledCtx(), "w", "op", 60, "hj", clk.Monotonic())
	mustFalse(t, ok, "acquired on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestTryAcquireRestHijackRefusesTunnelWorker(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeTunnelWS{})
	h.lock.Lock()
	st.IsTunnelWorker = true
	h.lock.Unlock()

	ok, reason, err := h.TryAcquireRestHijack(bg(), "w", "op", 60, "hj", clk.Monotonic())
	mustTrue(t, err == nil, "unexpected error")
	mustFalse(t, ok, "tunnel worker accepted a REST hijack")
	mustEqual(t, reason, OwnedInputUnsupported, "reason")
}
