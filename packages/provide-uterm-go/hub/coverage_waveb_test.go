//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"testing"
)

// TestFacadeThinDelegators exercises the one-line facade delegators so the
// public server-facing API surface is all covered.
func TestFacadeThinDelegators(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	registerWorkerState(h, "w1", worker)

	// Presence / router facades.
	if _, err := h.AppendEventData(bg(), "w1", "term", map[string]any{"data": "x"}); err != nil {
		t.Fatal(err)
	}
	mustEqual(t, h.RequestSnapshot(bg(), "w1"), nil, "RequestSnapshot")
	mustEqual(t, h.RequestAnalysis(bg(), "w1"), nil, "RequestAnalysis")
	st := h.registry.Get("w1")
	st.Browsers[ws] = "operator"
	st.HijackOwner = ws
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp
	mustTrue(t, h.CanSendInputTo(st, ws), "CanSendInputTo")
	snap := h.RegisterBrowserStateSnapshot("w1", ws)
	mustEqual(t, snap["worker_online"], true, "RegisterBrowserStateSnapshot facade")

	// Worker lifecycle facades.
	mustTrue(t, h.IsActiveWorker(bg(), "w1", worker), "IsActiveWorker facade")
	h.SetWorkerTunnelFlag(bg(), "w1", false)
	if _, err := h.SetWorkerHello(bg(), "w1", InputModeHijack, nil); err != nil {
		t.Fatal(err)
	}
	h.UpdateLastSnapshot(bg(), "w1", map[string]any{"screen": "s"})
	h.ActivateBrowserBroadcasts(bg(), "w1", ws)

	// Lease facades.
	if got := h.TouchHijackOwner("w1", nil); got == nil {
		t.Fatal("TouchHijackOwner should extend")
	}
	if got := h.TouchIfOwner("w1", ws); got == nil {
		t.Fatal("TouchIfOwner should extend")
	}
	mustFalse(t, h.IsInputOpenMode("w1"), "IsInputOpenMode")
	mustTrue(t, h.PrepareBrowserInput("w1", ws), "PrepareBrowserInput")
}

func TestFacadeRestLeaseAccessors(t *testing.T) {
	h, clk := newTestHub(t, nil)
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	ok, _, _ := h.TryAcquireRestHijack(bg(), "w1", "op", 60, "hj-1", clk.Monotonic())
	mustTrue(t, ok, "acquire")

	if h.ExtendHijackLease("w1", "hj-1", "op", 120, clk.Monotonic()) == nil {
		t.Fatal("ExtendHijackLease")
	}
	if h.GetFreshHijackExpiry("w1", "hj-1", 0) == 0 {
		t.Fatal("GetFreshHijackExpiry")
	}
	sess, _ := h.GetRestSession(bg(), "w1", "hj-1")
	data := h.GetHijackEventsData("w1", "hj-1", sess, 0, 10)
	if _, ok := data["rows"]; !ok {
		t.Fatal("GetHijackEventsData rows")
	}
	released, resume := h.ReleaseRestHijack("w1", "hj-1")
	mustTrue(t, released, "ReleaseRestHijack")
	mustTrue(t, resume, "should resume after rest release")
}

func TestFacadeWaitForGuardImmediate(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	matched, _, reason, err := h.WaitForGuard(bg(), "w1", "", "", 1000, 20)
	mustEqual(t, err, nil, "no err")
	mustTrue(t, matched, "empty guards match immediately")
	mustEqual(t, reason, "", "no reason")
}

func TestLeaseHubInternalHelpers(t *testing.T) {
	h, _ := newTestHub(t, nil)
	// get creates on demand.
	st := h.get("w1")
	if st == nil {
		t.Fatal("get should create state")
	}
	// resolveRoleForBrowser + CanSendInput (LeaseHub method) coverage.
	role, err := h.resolveRoleForBrowser(bg(), newBrowserWS("x"), "w1")
	mustEqual(t, err, nil, "resolve err")
	mustEqual(t, role, "viewer", "default role")
	mustFalse(t, h.CanSendInput(st, newBrowserWS("y")), "CanSendInput leasehub")
	// emitTelemetry nil-metadata branch (no sink -> returns early, but with a
	// sink and nil metadata the {} branch is taken).
	sink := &recordingSink{}
	h.telemetrySink = sink
	h.emitTelemetry(bg(), "custom", "w1", nil, nil, nil)
	mustEqual(t, len(sink.events), 1, "telemetry with nil metadata")
	if sink.events[0].Metadata == nil {
		t.Fatal("nil metadata should be replaced with empty map")
	}
}

// denyGate denies every connection with the given reason.
type denyGate struct{ reason *string }

