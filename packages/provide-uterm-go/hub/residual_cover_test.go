//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"image"
	"math"
	"strings"
	"sync"
	"testing"
)

var errBrowserGone = errors.New("browser gone")

// hookBrowser is a fakeBrowserWS whose sends run onSend first; a non-nil
// return fails that send.
type hookBrowser struct {
	*fakeBrowserWS
	onSend func(payload string) error
}

func newHookBrowser(name string) *hookBrowser { return &hookBrowser{fakeBrowserWS: newBrowserWS(name)} }

func (b *hookBrowser) SendText(ctx context.Context, payload string) error {
	if b.onSend != nil {
		if err := b.onSend(payload); err != nil {
			return err
		}
	}
	return b.fakeBrowserWS.SendText(ctx, payload)
}

// breakBroadcastOn makes b's send of any payload containing match fail AND, in
// the same instant, park a fresh input reservation on workerID and cancel the
// caller's context. Broadcast then prunes b as dead, and that pruning has to
// wait out the reservation under a cancelled context: the broadcast fails with
// the context's error. This is how a broadcast fails in production -- the
// caller gave up while a dead socket was being cleaned up after.
func breakBroadcastOn(h *TermHub, b *hookBrowser, workerID, match string, cancel context.CancelFunc) {
	var once sync.Once
	b.onSend = func(payload string) error {
		if !strings.Contains(payload, match) {
			return nil
		}
		once.Do(func() {
			h.lock.Lock()
			if st := h.registry.Get(workerID); st != nil && st.InputSendPending == nil {
				st.InputSendPending = &InputSendReservation{Done: make(chan struct{})}
			}
			h.lock.Unlock()
			cancel()
		})
		return errBrowserGone
	}
}

func TestCloneApprovalRequestCopiesGroupID(t *testing.T) {
	s := NewInMemoryApprovalStore(nil)
	group := "g1"
	if !s.Add(&ApprovalRequest{ID: "r1", GroupID: &group}) {
		t.Fatal("add")
	}
	got := s.Get("r1")
	if got.GroupID == nil || *got.GroupID != "g1" || got.GroupID == &group {
		t.Fatalf("GroupID = %v, want an independent copy of g1", got.GroupID)
	}
	*got.GroupID = "mutated"
	if *s.Get("r1").GroupID != "g1" {
		t.Fatal("mutating a returned copy changed the stored request")
	}
}

func parkedHub(t *testing.T) (*TermHub, *ManualClock, *fakeWorkerWS, *hookBrowser, string) {
	t.Helper()
	h, clk := newTestHub(t, func(c *TermHubConfig) { c.MaxInputChars = 100; c.MaxBufferChars = 100 })
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newHookBrowser("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	reqID, err := h.ParkBrowserForApproval(bg(), "w", b, "rm -rf /", PolicyDecision{Action: "hold", TimeoutS: 60})
	if err != nil {
		t.Fatalf("park: %v", err)
	}
	return h, clk, worker, b, reqID
}

func TestTryHoldBrowserInputRejectsOverlongHold(t *testing.T) {
	h, _, _, b, _ := parkedHub(t)
	held, tooLong := h.TryHoldBrowserInput(b, strings.Repeat("x", 101))
	if !held || !tooLong {
		t.Fatalf("TryHoldBrowserInput = held %v tooLong %v, want both", held, tooLong)
	}
	if h.holdBuffers[b] != "" {
		t.Fatalf("an overlong hold must not be buffered, got %q", h.holdBuffers[b])
	}
}

type failingIdentity struct{ err error }

func (f failingIdentity) ResolvePrincipal(context.Context, BrowserConn) (any, error) {
	return nil, f.err
}

func TestInterceptBrowserInputPropagatesIdentityFailure(t *testing.T) {
	boom := errors.New("idp down")
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.IdentityProvider = failingIdentity{boom} })
	registerWorkerState(h, "w", &fakeWorkerWS{})
	_, err := h.InterceptBrowserInput(bg(), "w", newBrowserWS("b"), "ls")
	if !errors.Is(err, boom) {
		t.Fatalf("InterceptBrowserInput = %v, want the identity failure", err)
	}
}

