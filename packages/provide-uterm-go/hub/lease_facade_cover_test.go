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

// errResumeStore is an [InMemoryResumeStore] whose MarkHijackOwner fails, so the
// resume-bookkeeping error arms of the lease facade run against a real failing
// store rather than a stub that proves nothing.
type errResumeStore struct {
	*InMemoryResumeStore
	markErr error
}

func (s *errResumeStore) MarkHijackOwner(_ context.Context, _ string, _ bool) error {
	return s.markErr
}

// newFailingResumeHub builds a hub whose resume store fails MarkHijackOwner and
// binds token "tok" to ws, so markBrowserResumeOwnerLocked reaches the store.
func newFailingResumeHub(t *testing.T, ws BrowserConn) (*TermHub, error) {
	t.Helper()
	markErr := errors.New("resume store down")
	store := &errResumeStore{InMemoryResumeStore: NewInMemoryResumeStore(nil, nil), markErr: markErr}
	h, _ := newTestHub(t, func(cfg *TermHubConfig) { cfg.ResumeStore = store })
	h.lock.Lock()
	h.wsToResumeToken[ws] = "tok"
	h.lock.Unlock()
	return h, markErr
}

// blockPending installs a live input-send reservation on workerID and returns a
// release func. Callers use it to drive the facade's reservation-wait loops
// without any sleep: the operation parks on the reservation channel, the test
// observes that via the reservation-wait barrier, then releases.
func blockPending(t *testing.T, h *TermHub, workerID string) func() {
	t.Helper()
	h.lock.Lock()
	st := h.registry.Get(workerID)
	if st == nil {
		h.lock.Unlock()
		t.Fatalf("worker %q not registered", workerID)
	}
	reservation := &InputSendReservation{Worker: st.WorkerWS, Done: make(chan struct{})}
	st.InputSendPending = reservation
	h.lock.Unlock()
	return func() {
		h.lock.Lock()
		if st := h.registry.Get(workerID); st != nil && st.InputSendPending == reservation {
			st.InputSendPending = nil
		}
		h.lock.Unlock()
		close(reservation.Done)
	}
}

// awaitBarrier fails the test unless the operation parked on a reservation wait.
func awaitBarrier(t *testing.T, reached <-chan struct{}) {
	t.Helper()
	select {
	case <-reached:
	case <-time.After(5 * time.Second):
		t.Fatal("operation never reached the reservation wait")
	}
}

func TestTryAcquireWsHijackNoWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ok, reason := h.TryAcquireWsHijack(bg(), "missing", newBrowserWS("b"))
	mustFalse(t, ok, "unregistered worker acquires")
	mustEqual(t, reason, "no_worker", "reason")

	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.WorkerWS = nil
	h.lock.Unlock()
	ok, reason = h.TryAcquireWsHijack(bg(), "w", newBrowserWS("b"))
	mustFalse(t, ok, "detached worker acquires")
	mustEqual(t, reason, "no_worker", "reason")
}

func TestTryAcquireWsHijackRefusesDuringAcquirePause(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.LifecyclePending = &LifecycleReservation{Kind: "ws_acquire_pause", Done: make(chan struct{})}
	h.lock.Unlock()

	ok, reason := h.TryAcquireWsHijack(bg(), "w", newBrowserWS("b"))
	mustFalse(t, ok, "acquired mid acquire-pause")
	mustEqual(t, reason, "already_hijacked", "reason")
}

func TestTryAcquireWsHijackWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	release := blockPending(t, h, "w")

	waitCtx, reached := WithReservationWaitBarrier(bg())
	type res struct {
		ok     bool
		reason string
	}
	done := make(chan res, 1)
	go func() {
		ok, reason := h.TryAcquireWsHijack(waitCtx, "w", newBrowserWS("b"))
		done <- res{ok, reason}
	}()
	select {
	case <-reached:
	case got := <-done:
		t.Fatalf("acquire finished before the reservation wait: %+v", got)
	case <-time.After(5 * time.Second):
		t.Fatal("acquire never parked on the reservation")
	}
	release()
	got := <-done
	mustTrue(t, got.ok, "acquire after the reservation released: "+got.reason)
}

func TestTryAcquireWsHijackCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	defer blockPending(t, h, "w")()

	ctx, cancel := context.WithCancel(bg())
	cancel()
	ok, reason := h.TryAcquireWsHijack(ctx, "w", newBrowserWS("b"))
	mustFalse(t, ok, "acquired on a cancelled context")
	mustEqual(t, reason, "cancelled", "reason")
}

func TestTryAcquireWsHijackAlreadyHijacked(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	exp := clk.Monotonic() + 60
	h.lock.Lock()
	st.setDashboardOwner(newBrowserWS("owner"), &exp)
	h.lock.Unlock()

	ok, reason := h.TryAcquireWsHijack(bg(), "w", newBrowserWS("b"))
	mustFalse(t, ok, "second dashboard acquire succeeded")
	mustEqual(t, reason, "already_hijacked", "reason")
}

func TestTryAcquireWsHijackResumeStoreFailureRollsBack(t *testing.T) {
	ws := newBrowserWS("b")
	h, _ := newFailingResumeHub(t, ws)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})

	ok, reason := h.TryAcquireWsHijack(bg(), "w", ws)
	mustFalse(t, ok, "acquired despite the resume store failing")
	mustEqual(t, reason, "resume_store", "reason")
	h.lock.Lock()
	owner := st.HijackOwner
	h.lock.Unlock()
	mustTrue(t, owner == nil, "dashboard owner was not rolled back")
}

func TestAcquireWsHijackAndPausePausesWorkerThenPublishesOwner(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	st := registerWorkerState(h, "w", worker)
	ws := newBrowserWS("b")

	ok, reason := h.AcquireWsHijackAndPause(bg(), "w", ws)
	mustTrue(t, ok, "acquire failed: "+reason)
	mustEqual(t, reason, "", "reason")
	control := decodeOneControl(t, worker.last())
	mustEqual(t, str(control["action"]), "pause", "worker frame action")
	h.lock.Lock()
	owner, pending := st.HijackOwner, st.LifecyclePending
	h.lock.Unlock()
	mustTrue(t, owner == ws, "dashboard owner not published")
	mustTrue(t, pending == nil, "lifecycle reservation not released")
}

func TestAcquireWsHijackAndPauseNoWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ok, reason := h.AcquireWsHijackAndPause(bg(), "missing", newBrowserWS("b"))
	mustFalse(t, ok, "acquired an unregistered worker")
	mustEqual(t, reason, "no_worker", "reason")
}

func TestAcquireWsHijackAndPauseWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	release := blockPending(t, h, "w")

	waitCtx, reached := WithReservationWaitBarrier(bg())
	done := make(chan bool, 1)
	go func() {
		ok, _ := h.AcquireWsHijackAndPause(waitCtx, "w", newBrowserWS("b"))
		done <- ok
	}()
	select {
	case <-reached:
	case ok := <-done:
		t.Fatalf("acquire finished before the reservation wait: %t", ok)
	case <-time.After(5 * time.Second):
		t.Fatal("acquire never parked on the reservation")
	}
	release()
	mustTrue(t, <-done, "acquire after the reservation released")
}

func TestAcquireWsHijackAndPauseCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	defer blockPending(t, h, "w")()

	ctx, cancel := context.WithCancel(bg())
	cancel()
	ok, reason := h.AcquireWsHijackAndPause(ctx, "w", newBrowserWS("b"))
	mustFalse(t, ok, "acquired on a cancelled context")
	mustEqual(t, reason, "cancelled", "reason")
}