func (g denyGate) AuditConnection(context.Context, ConnectionHeuristics, PolicyContext, BehavioralThresholds) (PolicyDecision, error) {
	return PolicyDecision{Action: "deny", Reason: g.reason}, nil
}

func TestAuditAllBrowsersDenyCloses(t *testing.T) {
	reason := "too fast"
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.BehavioralAuditGate = denyGate{reason: &reason} })
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "operator"

	mustEqual(t, h.Router.AuditAllBrowsers(bg()), nil, "audit err")
	mustTrue(t, ws.closed, "denied browser closed")
	mustEqual(t, ws.closeCode, 1008, "policy-violation close code")
	mustEqual(t, ws.closeMsg, "too fast", "close reason")
}

func TestAuditAllBrowsersAllowNoClose(t *testing.T) {
	h, _ := newTestHub(t, nil) // NoOp gate allows
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "viewer"
	mustEqual(t, h.Router.AuditAllBrowsers(bg()), nil, "audit err")
	mustFalse(t, ws.closed, "allowed browser not closed")
}

func TestAuditAllBrowsersDenyNoReasonDefault(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.BehavioralAuditGate = denyGate{reason: nil} })
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "operator"
	_ = h.Router.AuditAllBrowsers(bg())
	mustEqual(t, ws.closeMsg, "Behavioral anomaly", "default reason")
}

func TestCleanupResumeWithoutOwnerScan(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	other := newBrowserWS("keeper")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "viewer"
	st.Browsers[other] = "viewer"
	_ = clk

	// ws is NOT the hijack owner; owned_hijack=true and worker online and not
	// hijacked triggers the scan-events branch. No lifecycle event present -> true.
	res, _ := h.Conn.CleanupBrowserDisconnect(bg(), "w1", ws, true)
	mustEqual(t, res["resume_without_owner"], true, "resume needed when no prior expiry event")

	// Now with a prior hijack_lease_expired event -> scan returns false.
	ws2 := newBrowserWS("c")
	st.Browsers[ws2] = "viewer"
	st.Events = []map[string]any{{"type": "hijack_lease_expired", "seq": 1}}
	res, _ = h.Conn.CleanupBrowserDisconnect(bg(), "w1", ws2, true)
	mustEqual(t, res["resume_without_owner"], false, "no resume when expiry already emitted")
}

func TestScanEventsForResumeBreakOnAcquired(t *testing.T) {
	st := NewWorkerTermState()
	st.Events = []map[string]any{
		{"type": "hijack_acquired", "seq": 1},
		{"type": "snapshot", "seq": 2},
		{"type": 123, "seq": 3}, // non-string type is skipped
	}
	mustTrue(t, scanEventsForResume(st), "no expiry after acquire -> resume needed")
}

func TestCleanupOwnerWithRestStillActive(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("owner")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "operator"
	st.HijackOwner = ws
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp
	// Also a live REST session so rest_still_active becomes true.
	st.HijackSession = &HijackSession{HijackID: "h", LeaseExpiresAt: clk.Monotonic() + 100}

	res, _ := h.Conn.CleanupBrowserDisconnect(bg(), "w1", ws, false)
	mustEqual(t, res["was_owner"], true, "was owner")
	mustEqual(t, res["rest_still_active"], true, "rest lease survives owner disconnect")
}

func TestBroadcastHijackStateDeadSocket(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	owner := newBrowserWS("owner")
	dead := newBrowserWS("dead")
	dead.failSend = errors.New("gone")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[owner] = "operator"
	st.Browsers[dead] = "viewer"
	st.HijackOwner = owner
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp

	mustEqual(t, h.BroadcastHijackState(bg(), "w1"), nil, "broadcast hijack state err")
	// dead browser pruned; owner received a "me" hijack_state frame.
	h.lock.Lock()
	_, deadPresent := st.Browsers[dead]
	h.lock.Unlock()
	mustFalse(t, deadPresent, "dead socket pruned during hijack-state broadcast")
	frame := decodeOneControl(t, owner.last())
	mustEqual(t, frame["owner"], "me", "owner sees me in hijack_state")
}

func TestBroadcastHijackStateUnknownWorker(t *testing.T) {
	h, _ := newTestHub(t, nil)
	mustEqual(t, h.BroadcastHijackState(bg(), "ghost"), nil, "unknown worker no-op")
}

func TestBroadcastGateError(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{err: errors.New("gate boom")}
		c.Redactor = upperRedactor
	})
	a := newBrowserWS("a")
	st := NewWorkerTermState()
	st.Browsers[a] = "viewer"
	h.registry.Put("w1", st)
	err := h.Broadcast(bg(), "w1", map[string]any{"type": "snapshot", "screen": "s"})
	if err == nil {
		t.Fatal("expected gate error to propagate")
	}
}