func TestParkBrowserForApprovalRecordsSubmitterAndFences(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	b.principal = &Principal{SubjectID: "alice"}
	registerActiveBrowser(t, h, "w", b, "admin")
	gen := h.registry.Get("w").HijackOwnerGeneration

	reqID, err := h.ParkBrowserForApproval(bg(), "w", b, "ls", PolicyDecision{TimeoutS: 60}, gen)
	if err != nil {
		t.Fatal(err)
	}
	if got := h.Approvals.Get(reqID).SubmitterID; got != "alice" {
		t.Fatalf("SubmitterID = %q, want alice", got)
	}
	// A stale ownership generation is refused.
	if _, err := h.ParkBrowserForApproval(bg(), "w", b, "ls", PolicyDecision{TimeoutS: 60}, gen+1); !errors.Is(err, ErrApprovalOwnershipInvalid) {
		t.Fatalf("stale generation = %v", err)
	}
	// A caller-chosen id that already exists is refused, not overwritten.
	dup := reqID
	if _, err := h.ParkBrowserForApproval(bg(), "w", b, "rm", PolicyDecision{TimeoutS: 60, RequestID: &dup}); !errors.Is(err, ErrApprovalDuplicateID) {
		t.Fatalf("duplicate id = %v", err)
	}
	if h.Approvals.Get(reqID).Command != "ls" {
		t.Fatal("the duplicate replaced the original request")
	}
}

// hookWorker is a fakeWorkerWS whose sends run onSend first.
type hookWorker struct {
	*fakeWorkerWS
	onSend func() error
}

func (w *hookWorker) SendText(ctx context.Context, payload string) error {
	if w.onSend != nil {
		if err := w.onSend(); err != nil {
			return err
		}
	}
	return w.fakeWorkerWS.SendText(ctx, payload)
}

// An approve whose injection fails while the request is pruned underneath it
// reports the claim as handled without broadcasting an outcome for a record
// that no longer exists.
func TestResolveApprovalRefusedAfterRecordPruned(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &hookWorker{fakeWorkerWS: &fakeWorkerWS{}}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	reqID, err := h.ParkBrowserForApproval(bg(), "w", b, "ls", PolicyDecision{TimeoutS: 60})
	if err != nil {
		t.Fatal(err)
	}
	worker.onSend = func() error {
		clk.SetWall(clk.Wall() + 60 + approvalPruneTTL + 1)
		h.Approvals.CleanupExpired()
		return errors.New("worker gone")
	}
	before := b.count()
	ok, err := h.ResolveApproval(bg(), reqID, true, nil, nil)
	if !ok || err != nil {
		t.Fatalf("ResolveApproval = %v, %v; want handled", ok, err)
	}
	if h.Approvals.Get(reqID) != nil {
		t.Fatal("request should have been pruned")
	}
	for _, p := range b.payloads()[before:] {
		if strings.Contains(p, "approval_resolved") {
			t.Fatalf("an outcome was broadcast for a pruned request: %q", p)
		}
	}
}

func TestResolveApprovalRejectBroadcastFailure(t *testing.T) {
	h, _, _, b, reqID := parkedHub(t)
	ctx, cancel := context.WithCancel(bg())
	defer cancel()
	breakBroadcastOn(h, b, "w", "[REJECTED]", cancel)
	ok, err := h.ResolveApproval(ctx, reqID, false, nil, nil)
	if !ok || !errors.Is(err, context.Canceled) {
		t.Fatalf("ResolveApproval = %v, %v; want claimed with the broadcast failure", ok, err)
	}
}

