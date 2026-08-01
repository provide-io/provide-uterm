//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

// holdGate is a PolicyGate that holds every input for approval.
type holdGate struct{}

func (holdGate) InterceptInput(_ context.Context, _ string, _ PolicyContext) (PolicyDecision, error) {
	return PolicyDecision{Action: "hold", TimeoutS: 60}, nil
}

// registerActiveBrowser registers ws for workerID and activates its broadcasts.
func registerActiveBrowser(t *testing.T, h *TermHub, workerID string, ws BrowserConn, role string) {
	t.Helper()
	if _, err := h.RegisterBrowser(bg(), workerID, ws, role, true); err != nil {
		t.Fatalf("RegisterBrowser: %v", err)
	}
	h.ActivateBrowserBroadcasts(bg(), workerID, ws)
	if ok, reason := h.TryAcquireWsHijack(bg(), workerID, ws); !ok {
		t.Fatalf("TryAcquireWsHijack: %s", reason)
	}
}

// findControl decodes the first control payload of typ in a browser's payloads.
func findControl(t *testing.T, payloads []string, typ string) map[string]any {
	t.Helper()
	for _, p := range payloads {
		if !strings.HasPrefix(p, controlPrefix) {
			continue
		}
		m := decodeOneControl(t, p)
		if m["type"] == typ {
			return m
		}
	}
	t.Fatalf("no control frame of type %q in %d payloads", typ, len(payloads))
	return nil
}

// controlPrefix is the DLE STX inline control-frame magic.
const controlPrefix = "\x10\x02"

func TestResolveApprovalApproveInjectsCommandOnce(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")

	reqID, err := h.ParkBrowserForApproval(bg(), "w", b, "rm -rf /", PolicyDecision{Action: "hold", TimeoutS: 60})
	if err != nil {
		t.Fatalf("ParkBrowserForApproval: %v", err)
	}
	if worker.last() != "" {
		t.Fatalf("park must not touch the worker; got %q", worker.last())
	}
	if !h.IsBrowserParked(b) {
		t.Fatal("browser should be parked after ParkBrowserForApproval")
	}
	// approval_pending fanned out to the browser.
	pending := findControl(t, b.payloads(), "approval_pending")
	if pending["request_id"] != reqID || pending["command"] != "rm -rf /" {
		t.Fatalf("approval_pending frame: %v", pending)
	}

	ok, err := h.ResolveApproval(bg(), reqID, true, nil, &Principal{SubjectID: "admin"})
	if err != nil || !ok {
		t.Fatalf("ResolveApproval approve: ok=%v err=%v", ok, err)
	}
	if got := worker.payloads(); len(got) != 1 {
		t.Fatalf("expected exactly one worker injection, got %d: %v", len(got), got)
	}
	if cmd := decodeTerminalData(t, worker.last()); cmd != "rm -rf /" {
		t.Fatalf("injected command = %q, want %q", cmd, "rm -rf /")
	}
	if h.IsBrowserParked(b) {
		t.Fatal("browser should be released after approval")
	}
	resolved := findControl(t, b.payloads(), "approval_resolved")
	if resolved["outcome"] != "approved" || resolved["request_id"] != reqID {
		t.Fatalf("approval_resolved frame: %v", resolved)
	}
}

func TestResolveApprovalRejectSendsMessageNoInjection(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")

	reqID, _ := h.ParkBrowserForApproval(bg(), "w", b, "sudo halt", PolicyDecision{Action: "hold", TimeoutS: 60})

	ok, err := h.ResolveApproval(bg(), reqID, false, strp("policy"), &Principal{SubjectID: "admin"})
	if err != nil || !ok {
		t.Fatalf("ResolveApproval reject: ok=%v err=%v", ok, err)
	}
	if got := worker.payloads(); len(got) != 0 {
		t.Fatalf("reject must not inject to worker, got %v", got)
	}
	// The red rejection banner ships as raw terminal data.
	var banner string
	for _, p := range b.payloads() {
		if !strings.HasPrefix(p, controlPrefix) {
			banner = decodeTerminalData(t, p)
		}
	}
	if !strings.Contains(banner, "[REJECTED] Command 'sudo halt' blocked by Admin.") {
		t.Fatalf("rejection banner = %q", banner)
	}
	if !strings.Contains(banner, "Reason: policy") {
		t.Fatalf("rejection banner missing reason: %q", banner)
	}
	resolved := findControl(t, b.payloads(), "approval_resolved")
	if resolved["outcome"] != "rejected" {
		t.Fatalf("approval_resolved outcome = %v, want rejected", resolved["outcome"])
	}
}