func TestPayloadsByRoleReuse(t *testing.T) {
	// Two viewers share one role -> payloadsByRole computes once (continue path).
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "x"}}}
		c.Redactor = upperRedactor
	})
	a := newBrowserWS("a")
	b := newBrowserWS("b")
	st := NewWorkerTermState()
	st.Browsers[a] = "viewer"
	st.Browsers[b] = "viewer"
	h.registry.Put("w1", st)
	mustEqual(t, h.Broadcast(bg(), "w1", map[string]any{"type": "snapshot", "screen": "s"}), nil, "err")
	mustEqual(t, decodeOneControl(t, a.last())["screen"], "[REDACTED]", "a redacted")
	mustEqual(t, decodeOneControl(t, b.last())["screen"], "[REDACTED]", "b redacted")
}

func TestRedactSnapshotGateError(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{err: errors.New("boom")}
	})
	_, err := h.Router.RedactSnapshotForRecipient(bg(), "w1", map[string]any{"screen": "s"}, newBrowserWS("r"))
	if err == nil {
		t.Fatal("expected gate error")
	}
}

func TestRegisterBrowserFacadeQuotaError(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.MaxConnectionsPerPrincipal = 1 })
	a := newBrowserWS("a")
	a.principal = &Principal{SubjectID: "z"}
	b := newBrowserWS("b")
	b.principal = &Principal{SubjectID: "z"}
	_, _ = h.RegisterBrowser(bg(), "w1", a, "viewer", false)
	_, err := h.RegisterBrowser(bg(), "w1", b, "viewer", false)
	if err == nil {
		t.Fatal("expected quota error through facade")
	}
}

// markErrResumeStore errors on MarkHijackOwner to cover the cleanup error path.
type markErrResumeStore struct{ *InMemoryResumeStore }

func (m markErrResumeStore) MarkHijackOwner(context.Context, string, bool) error {
	return errors.New("mark failed")
}

func TestCleanupDisconnectMarkError(t *testing.T) {
	h, clk := newTestHub(t, func(c *TermHubConfig) {
		c.ResumeStore = markErrResumeStore{NewInMemoryResumeStore(c.Clock, seqTokenGen())}
	})
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("owner")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "operator", false)
	st := h.registry.Get("w1")
	st.WorkerWS = worker
	st.HijackOwner = ws
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp
	_, err := h.Conn.CleanupBrowserDisconnect(bg(), "w1", ws, false)
	if err == nil {
		t.Fatal("expected MarkHijackOwner error to propagate")
	}
}

func TestStrAndRuneSliceHelpers(t *testing.T) {
	mustEqual(t, str(nil), "", "nil -> empty")
	mustEqual(t, str("x"), "x", "string passthrough")
	mustEqual(t, str(42), "42", "non-string coerced")
	mustEqual(t, runeSlice("abc", 5), "abc", "n>=len returns whole string")
	mustEqual(t, runeSlice("abc", 2), "ab", "n<len truncates by rune")
}

func TestConfigWorkerFrameOnInvalidOverride(t *testing.T) {
	h := NewTermHub(TermHubConfig{Logger: discardLogger(), WorkerFrameOnInvalid: "reject", BrowserControlRateLimitPerSec: 0.01})
	mustEqual(t, h.WorkerFrameOnInvalid(), "reject", "override honored")
	// browserControlRateLimitPerSec below floor is clamped (exercises clampMinF).
	mustEqual(t, h.browserControlRateLimitPerSec, 0.1, "control rate floored")
}

func TestConfigDelegateRolesFalse(t *testing.T) {
	dr := false
	h := NewTermHub(TermHubConfig{Logger: discardLogger(), DelegateRoles: &dr})
	mustFalse(t, h.delegateRoles, "delegate roles false honored")
}

func TestDeliverWorkerTunnelNoCodecRejects(t *testing.T) {
	// A worker flagged tunnel but whose socket lacks TunnelSender rejects input
	// without treating the worker as dead.
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{} // not a TunnelSender
	st := registerWorkerState(h, "w1", worker)
	st.IsTunnelWorker = true
	ok, err := h.SendWorker(bg(), "w1", map[string]any{"type": "input", "data": "x"})
	mustEqual(t, err, nil, "no err")
	mustFalse(t, ok, "unsupported tunnel send returns false")
	mustEqual(t, len(worker.payloads()), 0, "nothing sent without codec")
	mustTrue(t, st.WorkerWS == worker, "unsupported operation preserves worker")
}
