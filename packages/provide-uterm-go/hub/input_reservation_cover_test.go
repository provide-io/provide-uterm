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

func inputMsg(data string) map[string]any {
	return map[string]any{"type": "input", "data": data}
}

// blockLifecycle installs a live lifecycle reservation on workerID and returns
// its release func, so the input path's lifecycle-wait loop can be driven
// without a sleep.
func blockLifecycle(t *testing.T, h *TermHub, workerID, kind string) func() {
	t.Helper()
	h.lock.Lock()
	st := h.registry.Get(workerID)
	if st == nil {
		h.lock.Unlock()
		t.Fatalf("worker %q not registered", workerID)
	}
	reservation := h.beginLifecycleLocked(st, kind)
	h.lock.Unlock()
	return func() { h.finishLifecycle(workerID, reservation) }
}

func TestSendBrowserOwnedInputDeliversToWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")

	sent, err := h.SendBrowserOwnedInput(bg(), "w", b, inputMsg("ls\n"))
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, sent, "owned input was not delivered")
	mustEqual(t, decodeTerminalData(t, worker.last()), "ls\n", "delivered payload")
}

func TestSendBrowserOwnedInputRejectsNilBrowser(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	sent, err := h.SendBrowserOwnedInput(bg(), "w", nil, inputMsg("x"))
	mustTrue(t, err == nil, "unexpected error")
	mustFalse(t, sent, "a nil browser must never own input")
}

func TestSendBrowserOwnedInputRejectsUnregisteredBrowser(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	sent, err := h.SendBrowserOwnedInput(bg(), "w", newBrowserWS("stranger"), inputMsg("x"))
	mustTrue(t, err == nil, "unexpected error")
	mustFalse(t, sent, "an unregistered browser must never own input")
}

func TestSendBrowserOwnedInputUnknownWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	sent, err := h.SendBrowserOwnedInput(bg(), "missing", newBrowserWS("b"), inputMsg("x"))
	mustTrue(t, err == nil, "unexpected error")
	mustFalse(t, sent, "input accepted for an unknown worker")
}

func TestSendBrowserOwnedInputRejectsDetachedWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	h.lock.Lock()
	h.registry.Get("w").WorkerWS = nil
	h.lock.Unlock()

	sent, err := h.SendBrowserOwnedInput(bg(), "w", b, inputMsg("x"))
	mustTrue(t, err == nil, "unexpected error")
	mustFalse(t, sent, "input accepted for a detached worker")
}

func TestSendBrowserOwnedInputWaitsForLifecycleReservation(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	release := blockLifecycle(t, h, "w", "ws_release_resume")

	waitCtx, reached := WithReservationWaitBarrier(bg())
	done := make(chan bool, 1)
	go func() {
		sent, _ := h.SendBrowserOwnedInput(waitCtx, "w", b, inputMsg("ls\n"))
		done <- sent
	}()
	select {
	case <-reached:
	case sent := <-done:
		t.Fatalf("input completed before the lifecycle wait: %t", sent)
	case <-time.After(5 * time.Second):
		t.Fatal("input never parked on the lifecycle reservation")
	}
	release()
	mustTrue(t, <-done, "input after the lifecycle reservation released")
}