func TestAcquireWsHijackAndPauseAlreadyHijacked(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()

	ok, reason := h.AcquireWsHijackAndPause(bg(), "w", newBrowserWS("b"))
	mustFalse(t, ok, "acquired over a live REST lease")
	mustEqual(t, reason, "already_hijacked", "reason")
}

func TestAcquireWsHijackAndPauseRefusesTunnelWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeTunnelWS{})
	h.lock.Lock()
	st.IsTunnelWorker = true
	h.lock.Unlock()

	ok, reason := h.AcquireWsHijackAndPause(bg(), "w", newBrowserWS("b"))
	mustFalse(t, ok, "tunnel worker accepted an owned pause")
	mustEqual(t, reason, OwnedInputUnsupported, "reason")
}

func TestAcquireWsHijackAndPauseDetachesWorkerOnPauseFailure(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{failSend: errors.New("socket closed")}
	st := registerWorkerState(h, "w", worker)

	ok, reason := h.AcquireWsHijackAndPause(bg(), "w", newBrowserWS("b"))
	mustFalse(t, ok, "acquired despite a failed pause")
	mustEqual(t, reason, "no_worker", "reason")
	h.lock.Lock()
	live := st.WorkerWS
	h.lock.Unlock()
	mustTrue(t, live == nil, "dead worker socket was not detached")
}

// cancellingWorkerWS cancels the caller's context from inside the send, so the
// pause failure is observed with an already-cancelled operation context.
type cancellingWorkerWS struct {
	cancel context.CancelFunc
	err    error
}

func (w *cancellingWorkerWS) SendText(context.Context, string) error {
	w.cancel()
	return w.err
}

func TestAcquireWsHijackAndPauseCancelledDuringPause(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ctx, cancel := context.WithCancel(bg())
	defer cancel()
	worker := &cancellingWorkerWS{cancel: cancel, err: errors.New("socket closed")}
	st := registerWorkerState(h, "w", worker)

	ok, reason := h.AcquireWsHijackAndPause(ctx, "w", newBrowserWS("b"))
	mustFalse(t, ok, "acquired on a cancelled pause")
	mustEqual(t, reason, "cancelled", "reason")
	h.lock.Lock()
	live := st.WorkerWS
	h.lock.Unlock()
	mustTrue(t, live == worker, "cancellation must not detach the worker socket")
}

// swappingWorkerWS replaces the registry's worker socket mid-send, modelling a
// worker reconnect that races the pause frame.
type swappingWorkerWS struct {
	h        *TermHub
	workerID string
	replaced bool
}

func (w *swappingWorkerWS) SendText(context.Context, string) error {
	if !w.replaced {
		w.replaced = true
		w.h.lock.Lock()
		if st := w.h.registry.Get(w.workerID); st != nil {
			st.WorkerWS = &fakeWorkerWS{}
			st.WorkerGeneration++
		}
		w.h.lock.Unlock()
	}
	return nil
}

func TestAcquireWsHijackAndPauseAbortsWhenWorkerReconnects(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &swappingWorkerWS{h: h, workerID: "w"}
	st := registerWorkerState(h, "w", worker)

	ok, reason := h.AcquireWsHijackAndPause(bg(), "w", newBrowserWS("b"))
	mustFalse(t, ok, "acquired against a replaced worker socket")
	mustEqual(t, reason, "no_worker", "reason")
	h.lock.Lock()
	owner := st.HijackOwner
	h.lock.Unlock()
	mustTrue(t, owner == nil, "owner published against a replaced worker")
}

func TestAcquireWsHijackAndPauseResumesWorkerOnResumeStoreFailure(t *testing.T) {
	ws := newBrowserWS("b")
	h, _ := newFailingResumeHub(t, ws)
	worker := &fakeWorkerWS{}
	st := registerWorkerState(h, "w", worker)

	ok, reason := h.AcquireWsHijackAndPause(bg(), "w", ws)
	mustFalse(t, ok, "acquired despite the resume store failing")
	mustEqual(t, reason, "resume_store", "reason")
	h.lock.Lock()
	owner := st.HijackOwner
	h.lock.Unlock()
	mustTrue(t, owner == nil, "dashboard owner was not rolled back")
	// The worker was paused, so the rollback must un-pause it.
	payloads := worker.payloads()
	mustEqual(t, len(payloads), 2, "worker frame count")
	mustEqual(t, str(decodeOneControl(t, payloads[1])["action"]), "resume", "rollback frame action")
}