func TestResolveApprovalOutcomeBroadcastFailure(t *testing.T) {
	h, _, _, b, reqID := parkedHub(t)
	ctx, cancel := context.WithCancel(bg())
	defer cancel()
	breakBroadcastOn(h, b, "w", "approval_resolved", cancel)
	ok, err := h.ResolveApproval(ctx, reqID, false, nil, nil)
	if !ok || !errors.Is(err, context.Canceled) {
		t.Fatalf("ResolveApproval = %v, %v; want claimed with the broadcast failure", ok, err)
	}
	if got := h.Approvals.Get(reqID).Status; got != ApprovalRejected {
		t.Fatalf("status = %v, want rejected even though the broadcast failed", got)
	}
}

func TestSetInputModeBroadcastFailures(t *testing.T) {
	for _, match := range []string{"input_mode_changed", "hijack_state"} {
		t.Run(match, func(t *testing.T) {
			h, _ := newTestHub(t, nil)
			registerWorkerState(h, "w", &fakeWorkerWS{})
			b := newHookBrowser("b1")
			registerActiveBrowser(t, h, "w", b, "admin")
			ctx, cancel := context.WithCancel(bg())
			defer cancel()
			breakBroadcastOn(h, b, "w", match, cancel)
			ok, _, err := h.SetInputMode(ctx, "w", "hijack")
			if ok || !errors.Is(err, context.Canceled) {
				t.Fatalf("SetInputMode = %v, %v; want the broadcast failure", ok, err)
			}
		})
	}
}

func TestDisconnectWorkerBroadcastFailures(t *testing.T) {
	for _, match := range []string{"worker_disconnected", "hijack_state"} {
		t.Run(match, func(t *testing.T) {
			h, _ := newTestHub(t, nil)
			registerWorkerState(h, "w", &fakeWorkerWS{})
			b := newHookBrowser("b1")
			registerActiveBrowser(t, h, "w", b, "admin")
			ctx, cancel := context.WithCancel(bg())
			defer cancel()
			breakBroadcastOn(h, b, "w", match, cancel)
			ok, err := h.DisconnectWorker(ctx, "w")
			if ok || !errors.Is(err, context.Canceled) {
				t.Fatalf("DisconnectWorker = %v, %v; want the broadcast failure", ok, err)
			}
		})
	}
}