func TestSendBrowserOwnedInputCancelledWhileWaitingOnLifecycle(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	defer blockLifecycle(t, h, "w", "ws_release_resume")()

	ctx, cancel := context.WithCancel(bg())
	cancel()
	sent, err := h.SendBrowserOwnedInput(ctx, "w", b, inputMsg("x"))
	mustFalse(t, sent, "input delivered on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestSendBrowserOwnedInputCancelledWhileWaitingOnInputReservation(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	defer blockPending(t, h, "w")()

	ctx, cancel := context.WithCancel(bg())
	cancel()
	sent, err := h.SendBrowserOwnedInput(ctx, "w", b, inputMsg("x"))
	mustFalse(t, sent, "input delivered on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestSendBrowserOwnedInputPropagatesCancelledSendFailure(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ctx, cancel := context.WithCancel(bg())
	defer cancel()
	worker := &cancellingWorkerWS{cancel: cancel, err: errors.New("socket closed")}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")

	sent, err := h.SendBrowserOwnedInput(ctx, "w", b, inputMsg("x"))
	mustFalse(t, sent, "cancelled send reported as delivered")
	mustTrue(t, err != nil, "cancelled send must propagate its error")
}

func TestSendBrowserOwnedInputBatchAtGenerationDeliversAll(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	generation, ok := h.BrowserInputFence("w", b)
	mustTrue(t, ok, "browser fence unavailable")

	batch, err := h.SendBrowserOwnedInputBatchAtGeneration(bg(), "w", b, generation,
		[]map[string]any{inputMsg("a\n"), inputMsg("b\n")})
	mustTrue(t, err == nil, "unexpected error")
	mustEqual(t, batch.Delivered, 2, "delivered")
	mustEqual(t, batch.Total, 2, "total")
	mustEqual(t, batch.Reason, "", "reason")
	mustEqual(t, len(worker.payloads()), 2, "worker frame count")
}

func TestSendBrowserOwnedInputBatchAtGenerationRejectsStaleFence(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	generation, ok := h.BrowserInputFence("w", b)
	mustTrue(t, ok, "browser fence unavailable")

	batch, err := h.SendBrowserOwnedInputBatchAtGeneration(bg(), "w", b, generation+1,
		[]map[string]any{inputMsg("a\n")})
	mustTrue(t, err == nil, "unexpected error")
	mustEqual(t, batch.Delivered, 0, "delivered")
	mustEqual(t, batch.Reason, "not_owner", "reason")
}

func TestSendBrowserOwnedInputBatchAtGenerationStopsAtFirstFailure(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	generation, ok := h.BrowserInputFence("w", b)
	mustTrue(t, ok, "browser fence unavailable")
	worker.mu.Lock()
	worker.failSend = errors.New("socket closed")
	worker.mu.Unlock()

	batch, err := h.SendBrowserOwnedInputBatchAtGeneration(bg(), "w", b, generation,
		[]map[string]any{inputMsg("a\n"), inputMsg("b\n")})
	mustTrue(t, err == nil, "a dead socket is not an error to the caller")
	mustEqual(t, batch.Delivered, 0, "delivered")
	mustEqual(t, batch.Reason, OwnedInputSendFailed, "reason")
}

func TestBrowserInputFenceRefusesPendingHijack(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	h.lock.Lock()
	h.registry.Get("w").HijackPending = strp("hj")
	h.lock.Unlock()

	_, ok := h.BrowserInputFence("w", b)
	mustFalse(t, ok, "fence issued while a REST acquire was reserving the worker")
}

func TestBrowserInputFenceRefusesUnregisteredBrowser(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	_, ok := h.BrowserInputFence("w", newBrowserWS("stranger"))
	mustFalse(t, ok, "fence issued to an unregistered browser")
}

func TestSendRESTOwnedInputDeliversAndReportsLease(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	st := registerWorkerState(h, "w", worker)
	expires := clk.Monotonic() + 60
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", Owner: "op", LeaseExpiresAt: expires}
	h.lock.Unlock()

	result, err := h.SendRESTOwnedInput(bg(), "w", "hj", inputMsg("ls\n"))
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, result.Sent, "REST-owned input was not delivered")
	got, ok := f64OrNil(result.LeaseExpiresAt)
	mustTrue(t, ok, "lease expiry not reported")
	mustEqual(t, got, expires, "lease expiry")
}

func TestSendRESTOwnedInputRejectsWrongHijackID(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()

	result, err := h.SendRESTOwnedInput(bg(), "w", "other", inputMsg("x"))
	mustTrue(t, err == nil, "unexpected error")
	mustFalse(t, result.Sent, "input accepted under the wrong hijack id")
	mustEqual(t, result.Reason, OwnedInputInvalidHijack, "reason")
}

func TestSendRESTOwnedInputRejectsExpiredLease(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() - 1}
	h.lock.Unlock()

	result, err := h.SendRESTOwnedInput(bg(), "w", "hj", inputMsg("x"))
	mustTrue(t, err == nil, "unexpected error")
	mustFalse(t, result.Sent, "input accepted under an expired lease")
	mustEqual(t, result.Reason, OwnedInputInvalidHijack, "reason")
}

func TestSendRESTOwnedInputUnknownWorkerReportsInvalidHijack(t *testing.T) {
	h, _ := newTestHub(t, nil)
	result, err := h.SendRESTOwnedInput(bg(), "missing", "hj", inputMsg("x"))
	mustTrue(t, err == nil, "unexpected error")
	mustEqual(t, result.Reason, OwnedInputInvalidHijack, "reason")
}