func TestReleaseWsHijackIgnoresNonOwner(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	exp := clk.Monotonic() + 60
	h.lock.Lock()
	st.setDashboardOwner(newBrowserWS("owner"), &exp)
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()

	released, restActive, err := h.ReleaseWsHijack(bg(), "w", newBrowserWS("other"))
	mustTrue(t, err == nil, "unexpected error")
	mustFalse(t, released, "non-owner released the dashboard hijack")
	mustTrue(t, restActive, "live REST lease not reported")
}

func TestReleaseWsHijackWaitsForPendingInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	ws := newBrowserWS("b")
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", ws); !ok {
		t.Fatalf("acquire: %s", reason)
	}
	release := blockPending(t, h, "w")

	waitCtx, reached := WithReservationWaitBarrier(bg())
	done := make(chan bool, 1)
	go func() {
		released, _, _ := h.ReleaseWsHijack(waitCtx, "w", ws)
		done <- released
	}()
	select {
	case <-reached:
	case released := <-done:
		t.Fatalf("release finished before the reservation wait: %t", released)
	case <-time.After(5 * time.Second):
		t.Fatal("release never parked on the reservation")
	}
	release()
	mustTrue(t, <-done, "release after the reservation released")
	mustEqual(t, str(decodeOneControl(t, worker.last())["action"]), "resume", "worker frame action")
}

func TestReleaseWsHijackCancelledWhileWaiting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	ws := newBrowserWS("b")
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", ws); !ok {
		t.Fatalf("acquire: %s", reason)
	}
	defer blockPending(t, h, "w")()

	ctx, cancel := context.WithCancel(bg())
	cancel()
	released, _, err := h.ReleaseWsHijack(ctx, "w", ws)
	mustFalse(t, released, "released on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestReleaseWsHijackPropagatesResumeStoreFailure(t *testing.T) {
	ws := newBrowserWS("b")
	h, markErr := newFailingResumeHub(t, ws)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	exp := h.clock.Monotonic() + 60
	h.lock.Lock()
	st.setDashboardOwner(ws, &exp)
	h.lock.Unlock()

	released, _, err := h.ReleaseWsHijack(bg(), "w", ws)
	mustFalse(t, released, "released despite the resume store failing")
	mustTrue(t, errors.Is(err, markErr), "expected the store error to propagate")
}

func TestReleaseWsHijackKeepsWorkerPausedForRestLease(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	ws := newBrowserWS("b")
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", ws); !ok {
		t.Fatalf("acquire: %s", reason)
	}
	h.lock.Lock()
	h.registry.Get("w").HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()

	released, restActive, err := h.ReleaseWsHijack(bg(), "w", ws)
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, released, "dashboard hijack not released")
	mustTrue(t, restActive, "REST lease not reported")
	mustEqual(t, len(worker.payloads()), 0, "no resume frame may be sent while REST holds the lease")
}

func TestReleaseWsHijackPropagatesResumeSendFailure(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	ws := newBrowserWS("b")
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", ws); !ok {
		t.Fatalf("acquire: %s", reason)
	}
	sendErr := errors.New("resume send failed")
	worker.mu.Lock()
	worker.failSend = sendErr
	worker.mu.Unlock()

	ctx, cancel := context.WithCancel(bg())
	cancel()
	released, _, err := h.ReleaseWsHijack(ctx, "w", ws)
	mustTrue(t, released, "release must be reported even when the resume frame fails")
	mustTrue(t, errors.Is(err, sendErr), "expected the send error to propagate")
}

func TestReleaseWsHijackReportsLostWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{failSend: errors.New("socket closed")}
	registerWorkerState(h, "w", worker)
	ws := newBrowserWS("b")
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", ws); !ok {
		t.Fatalf("acquire: %s", reason)
	}

	released, _, err := h.ReleaseWsHijack(bg(), "w", ws)
	mustTrue(t, released, "release must be reported even when the worker is gone")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled for an undelivered resume")
}

func TestReleaseRestHijackAndResumeIgnoresUnknownLease(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})

	released, resume, err := h.ReleaseRestHijackAndResume(bg(), "w", "hj")
	mustTrue(t, err == nil, "unexpected error")
	mustFalse(t, released, "released a lease that was never acquired")
	mustFalse(t, resume, "resume requested for a missing lease")
}

func TestReleaseRestHijackAndResumeWaitsForPendingInput(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	st := registerWorkerState(h, "w", worker)
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", Owner: "op", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()
	release := blockPending(t, h, "w")

	waitCtx, reached := WithReservationWaitBarrier(bg())
	done := make(chan bool, 1)
	go func() {
		released, _, _ := h.ReleaseRestHijackAndResume(waitCtx, "w", "hj")
		done <- released
	}()
	awaitBarrier(t, reached)
	release()
	mustTrue(t, <-done, "release after the reservation released")
	control := decodeOneControl(t, worker.last())
	mustEqual(t, str(control["action"]), "resume", "worker frame action")
	mustEqual(t, str(control["owner"]), "op", "resume frame owner")
}

func TestReleaseRestHijackAndResumeCancelledWhileWaiting(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()
	defer blockPending(t, h, "w")()

	ctx, cancel := context.WithCancel(bg())
	cancel()
	released, _, err := h.ReleaseRestHijackAndResume(ctx, "w", "hj")
	mustFalse(t, released, "released on a cancelled context")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled")
}

func TestReleaseRestHijackAndResumeKeepsPausedForDashboardOwner(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	ws := newBrowserWS("b")
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", ws); !ok {
		t.Fatalf("acquire: %s", reason)
	}
	h.lock.Lock()
	h.registry.Get("w").HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()

	released, resume, err := h.ReleaseRestHijackAndResume(bg(), "w", "hj")
	mustTrue(t, err == nil, "unexpected error")
	mustTrue(t, released, "REST lease not released")
	mustFalse(t, resume, "resume requested while the dashboard still owns input")
	mustEqual(t, len(worker.payloads()), 0, "no resume frame may be sent to a dashboard-owned worker")
}

func TestReleaseRestHijackAndResumePropagatesResumeSendFailure(t *testing.T) {
	h, clk := newTestHub(t, nil)
	sendErr := errors.New("resume send failed")
	worker := &fakeWorkerWS{failSend: sendErr}
	st := registerWorkerState(h, "w", worker)
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", Owner: "op", LeaseExpiresAt: clk.Monotonic() + 60}
	h.lock.Unlock()

	ctx, cancel := context.WithCancel(bg())
	cancel()
	released, resume, err := h.ReleaseRestHijackAndResume(ctx, "w", "hj")
	mustTrue(t, released, "release must be reported even when the resume frame fails")
	mustTrue(t, resume, "resume was expected")
	mustTrue(t, errors.Is(err, sendErr), "expected the send error to propagate")
}

func TestReleaseRestHijackAndResumeReportsLostWorker(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", Owner: "op", LeaseExpiresAt: clk.Monotonic() + 60}
	st.WorkerWS = nil
	h.lock.Unlock()

	released, resume, err := h.ReleaseRestHijackAndResume(bg(), "w", "hj")
	mustTrue(t, released, "release must be reported even when the worker is gone")
	mustTrue(t, resume, "resume was expected")
	mustTrue(t, errors.Is(err, context.Canceled), "expected context.Canceled for an undelivered resume")
}