func TestForceReleaseHijackBroadcastFailure(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newHookBrowser("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	ctx, cancel := context.WithCancel(bg())
	defer cancel()
	breakBroadcastOn(h, b, "w", "hijack_state", cancel)
	ok, err := h.ForceReleaseHijack(ctx, "w")
	if ok || !errors.Is(err, context.Canceled) {
		t.Fatalf("ForceReleaseHijack = %v, %v; want the broadcast failure", ok, err)
	}
}

func TestSubscriptionPendingHonoursOverride(t *testing.T) {
	bus := NewEventBus(EventBusOptions{})
	sub, remove, err := bus.Watch("w", nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer remove()
	bus.Enqueue("w", map[string]any{"type": "term"})
	if sub.Pending() != 1 {
		t.Fatalf("Pending = %d, want the queue length 1", sub.Pending())
	}
	bus.PendingOverride = func(workerID string) (int, bool) { return 7, workerID == "w" }
	if sub.Pending() != 7 {
		t.Fatalf("Pending = %d, want the override", sub.Pending())
	}
	bus.PendingOverride = func(string) (int, bool) { return 0, false }
	if sub.Pending() != 1 {
		t.Fatalf("Pending = %d, want the queue length when the override declines", sub.Pending())
	}
	if (&Subscription{Queue: make(chan map[string]any, 1)}).Pending() != 0 {
		t.Fatal("a bus-less subscription reports its queue length")
	}
}

func TestValidateWatchPattern(t *testing.T) {
	if err := ValidateWatchPattern(`^\$ $`); err != nil {
		t.Fatalf("a plain pattern is valid: %v", err)
	}
	if err := ValidateWatchPattern(`(a*)*`); err == nil {
		t.Fatal("a nested quantifier must be refused")
	}
}

// A rule nested just inside RE2's depth limit compiles alone, but the combined
// pattern (two such rules, each wrapped, under an alternation) nests deeper
// and cannot compile; the redactor falls
// back to identity rather than panicking.
func TestNewStreamRedactorFallsBackWhenTheCombinationCannotCompile(t *testing.T) {
	deep := strings.Repeat("(", 998) + "a" + strings.Repeat(")", 998)
	r := NewStreamRedactor([]RedactionRule{{Pattern: deep}, {Pattern: deep}})
	if r.pattern != nil {
		t.Fatal("an uncompilable combination must leave the redactor without a pattern")
	}
}

type closeTrackingGraphical struct{ closed int }

func (*closeTrackingGraphical) Screenshot() (image.Image, error)    { return nil, nil }
func (*closeTrackingGraphical) InjectPointer(int, int, uint8) error { return nil }
func (*closeTrackingGraphical) InjectKey(uint32, bool) error        { return nil }

func (c *closeTrackingGraphical) Close() error {
	c.closed++
	return nil
}

func TestRegistryClosesGraphicalSessionOnRemoval(t *testing.T) {
	r := NewWorkerRegistry()
	for _, remove := range []func(string){
		func(id string) { r.Pop(id) },
		func(id string) { r.Discard(id) },
	} {
		g := &closeTrackingGraphical{}
		st := NewWorkerTermState()
		st.GraphicalSession = g
		r.Put("w", st)
		remove("w")
		if g.closed != 1 {
			t.Fatalf("graphical session closed %d times, want 1", g.closed)
		}
	}
}

func TestHasWorkerHello(t *testing.T) {
	h, _ := newTestHub(t, nil)
	if h.HasWorkerHello("w") {
		t.Fatal("an unknown worker has no hello")
	}
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	if h.HasWorkerHello("w") {
		t.Fatal("a worker before its hello reports none")
	}
	h.lock.Lock()
	st.HelloApplied = true
	h.lock.Unlock()
	if !h.HasWorkerHello("w") {
		t.Fatal("an applied hello is reported")
	}
}

func TestWaitResumeTokenReadyUnknownToken(t *testing.T) {
	h, _ := newTestHub(t, nil)
	if err := h.WaitResumeTokenReady(bg(), "never-issued", newBrowserWS("b")); err != nil {
		t.Fatalf("an unknown token has nothing to wait for: %v", err)
	}
}

func TestAuditAllBrowsersSkipsEmptyRegistryEntries(t *testing.T) {
	h, _ := newTestHub(t, nil)
	h.registry.Put("ghost", nil)
	if err := h.Router.AuditAllBrowsers(bg()); err != nil {
		t.Fatalf("AuditAllBrowsers = %v", err)
	}
}

func TestAuditAllBrowsersPropagatesFailures(t *testing.T) {
	boom := errors.New("idp down")
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.IdentityProvider = failingIdentity{boom} })
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	st.Browsers[newBrowserWS("b")] = "viewer"
	if err := h.Router.AuditAllBrowsers(bg()); !errors.Is(err, boom) {
		t.Fatalf("AuditAllBrowsers = %v, want the identity failure", err)
	}
}

func TestBroadcastRefusesAnUnencodableFrame(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b")
	st.Browsers[b] = "viewer"
	if err := h.Router.Broadcast(bg(), "w", map[string]any{"type": "x", "v": math.NaN()}); err == nil {
		t.Fatal("an unencodable frame must fail the broadcast")
	}
	if b.count() != 0 {
		t.Fatal("nothing may be sent for an unencodable frame")
	}
}

func TestBroadcastHijackStateRefusesAnUnencodableLease(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b")
	st.Browsers[b] = "viewer"
	nan := math.NaN()
	st.HijackOwnerExpiresAt = &nan
	if err := h.Router.BroadcastHijackState(bg(), "w"); err == nil {
		t.Fatal("a NaN lease expiry cannot be rendered and must fail the broadcast")
	}
}