func TestResolveApprovalDoubleResolveIsNoOp(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	reqID, _ := h.ParkBrowserForApproval(bg(), "w", b, "echo hi", PolicyDecision{Action: "hold", TimeoutS: 60})

	ok1, _ := h.ResolveApproval(bg(), reqID, true, nil, nil)
	ok2, _ := h.ResolveApproval(bg(), reqID, true, nil, nil)
	ok3, _ := h.ResolveApproval(bg(), reqID, false, nil, nil)
	if !ok1 || ok2 || ok3 {
		t.Fatalf("claim-once semantics broken: ok1=%v ok2=%v ok3=%v", ok1, ok2, ok3)
	}
	if got := worker.payloads(); len(got) != 1 {
		t.Fatalf("double-resolve must inject exactly once, got %d", len(got))
	}
	// Unknown request id resolves to a no-op.
	if ok, _ := h.ResolveApproval(bg(), "does-not-exist", true, nil, nil); ok {
		t.Fatal("unknown request id must be a no-op")
	}
}

func TestResolveApprovalReleasesHoldBuffer(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	reqID, _ := h.ParkBrowserForApproval(bg(), "w", b, "cmd\n", PolicyDecision{Action: "hold", TimeoutS: 60})

	// Extra keystrokes typed while parked are buffered, not forwarded.
	if tooLong := h.HoldBrowserInput(b, "extra\n"); tooLong {
		t.Fatal("hold buffer should accept small input")
	}
	if got := worker.payloads(); len(got) != 0 {
		t.Fatalf("held input must not reach the worker yet, got %v", got)
	}

	ok, err := h.ResolveApproval(bg(), reqID, true, nil, nil)
	if err != nil || !ok {
		t.Fatalf("ResolveApproval: ok=%v err=%v", ok, err)
	}
	// The original command, then the replayed hold buffer.
	got := worker.payloads()
	if len(got) != 2 {
		t.Fatalf("expected command + replayed buffer, got %d: %v", len(got), got)
	}
	if decodeTerminalData(t, got[0]) != "cmd\n" || decodeTerminalData(t, got[1]) != "extra\n" {
		t.Fatalf("replay order wrong: %q then %q", decodeTerminalData(t, got[0]), decodeTerminalData(t, got[1]))
	}
}

func TestResolveApprovalAfterOwnershipLossRefusesCommandAndReplay(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	reqID, err := h.ParkBrowserForApproval(bg(), "w", b, "cmd\n", PolicyDecision{Action: "hold", TimeoutS: 60})
	if err != nil {
		t.Fatalf("ParkBrowserForApproval: %v", err)
	}
	if tooLong := h.HoldBrowserInput(b, "buffered\n"); tooLong {
		t.Fatal("hold buffer should accept small input")
	}
	if released, _ := h.TryReleaseWsHijack(bg(), "w", b); !released {
		t.Fatal("owner release failed")
	}

	resolved, err := h.ResolveApproval(bg(), reqID, true, nil, &Principal{SubjectID: "approver"})
	if !resolved || err == nil {
		t.Fatalf("approval after ownership loss = resolved:%t err:%v, want terminal refusal", resolved, err)
	}
	if got := worker.payloads(); len(got) != 0 {
		t.Fatalf("ownership-lost approval delivered command or replay: %v", got)
	}
	if status := string(h.Approvals.Get(reqID).Status); status != "refused" {
		t.Fatalf("terminal approval status = %q, want refused", status)
	}
}

func TestResolveApprovalAfterSameBrowserReacquiresRefusesStaleGeneration(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	reqID, err := h.ParkBrowserForApproval(bg(), "w", b, "stale\n", PolicyDecision{Action: "hold", TimeoutS: 60})
	if err != nil {
		t.Fatal(err)
	}
	if released, _ := h.TryReleaseWsHijack(bg(), "w", b); !released {
		t.Fatal("release failed")
	}
	if ok, reason := h.TryAcquireWsHijack(bg(), "w", b); !ok {
		t.Fatalf("same-browser reacquire failed: %s", reason)
	}
	resolved, err := h.ResolveApproval(bg(), reqID, true, nil, nil)
	if !resolved || err == nil {
		t.Fatalf("stale-generation approval = resolved:%t err:%v", resolved, err)
	}
	if got := worker.payloads(); len(got) != 0 {
		t.Fatalf("stale-generation command reached worker: %v", got)
	}
}

type orderedApprovalWorker struct {
	mu       sync.Mutex
	payloads []string
	entered  chan struct{}
	release  chan struct{}
	once     sync.Once
}

