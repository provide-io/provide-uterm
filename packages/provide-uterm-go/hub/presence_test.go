//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "testing"

func TestCanSendInputHijackMode(t *testing.T) {
	h, clk := newTestHub(t, nil)
	owner := newBrowserWS("owner")
	other := newBrowserWS("other")
	st := NewWorkerTermState()
	st.Browsers[owner] = "viewer"
	st.Browsers[other] = "operator"
	st.HijackOwner = owner
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	mustTrue(t, h.Presence.CanSendInput(st, owner), "dashboard owner can send")
	mustFalse(t, h.Presence.CanSendInput(st, other), "non-owner cannot send in hijack mode")
}

func TestCanSendInputOpenMode(t *testing.T) {
	h, _ := newTestHub(t, nil)
	viewer := newBrowserWS("v")
	op := newBrowserWS("o")
	admin := newBrowserWS("a")
	unknown := newBrowserWS("u")
	st := NewWorkerTermState()
	st.InputMode = InputModeOpen
	st.Browsers[viewer] = "viewer"
	st.Browsers[op] = "operator"
	st.Browsers[admin] = "admin"

	mustFalse(t, h.Presence.CanSendInput(st, viewer), "viewer excluded in open mode")
	mustTrue(t, h.Presence.CanSendInput(st, op), "operator may send in open mode")
	mustTrue(t, h.Presence.CanSendInput(st, admin), "admin may send in open mode")
	mustFalse(t, h.Presence.CanSendInput(st, unknown), "unknown ws defaults to viewer -> excluded")
}

func TestRegisterBrowserStateSnapshot(t *testing.T) {
	h, clk := newTestHub(t, nil)
	// Unknown worker returns defaults.
	got := h.Presence.RegisterBrowserStateSnapshot("nope", newBrowserWS("x"))
	mustEqual(t, got["is_hijacked"], false, "unknown is_hijacked")
	mustEqual(t, got["worker_online"], false, "unknown worker_online")
	mustEqual(t, got["input_mode"], "hijack", "unknown input_mode")

	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "operator"
	st.HijackOwner = ws
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	got = h.Presence.RegisterBrowserStateSnapshot("w1", ws)
	mustEqual(t, got["is_hijacked"], true, "hijacked")
	mustEqual(t, got["hijacked_by_me"], true, "hijacked_by_me")
	mustEqual(t, got["worker_online"], true, "worker_online")
	mustEqual(t, got["input_mode"], "hijack", "input_mode")
}

func TestRequestSnapshotAndAnalysisFrames(t *testing.T) {
	seq := 0
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.IDGen = func() string { seq++; return "id" + string(rune('0'+seq)) }
	})
	worker := &fakeWorkerWS{}
	registerWorkerState(h, "w1", worker)

	mustEqual(t, h.Presence.RequestSnapshot(bg(), "w1"), nil, "snapshot req err")
	frame := decodeOneControl(t, worker.last())
	mustEqual(t, frame["type"], "snapshot_req", "snapshot_req type")
	mustEqual(t, frame["req_id"], "id1", "req_id")
	if _, ok := frame["ts"]; !ok {
		t.Fatal("snapshot_req missing ts")
	}

	mustEqual(t, h.Presence.RequestAnalysis(bg(), "w1"), nil, "analysis req err")
	frame = decodeOneControl(t, worker.last())
	mustEqual(t, frame["type"], "analyze_req", "analyze_req type")
	mustEqual(t, frame["req_id"], "id2", "req_id 2")
}

func TestRequestSnapshotNoWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	// No worker connected: send_worker returns (false, nil) — no error.
	mustEqual(t, h.Presence.RequestSnapshot(bg(), "ghost"), nil, "no-op snapshot req")
	mustEqual(t, h.Presence.RequestAnalysis(bg(), "ghost"), nil, "no-op analysis req")
}

func TestUUID4Format(t *testing.T) {
	id := newUUID4()
	// 8-4-4-4-12 with version/variant nibbles.
	if len(id) != 36 || id[14] != '4' {
		t.Fatalf("unexpected uuid4 format: %q", id)
	}
}