func TestRecheckAndResumeForwardsToLease(t *testing.T) {
	h, clk := newTestHub(t, nil)
	if err := h.RecheckAndResume(bg(), "missing", clk.Monotonic()); err != nil {
		t.Fatalf("RecheckAndResume = %v", err)
	}
}

func TestGetRestSessionCancelledWhileReservationPending(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 100}
	release := blockPending(t, h, "w")
	defer release()
	ctx, cancel := context.WithCancel(bg())
	cancel()
	if _, err := h.GetRestSession(ctx, "w", "hj"); !errors.Is(err, context.Canceled) {
		t.Fatalf("GetRestSession = %v, want the cancellation", err)
	}
}

// An approve that cannot even reserve the worker (the caller gave up while
// another send was in flight) is refused and returns the cancellation.
func TestResolveApprovalApproveCancelledBeforeReservation(t *testing.T) {
	h, _, _, _, reqID := parkedHub(t)
	release := blockPending(t, h, "w")
	defer release()
	ctx, cancel := context.WithCancel(bg())
	cancel()
	ok, err := h.ResolveApproval(ctx, reqID, true, nil, nil)
	if !ok || !errors.Is(err, context.Canceled) {
		t.Fatalf("ResolveApproval = %v, %v; want claimed with the cancellation", ok, err)
	}
	if got := h.Approvals.Get(reqID).Status; got != ApprovalRefused {
		t.Fatalf("status = %v, want refused", got)
	}
}

func TestLastPrincipalConnectionReleasesItsQuotaSlot(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.MaxConnectionsPerPrincipal = 1 })
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b1")
	b.principal = &Principal{SubjectID: "alice"}
	if _, err := h.RegisterBrowser(bg(), "w", b, "viewer", false); err != nil {
		t.Fatal(err)
	}
	if _, err := h.CleanupBrowserDisconnect(bg(), "w", b, false); err != nil {
		t.Fatal(err)
	}
	if _, tracked := h.principalBrowserCounts["alice"]; tracked {
		t.Fatal("a principal with no connections left must not keep a count")
	}
	again := newBrowserWS("b2")
	again.principal = &Principal{SubjectID: "alice"}
	if _, err := h.RegisterBrowser(bg(), "w", again, "viewer", false); err != nil {
		t.Fatalf("the freed slot must be reusable: %v", err)
	}
}

// waitThenRelease drives a raw `<-done` reservation wait deterministically:
// the unbuffered send completes only once the operation is parked on done,
// after which the reservation is cleared and done closed.
func waitThenRelease(h *TermHub, st *WorkerTermState, done chan struct{}) {
	done <- struct{}{}
	h.lock.Lock()
	st.InputSendPending = nil
	h.lock.Unlock()
	close(done)
}

func TestTryReleaseWsWaitsOutAnInFlightSend(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b")
	registerActiveBrowser(t, h, "w", b, "admin")
	done := make(chan struct{})
	h.lock.Lock()
	st.InputSendPending = &InputSendReservation{Done: done}
	h.lock.Unlock()
	result := make(chan bool, 1)
	go func() {
		released, _ := h.Lease.TryReleaseWs("w", b)
		result <- released
	}()
	waitThenRelease(h, st, done)
	if !<-result {
		t.Fatal("the owner's release must succeed once the send completes")
	}
}

func TestReleaseRestWaitsOutAnInFlightSend(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	done := make(chan struct{})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() + 100}
	st.InputSendPending = &InputSendReservation{Done: done}
	h.lock.Unlock()
	result := make(chan bool, 1)
	go func() {
		released, _ := h.Lease.ReleaseRest("w", "hj")
		result <- released
	}()
	waitThenRelease(h, st, done)
	if !<-result {
		t.Fatal("the REST release must succeed once the send completes")
	}
}

