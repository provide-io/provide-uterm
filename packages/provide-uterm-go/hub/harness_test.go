//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"sync"
	"testing"
)

// fakeBrowser is a comparable browser-conn identity used as a map key / owner.
type fakeBrowser struct{ name string }

func newBrowser(name string) *fakeBrowser { return &fakeBrowser{name: name} }

// recordingWorkerWS records the payloads sent to a worker, or returns a fixed
// error / runs a custom hook.
type recordingWorkerWS struct {
	mu     sync.Mutex
	sent   []string
	err    error
	onSend func(ctx context.Context, payload string) error
}

func (w *recordingWorkerWS) SendText(ctx context.Context, payload string) error {
	if w.onSend != nil {
		return w.onSend(ctx, payload)
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.sent = append(w.sent, payload)
	return w.err
}

func (w *recordingWorkerWS) sentCount() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.sent)
}

func (w *recordingWorkerWS) lastPayload() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.sent[len(w.sent)-1]
}

type sendCall struct {
	workerID string
	msg      map[string]any
}

type notifyCall struct {
	workerID string
	enabled  bool
	owner    *string
}

type eventCall struct {
	workerID  string
	eventType string
}

// fakeLeaseHub is a recording [LeaseHub] mirroring the Python _FakeHub /
// _RecordingHub used by the lease suites.
type fakeLeaseHub struct {
	clock              Clock
	mgr                *HijackLeaseManager
	sendWorkerCalls    []sendCall
	notifyCalls        []notifyCall
	broadcastCalls     []string
	events             []eventCall
	metrics            []string
	pruneCalls         []string
	recheckCalls       []recheckCall
	seq                []string
	isHijackedOverride func(*WorkerTermState) bool
	sendWorkerErr      error
	appendEventErr     error
	broadcastErr       error
	pruneErr           error
	recheckErr         error
}

type recheckCall struct {
	workerID string
	now      float64
}

func (h *fakeLeaseHub) IsDashboardHijackActive(st *WorkerTermState) bool {
	if st.HijackOwner == nil {
		return false
	}
	if st.HijackOwnerExpiresAt == nil {
		return true
	}
	return *st.HijackOwnerExpiresAt > h.clock.Monotonic()
}

func (h *fakeLeaseHub) HasValidRESTLease(st *WorkerTermState) bool {
	return st.HijackSession != nil && st.HijackSession.LeaseExpiresAt > h.clock.Monotonic()
}

func (h *fakeLeaseHub) IsHijacked(st *WorkerTermState) bool {
	if h.isHijackedOverride != nil {
		return h.isHijackedOverride(st)
	}
	return h.IsDashboardHijackActive(st) || h.HasValidRESTLease(st)
}

func (h *fakeLeaseHub) CanSendInput(st *WorkerTermState, ws BrowserConn) bool {
	return st.HijackOwner == ws || st.InputMode == InputModeOpen
}

func (h *fakeLeaseHub) Metric(name string, _ int) {
	h.metrics = append(h.metrics, name)
	h.seq = append(h.seq, "metric")
}

func (h *fakeLeaseHub) NotifyHijackChanged(workerID string, enabled bool, owner *string) {
	h.notifyCalls = append(h.notifyCalls, notifyCall{workerID, enabled, owner})
	h.seq = append(h.seq, "notify")
}

func (h *fakeLeaseHub) SendWorker(_ context.Context, workerID string, msg map[string]any) (bool, error) {
	h.sendWorkerCalls = append(h.sendWorkerCalls, sendCall{workerID, msg})
	h.seq = append(h.seq, "send_worker")
	if h.sendWorkerErr != nil {
		return false, h.sendWorkerErr
	}
	return true, nil
}

func (h *fakeLeaseHub) BroadcastHijackState(_ context.Context, workerID string) error {
	h.broadcastCalls = append(h.broadcastCalls, workerID)
	h.seq = append(h.seq, "broadcast")
	return h.broadcastErr
}

func (h *fakeLeaseHub) AppendEvent(_ context.Context, workerID, eventType string) error {
	h.events = append(h.events, eventCall{workerID, eventType})
	h.seq = append(h.seq, "append_event")
	return h.appendEventErr
}

func (h *fakeLeaseHub) PruneIfIdle(_ context.Context, workerID string) error {
	h.pruneCalls = append(h.pruneCalls, workerID)
	h.seq = append(h.seq, "prune")
	return h.pruneErr
}

func (h *fakeLeaseHub) RecheckAndResume(ctx context.Context, workerID string, now float64) error {
	h.recheckCalls = append(h.recheckCalls, recheckCall{workerID, now})
	h.seq = append(h.seq, "recheck")
	if h.recheckErr != nil {
		return h.recheckErr
	}
	if h.mgr != nil {
		return h.mgr.RecheckAndResume(ctx, workerID, now)
	}
	return nil
}

// leaseFixture bundles a manager with its registry, hub, lock, and clock.
type leaseFixture struct {
	mgr      *HijackLeaseManager
	registry *WorkerRegistry
	hub      *fakeLeaseHub
	lock     *sync.Mutex
	clock    *ManualClock
}

// makeManager builds a lease fixture with a manual clock pinned at mono=1000,
// wall=5000 (so tests set expiries relative to clock.Monotonic()).
func makeManager(t *testing.T, dashboardLeaseS int) leaseFixture {
	t.Helper()
	registry := NewWorkerRegistry()
	lock := &sync.Mutex{}
	clk := NewManualClock(5000)
	clk.SetMonotonic(1000)
	hub := &fakeLeaseHub{clock: clk}
	mgr := NewHijackLeaseManager(registry, lock, dashboardLeaseS, hub, clk, discardLogger())
	hub.mgr = mgr
	return leaseFixture{mgr: mgr, registry: registry, hub: hub, lock: lock, clock: clk}
}

// makeState creates a registered worker state with a live worker_ws.
func makeState() *WorkerTermState {
	st := NewWorkerTermState()
	st.WorkerWS = &recordingWorkerWS{}
	return st
}

func (f leaseFixture) now() float64 { return f.clock.Monotonic() }