func (w *orderedApprovalWorker) SendText(ctx context.Context, payload string) error {
	w.mu.Lock()
	w.payloads = append(w.payloads, payload)
	index := len(w.payloads)
	w.mu.Unlock()
	if index == 1 {
		w.once.Do(func() { close(w.entered) })
		select {
		case <-w.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func (w *orderedApprovalWorker) decoded(t *testing.T) []string {
	t.Helper()
	w.mu.Lock()
	defer w.mu.Unlock()
	out := make([]string, 0, len(w.payloads))
	for _, payload := range w.payloads {
		out = append(out, decodeTerminalData(t, payload))
	}
	return out
}

func TestApprovalCommandAndReplayCannotBeOvertakenByFreshInput(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &orderedApprovalWorker{entered: make(chan struct{}), release: make(chan struct{})}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	reqID, err := h.ParkBrowserForApproval(bg(), "w", b, "command\n", PolicyDecision{Action: "hold", TimeoutS: 60})
	if err != nil {
		t.Fatal(err)
	}
	h.HoldBrowserInput(b, "replay\n")

	resolveDone := make(chan error, 1)
	go func() {
		_, err := h.ResolveApproval(bg(), reqID, true, nil, nil)
		resolveDone <- err
	}()
	<-worker.entered
	generation, ok := h.BrowserInputFence("w", b)
	if !ok {
		t.Fatal("browser fence unavailable")
	}
	freshDone := make(chan bool, 1)
	waitCtx, reachedWait := WithReservationWaitBarrier(bg())
	go func() {
		sent, _ := h.SendBrowserOwnedInputAtGeneration(waitCtx, "w", b, generation,
			map[string]any{"type": "input", "data": "fresh\n"})
		freshDone <- sent
	}()
	select {
	case <-reachedWait:
	case sent := <-freshDone:
		t.Fatalf("fresh input completed before reservation wait: %t", sent)
	case <-time.After(5 * time.Second):
		t.Fatal("fresh input did not reach approval batch reservation wait")
	}
	close(worker.release)
	if err := <-resolveDone; err != nil {
		t.Fatal(err)
	}
	if sent := <-freshDone; !sent {
		t.Fatal("fresh input was not delivered after the approval batch")
	}
	want := []string{"command\n", "replay\n", "fresh\n"}
	if got := worker.decoded(t); !reflect.DeepEqual(got, want) {
		t.Fatalf("delivery order = %q, want %q", got, want)
	}
}

func TestResolveApprovalFinalCASDoesNotPublishForReusedID(t *testing.T) {
	h, clock := newTestHub(t, nil)
	worker := &orderedApprovalWorker{entered: make(chan struct{}), release: make(chan struct{})}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	requestID := "reused"
	reqID, err := h.ParkBrowserForApproval(bg(), "w", b, "old-command\n", PolicyDecision{
		Action: "hold", TimeoutS: 60, RequestID: &requestID,
	})
	if err != nil || reqID != requestID {
		t.Fatalf("park = id:%q err:%v", reqID, err)
	}
	resolveDone := make(chan error, 1)
	go func() {
		_, err := h.ResolveApproval(bg(), requestID, true, nil, nil)
		resolveDone <- err
	}()
	<-worker.entered
	clock.SetWall(5000 + 60 + approvalPruneTTL + 1)
	h.Approvals.CleanupExpired()
	replacement := pendingReq(requestID, clock.Wall()+60)
	replacement.WorkerID = "replacement-worker"
	replacement.Command = "replacement"
	if !h.Approvals.Add(replacement) {
		t.Fatal("replacement approval was not added after prune")
	}
	close(worker.release)
	if err := <-resolveDone; err != nil {
		t.Fatal(err)
	}
	if current := h.Approvals.Get(requestID); current == nil || current.Status != ApprovalPending || current.Command != "replacement" {
		t.Fatalf("replacement record changed: %+v", current)
	}
	for _, payload := range b.payloads() {
		if !strings.HasPrefix(payload, controlPrefix) {
			continue
		}
		frame := decodeOneControl(t, payload)
		if frame["type"] == "approval_resolved" && frame["request_id"] == requestID {
			t.Fatalf("stale resolver published terminal outcome for reused id: %v", frame)
		}
	}
}

type failSecondApprovalWorker struct {
	mu       sync.Mutex
	payloads []string
}

func (w *failSecondApprovalWorker) SendText(_ context.Context, payload string) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.payloads = append(w.payloads, payload)
	if len(w.payloads) == 2 {
		return context.DeadlineExceeded
	}
	return nil
}

func TestResolveApprovalCommandSuccessReplayFailureStaysApproved(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &failSecondApprovalWorker{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	reqID, err := h.ParkBrowserForApproval(bg(), "w", b, "command\n", PolicyDecision{Action: "hold", TimeoutS: 60})
	if err != nil {
		t.Fatal(err)
	}
	h.HoldBrowserInput(b, "replay\n")

	resolved, err := h.ResolveApproval(bg(), reqID, true, nil, &Principal{SubjectID: "approver"})
	if !resolved || err != nil {
		t.Fatalf("partial approval = resolved:%t err:%v", resolved, err)
	}
	if status := h.Approvals.Get(reqID).Status; status != ApprovalApproved {
		t.Fatalf("partial approval status = %q, want approved", status)
	}
}

func TestHoldBrowserInputTooLong(t *testing.T) {
	// MaxBufferChars is clamped to at least MaxInputChars (min 100), so pin both
	// at 100 to exercise the overflow boundary.
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.MaxInputChars = 100; c.MaxBufferChars = 100 })
	b := newBrowserWS("b1")
	if h.HoldBrowserInput(b, strings.Repeat("a", 90)) {
		t.Fatal("90 bytes should fit in a 100-byte buffer")
	}
	if !h.HoldBrowserInput(b, strings.Repeat("b", 20)) {
		t.Fatal("90+20 bytes should exceed the 100-byte buffer")
	}
	// The over-limit append is discarded, so the stored buffer is unchanged: a
	// further 10 bytes (total 100) still fits.
	if h.HoldBrowserInput(b, strings.Repeat("c", 10)) {
		t.Fatal("buffer should still hold 90 bytes; a further 10 (total 100) fits")
	}
}

func TestTryHoldBrowserInputAtomicallyDistinguishesUnparked(t *testing.T) {
	h, _ := newTestHub(t, nil)
	b := newBrowserWS("b1")
	if held, tooLong := h.TryHoldBrowserInput(b, "fresh"); held || tooLong {
		t.Fatalf("unparked hold = held:%t tooLong:%t", held, tooLong)
	}
	h.lock.Lock()
	h.pausedBrowsers[b] = true
	h.lock.Unlock()
	if held, tooLong := h.TryHoldBrowserInput(b, "buffered"); !held || tooLong {
		t.Fatalf("parked hold = held:%t tooLong:%t", held, tooLong)
	}
	if got := h.releaseParkedBrowser(&ApprovalRequest{OriginBrowser: b}, true); got != "buffered" {
		t.Fatalf("atomic hold buffer = %q", got)
	}
}

func TestPendingApprovalsSnapshot(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")

	if len(h.Approvals.PendingApprovals()) != 0 {
		t.Fatal("no pending approvals initially")
	}
	reqID, _ := h.ParkBrowserForApproval(bg(), "w", b, "x", PolicyDecision{Action: "hold", TimeoutS: 60})
	pend := h.Approvals.PendingApprovals()
	if len(pend) != 1 || pend[0].ID != reqID {
		t.Fatalf("pending snapshot = %v", pend)
	}
	_, _ = h.ResolveApproval(bg(), reqID, true, nil, nil)
	if len(h.Approvals.PendingApprovals()) != 0 {
		t.Fatal("resolved request must leave the pending set")
	}
}

func TestIsNoOpPolicyGate(t *testing.T) {
	def, _ := newTestHub(t, nil)
	if !def.IsNoOpPolicyGate() {
		t.Fatal("default hub gate should be the no-op gate")
	}
	custom, _ := newTestHub(t, func(c *TermHubConfig) { c.PolicyGate = holdGate{} })
	if custom.IsNoOpPolicyGate() {
		t.Fatal("custom gate should not report as no-op")
	}
}

func TestInterceptBrowserInputRunsGate(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.PolicyGate = holdGate{} })
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	dec, err := h.InterceptBrowserInput(bg(), "w", b, "ls")
	if err != nil {
		t.Fatalf("InterceptBrowserInput: %v", err)
	}
	if dec.Action != "hold" {
		t.Fatalf("decision = %q, want hold", dec.Action)
	}
}

// TestResolveApprovalRaceClaimsOnce drives many concurrent resolves and asserts
// exactly one succeeds (the injection happens once). Run under -race.
func TestResolveApprovalRaceClaimsOnce(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w", worker)
	b := newBrowserWS("b1")
	registerActiveBrowser(t, h, "w", b, "admin")
	reqID, _ := h.ParkBrowserForApproval(bg(), "w", b, "go", PolicyDecision{Action: "hold", TimeoutS: 60})

	var wg sync.WaitGroup
	var mu sync.Mutex
	wins := 0
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if ok, _ := h.ResolveApproval(bg(), reqID, true, nil, nil); ok {
				mu.Lock()
				wins++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if wins != 1 {
		t.Fatalf("exactly one resolve should win, got %d", wins)
	}
	if got := worker.payloads(); len(got) != 1 {
		t.Fatalf("command injected %d times, want 1", len(got))
	}
}