func TestTryAcquireWsWaitsForAPendingLifecycle(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b")
	if _, err := h.RegisterBrowser(bg(), "w", b, "admin", false); err != nil {
		t.Fatal(err)
	}
	lifecycle := &LifecycleReservation{Kind: "test", Done: make(chan struct{})}
	h.lock.Lock()
	st.LifecyclePending = lifecycle
	h.lock.Unlock()

	cancelled, cancel := context.WithCancel(bg())
	cancel()
	if ok, reason := h.Lease.TryAcquireWsContext(cancelled, "w", b); ok || reason != "cancelled" {
		t.Fatalf("cancelled acquire = %v %q", ok, reason)
	}

	ctx, reached := WithReservationWaitBarrier(bg())
	result := make(chan bool, 1)
	go func() {
		ok, _ := h.Lease.TryAcquireWsContext(ctx, "w", b)
		result <- ok
	}()
	awaitBarrier(t, reached)
	h.lock.Lock()
	st.LifecyclePending = nil
	h.lock.Unlock()
	close(lifecycle.Done)
	if !<-result {
		t.Fatal("the acquire must go through once the lifecycle step finishes")
	}
}

// openModeWorker flips the worker to open input mode while the REST acquire's
// pause is on the wire.
type openModeWorker struct {
	*fakeWorkerWS
	h *TermHub
}

func (w *openModeWorker) SendText(ctx context.Context, payload string) error {
	if strings.Contains(payload, "pause") {
		w.h.lock.Lock()
		w.h.registry.Get("w").InputMode = InputModeOpen
		w.h.lock.Unlock()
	}
	return w.fakeWorkerWS.SendText(ctx, payload)
}

func TestTryAcquireRestRefusedWhenModeOpensDuringPause(t *testing.T) {
	h, clk := newTestHub(t, nil)
	registerWorkerState(h, "w", &openModeWorker{fakeWorkerWS: &fakeWorkerWS{}, h: h})
	ok, reason, err := h.Lease.TryAcquireRest(bg(), "w", "op", 60, "hj", clk.Monotonic())
	if ok || reason != "open_mode" || err != nil {
		t.Fatalf("TryAcquireRest = %v %q %v; want open_mode", ok, reason, err)
	}
	if h.registry.Get("w").HijackSession != nil {
		t.Fatal("no session may be granted in open mode")
	}
}

func TestExpireLeasesUnderLockFinishesItsLifecycle(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	h.lock.Lock()
	st.HijackSession = &HijackSession{HijackID: "hj", LeaseExpiresAt: clk.Monotonic() - 1}
	h.lock.Unlock()
	rest, _, resume, ok := h.Lease.expireLeasesUnderLock("w", clk.Monotonic())
	if !rest || !resume || !ok {
		t.Fatalf("expire = rest %v resume %v ok %v", rest, resume, ok)
	}
	if h.registry.Get("w").LifecyclePending != nil {
		t.Fatal("the resume lifecycle must be finished, not left pending")
	}
}

func TestWaitResumeTokenReadyAfterDetach(t *testing.T) {
	h, _ := newTestHub(t, nil)
	done := make(chan struct{})
	close(done)
	h.lock.Lock()
	h.resumeTokenDetached["tok"] = done
	h.lock.Unlock()
	if err := h.WaitResumeTokenReady(bg(), "tok", newBrowserWS("b")); err != nil {
		t.Fatalf("a detached token is ready: %v", err)
	}
}

type failingAuditGate struct{ err error }

func (g failingAuditGate) AuditConnection(context.Context, ConnectionHeuristics, PolicyContext, BehavioralThresholds) (PolicyDecision, error) {
	return PolicyDecision{}, g.err
}

func TestAuditAllBrowsersPropagatesGateFailure(t *testing.T) {
	boom := errors.New("audit backend down")
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.BehavioralAuditGate = failingAuditGate{boom} })
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	st.Browsers[newBrowserWS("b")] = "viewer"
	if err := h.Router.AuditAllBrowsers(bg()); !errors.Is(err, boom) {
		t.Fatalf("AuditAllBrowsers = %v, want the gate failure", err)
	}
}

