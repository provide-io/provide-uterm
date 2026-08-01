//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"errors"
	"testing"
)

func TestBroadcastTermFrameToBrowsers(t *testing.T) {
	h, _ := newTestHub(t, nil)
	a := newBrowserWS("a")
	b := newBrowserWS("b")
	st := NewWorkerTermState()
	st.Browsers[a] = "viewer"
	st.Browsers[b] = "operator"
	h.registry.Put("w1", st)

	err := h.Broadcast(bg(), "w1", map[string]any{"type": "term", "data": "hi\x10there"})
	mustEqual(t, err, nil, "broadcast err")
	mustEqual(t, decodeTerminalData(t, a.last()), "hi\x10there", "a got escaped term data")
	mustEqual(t, decodeTerminalData(t, b.last()), "hi\x10there", "b got escaped term data")
}

func TestBroadcastControlFrame(t *testing.T) {
	h, _ := newTestHub(t, nil)
	a := newBrowserWS("a")
	st := NewWorkerTermState()
	st.Browsers[a] = "viewer"
	h.registry.Put("w1", st)

	err := h.Broadcast(bg(), "w1", map[string]any{"type": "custom", "k": "v"})
	mustEqual(t, err, nil, "broadcast err")
	frame := decodeOneControl(t, a.last())
	mustEqual(t, frame["type"], "custom", "control type")
	mustEqual(t, frame["k"], "v", "control field")
}

func TestBroadcastSkipsStartupPending(t *testing.T) {
	h, _ := newTestHub(t, nil)
	a := newBrowserWS("a")
	st := NewWorkerTermState()
	st.Browsers[a] = "viewer"
	h.startupPendingBrowsers[a] = true
	h.registry.Put("w1", st)
	mustEqual(t, h.Broadcast(bg(), "w1", map[string]any{"type": "term", "data": "x"}), nil, "err")
	mustEqual(t, a.count(), 0, "startup-pending browser not sent to")
}

func TestBroadcastUnknownWorkerNoOp(t *testing.T) {
	h, _ := newTestHub(t, nil)
	mustEqual(t, h.Broadcast(bg(), "ghost", map[string]any{"type": "term", "data": "x"}), nil, "no-op")
}

func TestBroadcastPrunesDeadBrowser(t *testing.T) {
	h, _ := newTestHub(t, nil)
	good := newBrowserWS("good")
	bad := newBrowserWS("bad")
	bad.failSend = errors.New("boom")
	st := NewWorkerTermState()
	st.Browsers[good] = "viewer"
	st.Browsers[bad] = "operator"
	h.registry.Put("w1", st)

	mustEqual(t, h.Broadcast(bg(), "w1", map[string]any{"type": "term", "data": "x"}), nil, "err")
	h.lock.Lock()
	_, badPresent := st.Browsers[bad]
	_, goodPresent := st.Browsers[good]
	h.lock.Unlock()
	mustFalse(t, badPresent, "dead browser pruned")
	mustTrue(t, goodPresent, "good browser retained")
}

func TestBroadcastDeadOwnerRebroadcastsHijackState(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	owner := newBrowserWS("owner")
	owner.failSend = errors.New("dead")
	survivor := newBrowserWS("survivor")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[owner] = "operator"
	st.Browsers[survivor] = "viewer"
	st.HijackOwner = owner
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	mustEqual(t, h.Broadcast(bg(), "w1", map[string]any{"type": "term", "data": "x"}), nil, "err")
	// Owner removed and hijack cleared; survivor received a hijack_state frame.
	h.lock.Lock()
	ownerCleared := st.HijackOwner == nil
	h.lock.Unlock()
	mustTrue(t, ownerCleared, "hijack owner cleared after dead-owner prune")
	// survivor's last frame is hijack_state (not hijacked anymore).
	frame := decodeOneControl(t, survivor.last())
	mustEqual(t, frame["type"], "hijack_state", "survivor got hijack_state")
	mustEqual(t, frame["hijacked"], false, "no longer hijacked")
}

func TestSendWorkerInputAsTerminalData(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w1", worker)
	ok, err := h.SendWorker(bg(), "w1", map[string]any{"type": "input", "data": "ls\r"})
	mustTrue(t, ok, "sent")
	mustEqual(t, err, nil, "no err")
	mustEqual(t, decodeTerminalData(t, worker.last()), "ls\r", "input as terminal data")
}