func TestSendRESTOwnedInputRejectsUnsupportedTunnelMessage(t *testing.T) {
	h, clk := newTestHub(t, nil)
	tunnel := &fakeTunnelWS{}
	st := registerWorkerState(h, "w", tunnel)
	h.lock.Lock()
	st.IsTunnelWorker = true
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()

	result, err := h.SendRESTOwnedInput(bg(), "w", "hj", map[string]any{"type": "resize", "cols": 80})
	mustTrue(t, err == nil, "an unsupported message is not an error")
	mustFalse(t, result.Sent, "resize accepted by a tunnel worker")
	mustEqual(t, result.Reason, OwnedInputUnsupported, "reason")

	// The supported types still route over the tunnel side channels.
	result, err = h.SendRESTOwnedInput(bg(), "w", "hj", inputMsg("ls\n"))
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, result.Sent, "input rejected by a tunnel worker")
	result, err = h.SendRESTOwnedInput(bg(), "w", "hj", map[string]any{"type": "http_action", "id": "1"})
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, result.Sent, "http_action rejected by a tunnel worker")
}

func TestWaitResumeTokenReadyAllowsTheBoundSocket(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ws := newBrowserWS("b")
	h.lock.Lock()
	h.wsToResumeToken[ws] = "tok"
	h.lock.Unlock()

	mustTrue(t, h.WaitResumeTokenReady(bg(), "tok", ws) == nil, "the bound socket must pass immediately")
}

func TestWaitResumeTokenReadyPassesWhenNothingHoldsTheToken(t *testing.T) {
	h, _ := newTestHub(t, nil)
	mustTrue(t, h.WaitResumeTokenReady(bg(), "tok", newBrowserWS("b")) == nil, "unheld token must pass")
}

func TestWaitResumeTokenReadyWaitsForDetach(t *testing.T) {
	h, _ := newTestHub(t, nil)
	detached := make(chan struct{})
	h.lock.Lock()
	h.resumeTokenDetached["tok"] = detached
	h.lock.Unlock()

	done := make(chan error, 1)
	go func() { done <- h.WaitResumeTokenReady(bg(), "tok", newBrowserWS("new")) }()
	h.lock.Lock()
	h.detachResumeTokenLocked("tok")
	h.lock.Unlock()
	select {
	case err := <-done:
		mustTrue(t, err == nil, "unexpected error")
	case <-time.After(5 * time.Second):
		t.Fatal("wait did not return after detach")
	}
}

func TestWaitResumeTokenReadyHonoursCancellation(t *testing.T) {
	h, _ := newTestHub(t, nil)
	h.lock.Lock()
	h.resumeTokenDetached["tok"] = make(chan struct{})
	h.lock.Unlock()

	ctx, cancel := context.WithCancel(bg())
	cancel()
	err := h.WaitResumeTokenReady(ctx, "tok", newBrowserWS("new"))
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestIsCurrentDashboardOwner(t *testing.T) {
	h, _ := newTestHub(t, nil)
	mustFalse(t, h.IsCurrentDashboardOwner("missing", newBrowserWS("b")), "unknown worker has no owner")

	registerWorkerState(h, "w", &fakeWorkerWS{})
	ws := newBrowserWS("b")
	mustFalse(t, h.IsCurrentDashboardOwner("w", ws), "idle worker has no owner")
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", ws); !ok {
		t.Fatalf("acquire: %s", reason)
	}
	mustTrue(t, h.IsCurrentDashboardOwner("w", ws), "acquiring socket is the owner")
	mustFalse(t, h.IsCurrentDashboardOwner("w", newBrowserWS("other")), "a different socket is not the owner")
}

func TestMarkBrowserResumeOwnerSkipsUnboundSocket(t *testing.T) {
	// A hub with a resume store but no token bound to the socket must not
	// reach the store at all, so a failing store cannot break the release.
	ws := newBrowserWS("b")
	h, _ := newFailingResumeHub(t, ws)
	h.lock.Lock()
	delete(h.wsToResumeToken, ws)
	h.lock.Unlock()
	registerWorkerState(h, "w", &fakeWorkerWS{})
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", ws); !ok {
		t.Fatalf("acquire: %s", reason)
	}

	released, _, err := h.ReleaseWsHijack(bg(), "w", ws)
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, released, "release blocked by a store that should not be consulted")
}