func TestStartupBufferCoalescesSnapshotsKeepingOtherFrames(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newBrowserWS("b")
	if _, err := h.RegisterBrowser(bg(), "w", b, "viewer", true); err != nil {
		t.Fatal(err)
	}
	for _, msg := range []map[string]any{
		{"type": "snapshot", "screen": "one"},
		{"type": "presence_sync", "n": 1.0},
		{"type": "snapshot", "screen": "two"},
	} {
		if err := h.Router.Broadcast(bg(), "w", msg); err != nil {
			t.Fatal(err)
		}
	}
	h.lock.Lock()
	queued := h.startupPendingFrames[b]
	h.lock.Unlock()
	if len(queued) != 2 || queued[0]["type"] != "presence_sync" || queued[1]["screen"] != "two" {
		t.Fatalf("queued = %v, want the presence frame then only the newest snapshot", queued)
	}
}

type rulesGate struct{ rules []RedactionRule }

func (g rulesGate) GetRedactionRules(context.Context, PolicyContext) ([]RedactionRule, error) {
	return g.rules, nil
}

func TestBroadcastWithOutputGate(t *testing.T) {
	t.Run("no rules sends the shared payload", func(t *testing.T) {
		h, _ := newTestHub(t, func(c *TermHubConfig) { c.OutputPolicyGate = rulesGate{} })
		st := registerWorkerState(h, "w", &fakeWorkerWS{})
		b := newBrowserWS("b")
		st.Browsers[b] = "viewer"
		if err := h.Router.Broadcast(bg(), "w", map[string]any{"type": "term", "data": "secret"}); err != nil {
			t.Fatal(err)
		}
		if got := decodeTerminalData(t, b.last()); got != "secret" {
			t.Fatalf("sent %q", got)
		}
	})
	t.Run("a redaction that cannot encode fails the broadcast", func(t *testing.T) {
		h, _ := newTestHub(t, func(c *TermHubConfig) {
			c.OutputPolicyGate = rulesGate{rules: []RedactionRule{{Pattern: "secret"}}}
			c.Redactor = func(map[string]any, []RedactionRule) map[string]any {
				return map[string]any{"type": "analysis", "v": math.NaN()}
			}
		})
		st := registerWorkerState(h, "w", &fakeWorkerWS{})
		b := newBrowserWS("b")
		st.Browsers[b] = "viewer"
		if err := h.Router.Broadcast(bg(), "w", map[string]any{"type": "term", "data": "secret"}); err == nil {
			t.Fatal("an unencodable redaction must fail the broadcast")
		}
		if b.count() != 0 {
			t.Fatal("nothing may be sent when the redaction cannot be encoded")
		}
	})
}

// A worker removed while its dead browsers are being pruned leaves no one to
// re-broadcast hijack state to.
func TestBroadcastHijackStateWorkerGoneAfterPrune(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := registerWorkerState(h, "w", &fakeWorkerWS{})
	b := newHookBrowser("b")
	b.onSend = func(string) error {
		h.lock.Lock()
		h.registry.Pop("w")
		h.lock.Unlock()
		return errBrowserGone
	}
	st.Browsers[b] = "viewer"
	if err := h.Router.BroadcastHijackState(bg(), "w"); err != nil {
		t.Fatalf("BroadcastHijackState = %v", err)
	}
	if h.registry.Get("w") != nil {
		t.Fatal("worker should be gone")
	}
}

// A worker message that cannot be encoded is not delivered, and the worker
// socket is treated as failed.
func TestSendWorkerUnencodableMessage(t *testing.T) {
	h, _ := newTestHub(t, nil)
	w := &fakeWorkerWS{}
	registerWorkerState(h, "w", w)
	ok, err := h.Router.SendWorker(bg(), "w", map[string]any{"type": "control", "v": math.NaN()}, nil)
	if ok || err != nil {
		t.Fatalf("SendWorker = %v, %v; want undelivered", ok, err)
	}
	if len(w.payloads()) != 0 || h.registry.Get("w").WorkerWS != nil {
		t.Fatal("nothing may be sent, and the failed socket must be dropped")
	}
}