func TestSendWorkerControlFrame(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w1", worker)
	ok, _ := h.SendWorker(bg(), "w1", map[string]any{"type": "snapshot_req", "req_id": "r1"})
	mustTrue(t, ok, "sent")
	frame := decodeOneControl(t, worker.last())
	mustEqual(t, frame["type"], "snapshot_req", "control type")
	mustEqual(t, frame["req_id"], "r1", "req_id")
}

func TestSendWorkerNoWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ok, err := h.SendWorker(bg(), "ghost", map[string]any{"type": "input", "data": "x"})
	mustFalse(t, ok, "no worker -> false")
	mustEqual(t, err, nil, "no err")
}

func TestSendWorkerDeadSocketNulls(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{failSend: errors.New("dead")}
	st := registerWorkerState(h, "w1", worker)
	ok, err := h.SendWorker(bg(), "w1", map[string]any{"type": "input", "data": "x"})
	mustFalse(t, ok, "dead -> false")
	mustEqual(t, err, nil, "swallowed non-cancel error")
	h.lock.Lock()
	nulled := st.WorkerWS == nil
	h.lock.Unlock()
	mustTrue(t, nulled, "worker_ws nulled on dead send")
}

func TestSendWorkerTunnelRouting(t *testing.T) {
	h, _ := newTestHub(t, nil)
	tun := &fakeTunnelWS{}
	st := registerWorkerState(h, "w1", tun)
	st.IsTunnelWorker = true

	// input -> raw PTY bytes
	ok, _ := h.SendWorker(bg(), "w1", map[string]any{"type": "input", "data": "abc"})
	mustTrue(t, ok, "tunnel input ok")
	mustEqual(t, len(tun.inputs), 1, "one input")
	mustEqual(t, tun.inputs[0], "abc", "raw input bytes")

	// http control -> HTTP side channel
	ok, _ = h.SendWorker(bg(), "w1", map[string]any{"type": "http_action", "action": "resend"})
	mustTrue(t, ok, "http control ok")
	mustEqual(t, len(tun.httpControl), 1, "one http control")

	// other type -> explicitly unsupported
	ok, _ = h.SendWorker(bg(), "w1", map[string]any{"type": "snapshot_req"})
	mustFalse(t, ok, "unsupported type returns false")
	mustEqual(t, len(tun.inputs), 1, "no new input")
	mustEqual(t, len(tun.httpControl), 1, "no new http control")

	// input with non-string data -> explicitly unsupported
	ok, _ = h.SendWorker(bg(), "w1", map[string]any{"type": "input", "data": 42})
	mustFalse(t, ok, "non-string input returns false")
	mustEqual(t, len(tun.inputs), 1, "still one input")
}

func TestTunnelUnsupportedControlReturnsExplicitFailure(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeTunnelWS{}
	err := h.Router.deliverWorker(bg(), worker, true,
		map[string]any{"type": "control", "action": "step"})
	if !errors.Is(err, errOwnedInputUnsupported) {
		t.Fatalf("unsupported tunnel control error = %v", err)
	}
}

func TestSendWorkerRecordsKeystroke(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w1", worker)
	src := newBrowserWS("src")
	_, _ = h.Router.SendWorker(bg(), "w1", map[string]any{"type": "input", "data": "a"}, src)
	clk.SetMonotonic(1001)
	_, _ = h.Router.SendWorker(bg(), "w1", map[string]any{"type": "input", "data": "b"}, src)
	hd := h.Router.GetHeuristics(src)
	if hd["cps"] <= 0 {
		t.Fatalf("expected cps > 0 after two keystrokes, got %v", hd["cps"])
	}
}

func TestAppendEventRingAndSeq(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.EventDequeMaxlen = 2 })
	h.registry.Put("w1", NewWorkerTermState())
	e1, _ := h.Router.AppendEvent(bg(), "w1", "hijack_acquired", nil)
	mustEqual(t, e1["seq"], 1, "first seq")
	_, _ = h.Router.AppendEvent(bg(), "w1", "term", map[string]any{"data": "x"})
	e3, _ := h.Router.AppendEvent(bg(), "w1", "term", map[string]any{"data": "y"})
	mustEqual(t, e3["seq"], 3, "third seq")
	st := h.registry.Get("w1")
	mustEqual(t, len(st.Events), 2, "ring capped at 2")
	mustEqual(t, st.MinEventSeq, 2, "min seq is oldest retained")
}

