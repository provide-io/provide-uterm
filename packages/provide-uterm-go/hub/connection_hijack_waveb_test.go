//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"errors"
	"testing"
)

func TestDisconnectWorkerClearsHijackAndBroadcasts(t *testing.T) {
	var notifyOff bool
	h, clk := newTestHub(t, func(c *TermHubConfig) {
		c.OnHijackChanged = func(_ string, enabled bool, _ *string) error {
			if !enabled {
				notifyOff = true
			}
			return nil
		}
	})
	worker := &fakeWorkerWS{}
	browser := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[browser] = "operator"
	st.HijackOwner = browser
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	ok, err := h.DisconnectWorker(bg(), "w1")
	mustEqual(t, err, nil, "no err")
	mustTrue(t, ok, "disconnected")
	mustTrue(t, worker.closed, "worker socket closed")
	mustEqual(t, st.WorkerWS, WorkerWS(nil), "worker_ws cleared")
	mustEqual(t, st.HijackOwner, BrowserConn(nil), "hijack owner cleared")
	mustTrue(t, notifyOff, "notify_hijack_changed(off) fired")

	// Browser received worker_disconnected then hijack_state (was hijacked).
	payloads := browser.payloads()
	if len(payloads) < 2 {
		t.Fatalf("expected >=2 frames, got %d", len(payloads))
	}
	first := decodeOneControl(t, payloads[0])
	mustEqual(t, first["type"], "worker_disconnected", "first frame worker_disconnected")
	mustEqual(t, first["worker_id"], "w1", "worker id in frame")
}

func TestDisconnectWorkerNoWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ok, err := h.DisconnectWorker(bg(), "ghost")
	mustEqual(t, err, nil, "no err")
	mustFalse(t, ok, "no worker to disconnect")
}

func TestDisconnectWorkerCloseErrorTolerated(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{closeErr: errors.New("already closed")}
	registerWorkerState(h, "w1", worker)
	ok, err := h.DisconnectWorker(bg(), "w1")
	mustEqual(t, err, nil, "close error tolerated")
	mustTrue(t, ok, "still reports disconnect")
}

func TestDisconnectWorkerNotHijackedSkipsHijackBroadcast(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	browser := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[browser] = "viewer"

	ok, _ := h.DisconnectWorker(bg(), "w1")
	mustTrue(t, ok, "disconnected")
	// Only one frame: worker_disconnected (no hijack_state, since not hijacked).
	mustEqual(t, browser.count(), 1, "only worker_disconnected sent")
}

func TestDisconnectWorkerClosesEventBus(t *testing.T) {
	bus := NewEventBus(EventBusOptions{Logger: discardLogger()})
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.EventBus = bus })
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	sub, remove, err := bus.Watch("w1", nil, nil)
	mustEqual(t, err, nil, "watch err")
	defer remove()

	_, _ = h.DisconnectWorker(bg(), "w1")
	// Event bus close pushes a nil sentinel.
	select {
	case ev := <-sub.Queue:
		if ev != nil {
			t.Fatalf("expected nil sentinel, got %v", ev)
		}
	default:
		t.Fatal("event bus not closed for worker")
	}
}

func TestForceReleaseHijackRest(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	st := registerWorkerState(h, "w1", worker)
	st.HijackSession = &HijackSession{HijackID: "h", Owner: "op-1", LeaseExpiresAt: clk.Monotonic() + 100}

	ok, err := h.ForceReleaseHijack(bg(), "w1")
	mustEqual(t, err, nil, "no err")
	mustTrue(t, ok, "force released")
	mustEqual(t, st.HijackSession, (*HijackSession)(nil), "session cleared")
	// Worker got a resume control frame carrying the original owner.
	frame := decodeOneControl(t, worker.last())
	mustEqual(t, frame["action"], "resume", "resume action")
	mustEqual(t, frame["owner"], "op-1", "original owner in resume")
}

func TestForceReleaseHijackDashboard(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	owner := newBrowserWS("o")
	st := registerWorkerState(h, "w1", worker)
	st.HijackOwner = owner
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	ok, _ := h.ForceReleaseHijack(bg(), "w1")
	mustTrue(t, ok, "force released dashboard")
	mustEqual(t, st.HijackOwner, BrowserConn(nil), "dashboard owner cleared")
	frame := decodeOneControl(t, worker.last())
	mustEqual(t, frame["owner"], "server-forced", "dashboard-only release owner is server-forced")
}

func TestForceReleaseHijackNoHijack(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	ok, err := h.ForceReleaseHijack(bg(), "w1")
	mustEqual(t, err, nil, "no err")
	mustFalse(t, ok, "nothing to release")

	// Unknown worker.
	ok, _ = h.ForceReleaseHijack(bg(), "ghost")
	mustFalse(t, ok, "unknown worker")
}