func TestAppendEventTermTruncation(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.MaxEventDataChars = 300 })
	h.registry.Put("w1", NewWorkerTermState())
	long := ""
	for i := 0; i < 400; i++ {
		long += "a"
	}
	evt, _ := h.Router.AppendEvent(bg(), "w1", "term", map[string]any{"data": long})
	data := evt["data"].(map[string]any)
	mustEqual(t, len(data["data"].(string)), 300, "term data truncated to cap")
}

func TestAppendEventUnknownWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	evt, _ := h.Router.AppendEvent(bg(), "ghost", "term", map[string]any{"data": "x"})
	mustEqual(t, evt["seq"], 0, "unknown worker seq 0")
	mustEqual(t, evt["type"], "term", "type retained")
}

func TestHijackStateMsgForOwnerVariants(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	owner := newBrowserWS("owner")
	other := newBrowserWS("other")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[owner] = "operator"
	st.Browsers[other] = "viewer"
	st.HijackOwner = owner
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	me := h.HijackStateMsgFor(bg(), "w1", owner)
	mustTrue(t, me.Hijacked, "hijacked")
	mustEqual(t, *me.Owner, "me", "owner sees me")
	mustEqual(t, *me.LeaseExpiresAt, 5100.0, "mono->wall lease")

	oth := h.HijackStateMsgFor(bg(), "w1", other)
	mustEqual(t, *oth.Owner, "other", "non-owner sees other")

	// unknown worker
	unk := h.HijackStateMsgFor(bg(), "ghost", owner)
	mustFalse(t, unk.Hijacked, "unknown not hijacked")
	if unk.Owner != nil {
		t.Fatal("unknown owner should be nil")
	}
}

func TestSetInputModeRejectsOpenWhileHijacked(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "operator"
	st.HijackOwner = ws
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	ok, reason, _ := h.SetInputMode(bg(), "w1", InputModeOpen)
	mustFalse(t, ok, "cannot open while hijacked")
	mustEqual(t, reason, "active_hijack", "reason")

	// unknown worker
	ok, reason, _ = h.SetInputMode(bg(), "ghost", InputModeOpen)
	mustFalse(t, ok, "unknown")
	mustEqual(t, reason, "not_found", "not_found")
}

func TestSetInputModeSuccessBroadcasts(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "operator"

	ok, reason, err := h.SetInputMode(bg(), "w1", InputModeOpen)
	mustTrue(t, ok, "set open ok")
	mustEqual(t, reason, "", "no reason")
	mustEqual(t, err, nil, "no err")
	mustEqual(t, st.InputMode, InputModeOpen, "mode applied")
	// Browser received input_mode_changed then hijack_state.
	payloads := ws.payloads()
	if len(payloads) < 2 {
		t.Fatalf("expected 2 frames, got %d", len(payloads))
	}
	first := decodeOneControl(t, payloads[0])
	mustEqual(t, first["type"], "input_mode_changed", "first frame")
	mustEqual(t, first["input_mode"], "open", "mode field")
}

func TestPruneIfIdleRemovesEmptyWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	h.registry.Put("w1", NewWorkerTermState()) // no ws, no browsers, no lease
	mustEqual(t, h.PruneIfIdle(bg(), "w1"), nil, "prune err")
	mustFalse(t, h.registry.Contains("w1"), "idle worker pruned")
}

func TestPruneIfIdleKeepsActiveWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	mustEqual(t, h.PruneIfIdle(bg(), "w1"), nil, "prune err")
	mustTrue(t, h.registry.Contains("w1"), "active worker kept")
	// Unknown worker is a no-op.
	mustEqual(t, h.PruneIfIdle(bg(), "ghost"), nil, "unknown no-op")
}

func TestBrowserCountsAndRecentEvents(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st1 := NewWorkerTermState()
	st1.Browsers[newBrowserWS("a")] = "viewer"
	st1.Browsers[newBrowserWS("b")] = "viewer"
	h.registry.Put("w1", st1)
	st2 := NewWorkerTermState()
	st2.Browsers[newBrowserWS("c")] = "viewer"
	h.registry.Put("w2", st2)

	mustEqual(t, h.BrowserCount(bg(), "w1"), 2, "w1 count")
	mustEqual(t, h.BrowserCount(bg(), "ghost"), 0, "ghost count")
	mustEqual(t, h.BrowserCountTotal(bg()), 3, "total")

	for i := 0; i < 5; i++ {
		_, _ = h.Router.AppendEvent(bg(), "w1", "term", map[string]any{"data": "x"})
	}
	recent := h.GetRecentEvents(bg(), "w1", 3)
	mustEqual(t, len(recent), 3, "limit honored")
	mustEqual(t, len(h.GetRecentEvents(bg(), "ghost", 3)), 0, "unknown empty")
}

func TestIdleCandidatesAndBrowserRole(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := NewWorkerTermState()
	st.LastActivityAt = clk.Monotonic() - 100
	h.registry.Put("w1", st)
	// Worker with a browser is never idle.
	st2 := NewWorkerTermState()
	st2.Browsers[newBrowserWS("a")] = "viewer"
	st2.LastActivityAt = clk.Monotonic() - 100
	h.registry.Put("w2", st2)

	cands := h.GetIdleCandidates(bg(), 50)
	mustEqual(t, len(cands), 1, "one idle candidate")
	mustEqual(t, cands[0].WorkerID, "w1", "w1 idle")

	ws := newBrowserWS("x")
	st.Browsers[ws] = "viewer"
	h.SetBrowserRole(bg(), "w1", ws, "admin")
	role, ok := h.GetWorkerBrowserRole(bg(), "w1", ws)
	mustTrue(t, ok, "role found")
	mustEqual(t, role, "admin", "role updated")
	_, ok = h.GetWorkerBrowserRole(bg(), "ghost", ws)
	mustFalse(t, ok, "unknown worker role")
}

func TestTryReclaimHijack(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("x")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "operator"

	mustTrue(t, h.TryReclaimHijack(bg(), "w1", ws), "reclaim succeeds when idle")
	mustEqual(t, st.HijackOwner, BrowserConn(ws), "owner set")
	// Second reclaim by another ws fails (already owned).
	mustFalse(t, h.TryReclaimHijack(bg(), "w1", newBrowserWS("y")), "cannot reclaim when owned")
	// Open mode blocks reclaim.
	st.HijackOwner = nil
	st.HijackOwnerExpiresAt = nil
	st.InputMode = InputModeOpen
	mustFalse(t, h.TryReclaimHijack(bg(), "w1", ws), "open mode blocks reclaim")
}

func TestGetLastSnapshotNoGate(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := NewWorkerTermState()
	st.LastSnapshot = map[string]any{"screen": "hello", "type": "snapshot"}
	h.registry.Put("w1", st)
	snap, err := h.GetLastSnapshot(bg(), "w1", newBrowserWS("r"))
	mustEqual(t, err, nil, "no err")
	mustEqual(t, snap["screen"], "hello", "raw snapshot returned without gate")
	// Unknown worker -> nil.
	nilSnap, _ := h.GetLastSnapshot(bg(), "ghost", nil)
	if nilSnap != nil {
		t.Fatal("unknown worker snapshot nil")
	}
}

func TestBehavioralHeuristicsInsufficientSamples(t *testing.T) {
	h, _ := newTestHub(t, nil)
	src := newBrowserWS("s")
	hd := h.Router.GetHeuristics(src)
	mustEqual(t, hd["cps"], 0.0, "no samples cps")
	mustEqual(t, hd["jitter"], 0.0, "no samples jitter")
	h.Router.RecordKeystroke(src)
	hd = h.Router.GetHeuristics(src)
	mustEqual(t, hd["cps"], 0.0, "one sample cps")
	h.Router.ForgetBrowser(src)
	// After forgetting, back to zero.
	mustEqual(t, h.Router.GetHeuristics(src)["cps"], 0.0, "forgotten")
}

func TestBehavioralHeuristicsJitter(t *testing.T) {
	h, clk := newTestHub(t, nil)
	src := newBrowserWS("s")
	for i, m := range []float64{1000, 1001, 1003, 1006} {
		clk.SetMonotonic(m)
		h.Router.RecordKeystroke(src)
		_ = i
	}
	hd := h.Router.GetHeuristics(src)
	// intervals: 1,2,3 -> sample variance = 1.0
	mustEqual(t, hd["jitter"], 1.0, "sample variance of intervals")
}
