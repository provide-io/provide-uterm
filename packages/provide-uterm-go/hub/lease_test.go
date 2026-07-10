//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "testing"

func restSession(hijackID, owner string, expiresAt float64) *HijackSession {
	return &HijackSession{HijackID: hijackID, Owner: owner, LeaseExpiresAt: expiresAt}
}

func TestInitClampsDashboardLease(t *testing.T) {
	mustEqual(t, makeManager(t, 0).mgr.DashboardHijackLeaseS(), 1, "clamp to floor")
	mustEqual(t, makeManager(t, 10000).mgr.DashboardHijackLeaseS(), 600, "clamp to ceiling")
	mustEqual(t, makeManager(t, 45).mgr.DashboardHijackLeaseS(), 45, "in range preserved")
}

func TestDashboardLeaseSetterReclamps(t *testing.T) {
	f := makeManager(t, 45)
	f.mgr.SetDashboardHijackLeaseS(-50)
	mustEqual(t, f.mgr.DashboardHijackLeaseS(), 1, "floor")
	f.mgr.SetDashboardHijackLeaseS(5000)
	mustEqual(t, f.mgr.DashboardHijackLeaseS(), 600, "ceiling")
}

func TestTryAcquireWsNoWorker(t *testing.T) {
	f := makeManager(t, 45)
	ok, reason := f.mgr.TryAcquireWs("missing", newBrowser("b"))
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "no_worker", "reason")
}

func TestTryAcquireWsNoWorkerWs(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.WorkerWS = nil
	f.registry.Put("w1", st)
	ok, reason := f.mgr.TryAcquireWs("w1", newBrowser("b"))
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "no_worker", "reason")
	mustTrue(t, st.HijackOwner == nil, "owner not set")
}

func TestTryAcquireWsSetsOwnerAndExpiry(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	f.registry.Put("w1", st)
	ws := newBrowser("b")
	ok, reason := f.mgr.TryAcquireWs("w1", ws)
	mustTrue(t, ok, "ok")
	mustEqual(t, reason, "", "reason none")
	mustTrue(t, st.HijackOwner == ws, "owner set")
	exp, has := f64OrNil(st.HijackOwnerExpiresAt)
	mustTrue(t, has, "expiry set")
	mustEqual(t, exp, f.now()+45, "expiry = now+ttl")
}

func TestTryAcquireWsUsesConfiguredTTL(t *testing.T) {
	f := makeManager(t, 120)
	st := makeState()
	f.registry.Put("w1", st)
	f.mgr.TryAcquireWs("w1", newBrowser("b"))
	exp, _ := f64OrNil(st.HijackOwnerExpiresAt)
	mustEqual(t, exp, f.now()+120, "ttl from config")
}

func TestTryAcquireWsAlreadyHijackedViaDashboard(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	existing := newBrowser("existing")
	st.HijackOwner = existing
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	f.registry.Put("w1", st)
	ok, reason := f.mgr.TryAcquireWs("w1", newBrowser("b"))
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "already_hijacked", "reason")
	mustTrue(t, st.HijackOwner == existing, "owner untouched")
}

func TestTryAcquireWsAlreadyHijackedViaRest(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("w1", st)
	ok, reason := f.mgr.TryAcquireWs("w1", newBrowser("b"))
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "already_hijacked", "reason")
	mustTrue(t, st.HijackOwner == nil, "owner not set")
}

func TestTryAcquireWsExpiredDashboardDoesNotBlock(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackOwner = newBrowser("old")
	st.HijackOwnerExpiresAt = f64p(f.now() - 1)
	f.registry.Put("w1", st)
	ws := newBrowser("new")
	ok, _ := f.mgr.TryAcquireWs("w1", ws)
	mustTrue(t, ok, "acquire succeeds over stale owner")
	mustTrue(t, st.HijackOwner == ws, "new owner")
}

func TestTryAcquireWsExpiredRestDoesNotBlock(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()-1)
	f.registry.Put("w1", st)
	ws := newBrowser("new")
	ok, _ := f.mgr.TryAcquireWs("w1", ws)
	mustTrue(t, ok, "acquire over stale rest")
	mustTrue(t, st.HijackOwner == ws, "owner set")
}

func TestTouchOwnerNoWorker(t *testing.T) {
	f := makeManager(t, 45)
	mustTrue(t, f.mgr.TouchOwner("ghost", nil) == nil, "nil for missing")
}

func TestTouchOwnerNoOwner(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	f.registry.Put("w1", st)
	mustTrue(t, f.mgr.TouchOwner("w1", nil) == nil, "nil when no owner")
	mustTrue(t, st.HijackOwnerExpiresAt == nil, "expiry untouched")
}

func TestTouchOwnerExtendsDefaultTTL(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackOwner = newBrowser("o")
	st.HijackOwnerExpiresAt = f64p(f.now())
	f.registry.Put("w1", st)
	exp := f.mgr.TouchOwner("w1", nil)
	mustTrue(t, exp != nil, "expiry returned")
	mustEqual(t, *exp, f.now()+45, "default ttl")
	mustEqual(t, *st.HijackOwnerExpiresAt, *exp, "written to state")
}

func TestTouchOwnerExplicitLeaseClamped(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackOwner = newBrowser("o")
	st.HijackOwnerExpiresAt = f64p(f.now())
	f.registry.Put("w1", st)
	mustEqual(t, *f.mgr.TouchOwner("w1", intp(120)), f.now()+120, "explicit lease")
	mustEqual(t, *f.mgr.TouchOwner("w1", intp(10000)), f.now()+600, "clamped to ceiling")
	mustEqual(t, *f.mgr.TouchOwner("w1", intp(0)), f.now()+1, "clamped to floor")
}

func TestTouchIfOwner(t *testing.T) {
	f := makeManager(t, 45)
	mustTrue(t, f.mgr.TouchIfOwner("ghost", newBrowser("x")) == nil, "missing worker")

	st := makeState()
	f.registry.Put("w1", st)
	mustTrue(t, f.mgr.TouchIfOwner("w1", newBrowser("x")) == nil, "no active dashboard")
	mustTrue(t, st.HijackOwnerExpiresAt == nil, "untouched")

	owner := newBrowser("o")
	orig := f.now() + 30
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(orig)
	mustTrue(t, f.mgr.TouchIfOwner("w1", newBrowser("other")) == nil, "not owner")
	mustEqual(t, *st.HijackOwnerExpiresAt, orig, "owner expiry untouched")

	exp := f.mgr.TouchIfOwner("w1", owner)
	mustTrue(t, exp != nil, "extended")
	mustEqual(t, *exp, f.now()+45, "new expiry")
}

func TestTouchIfOwnerExpiredLeaseReturnsNil(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	ws := newBrowser("o")
	st.HijackOwner = ws
	st.HijackOwnerExpiresAt = f64p(f.now() - 1)
	f.registry.Put("w1", st)
	mustTrue(t, f.mgr.TouchIfOwner("w1", ws) == nil, "expired lease -> nil")
}

func TestTryReleaseWs(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	ws := newBrowser("o")
	st.HijackOwner = ws
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	f.registry.Put("w1", st)
	released, restActive := f.mgr.TryReleaseWs("w1", ws)
	mustTrue(t, released, "released")
	mustFalse(t, restActive, "no rest")
	mustTrue(t, st.HijackOwner == nil, "owner cleared")
	mustTrue(t, st.HijackOwnerExpiresAt == nil, "expiry cleared")
}

func TestTryReleaseWsRestActiveTrue(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	ws := newBrowser("o")
	st.HijackOwner = ws
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	st.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("w1", st)
	released, restActive := f.mgr.TryReleaseWs("w1", ws)
	mustTrue(t, released && restActive, "released with rest active")
	mustTrue(t, st.HijackSession != nil, "rest untouched")
}

func TestTryReleaseWsRestExpiredFalse(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	ws := newBrowser("o")
	st.HijackOwner = ws
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	st.HijackSession = restSession("h", "o", f.now()-1)
	f.registry.Put("w1", st)
	released, restActive := f.mgr.TryReleaseWs("w1", ws)
	mustTrue(t, released, "released")
	mustFalse(t, restActive, "stale rest reported false")
}

func TestTryReleaseWsNonOwner(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	owner := newBrowser("o")
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	f.registry.Put("w1", st)
	released, restActive := f.mgr.TryReleaseWs("w1", newBrowser("other"))
	mustFalse(t, released, "not released")
	mustFalse(t, restActive, "no rest")
	mustTrue(t, st.HijackOwner == owner, "owner untouched")
}

func TestTryReleaseWsNonOwnerRestActive(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	owner := newBrowser("o")
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	st.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("w1", st)
	released, restActive := f.mgr.TryReleaseWs("w1", newBrowser("other"))
	mustFalse(t, released, "not released")
	mustTrue(t, restActive, "rest active reported")
}

func TestTryReleaseWsMissingWorker(t *testing.T) {
	f := makeManager(t, 45)
	released, restActive := f.mgr.TryReleaseWs("ghost", newBrowser("x"))
	mustFalse(t, released, "not released")
	mustFalse(t, restActive, "no rest")
}

func TestTryReleaseWsInactiveDashboardWithRest(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("w1", st)
	released, restActive := f.mgr.TryReleaseWs("w1", newBrowser("x"))
	mustFalse(t, released, "not released")
	mustTrue(t, restActive, "rest active")
}

func TestStillHijacked(t *testing.T) {
	f := makeManager(t, 45)
	mustFalse(t, f.mgr.StillHijacked("ghost"), "missing worker")
	f.registry.Put("idle", makeState())
	mustFalse(t, f.mgr.StillHijacked("idle"), "idle worker")

	dash := makeState()
	dash.HijackOwner = newBrowser("o")
	dash.HijackOwnerExpiresAt = f64p(f.now() + 30)
	f.registry.Put("dash", dash)
	mustTrue(t, f.mgr.StillHijacked("dash"), "active dashboard")

	rest := makeState()
	rest.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("rest", rest)
	mustTrue(t, f.mgr.StillHijacked("rest"), "active rest")

	both := makeState()
	both.HijackOwner = newBrowser("o")
	both.HijackOwnerExpiresAt = f64p(f.now() - 1)
	both.HijackSession = restSession("h", "o", f.now()-1)
	f.registry.Put("both", both)
	mustFalse(t, f.mgr.StillHijacked("both"), "both expired")
}

func TestIsInputOpenMode(t *testing.T) {
	f := makeManager(t, 45)
	mustFalse(t, f.mgr.IsInputOpenMode("ghost"), "missing worker")
	open := makeState()
	open.InputMode = InputModeOpen
	f.registry.Put("open", open)
	mustTrue(t, f.mgr.IsInputOpenMode("open"), "open mode")
	f.registry.Put("hijack", makeState())
	mustFalse(t, f.mgr.IsInputOpenMode("hijack"), "hijack mode")
}

func TestPrepareBrowserInput(t *testing.T) {
	f := makeManager(t, 45)
	mustFalse(t, f.mgr.PrepareBrowserInput("ghost", newBrowser("x")), "missing worker")

	st := makeState()
	ws := newBrowser("o")
	st.HijackOwner = ws
	st.HijackOwnerExpiresAt = f64p(f.now() + 1)
	f.registry.Put("w1", st)
	mustTrue(t, f.mgr.PrepareBrowserInput("w1", ws), "owner allowed")
	mustEqual(t, *st.HijackOwnerExpiresAt, f.now()+45, "lease extended")
}

func TestPrepareBrowserInputOpenModeNonOwnerNoExtend(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	owner := newBrowser("o")
	orig := f.now() + 5
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(orig)
	st.InputMode = InputModeOpen
	f.registry.Put("w1", st)
	mustTrue(t, f.mgr.PrepareBrowserInput("w1", newBrowser("other")), "open allows non-owner")
	mustEqual(t, *st.HijackOwnerExpiresAt, orig, "owner lease not extended")
}

func TestPrepareBrowserInputHijackNonOwnerDenied(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	owner := newBrowser("o")
	orig := f.now() + 5
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(orig)
	f.registry.Put("w1", st)
	mustFalse(t, f.mgr.PrepareBrowserInput("w1", newBrowser("other")), "hijack non-owner denied")
	mustEqual(t, *st.HijackOwnerExpiresAt, orig, "no extension")
}

func TestPrepareBrowserInputOwnerExpiredAllowedNotExtended(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	ws := newBrowser("o")
	past := f.now() - 1
	st.HijackOwner = ws
	st.HijackOwnerExpiresAt = f64p(past)
	f.registry.Put("w1", st)
	mustTrue(t, f.mgr.PrepareBrowserInput("w1", ws), "identity match allows")
	mustEqual(t, *st.HijackOwnerExpiresAt, past, "inactive lease not extended")
}

func TestCheckValid(t *testing.T) {
	f := makeManager(t, 45)
	future := makeState()
	future.HijackSession = restSession("h", "o", f.now()+100)
	f.registry.Put("future", future)
	mustTrue(t, f.mgr.CheckValid("future", "h"), "future valid")

	expired := makeState()
	expired.HijackSession = restSession("h", "o", f.now()-1)
	f.registry.Put("expired", expired)
	mustFalse(t, f.mgr.CheckValid("expired", "h"), "expired invalid")

	boundary := makeState()
	boundary.HijackSession = restSession("h", "o", f.now())
	f.registry.Put("boundary", boundary)
	mustFalse(t, f.mgr.CheckValid("boundary", "h"), "== now invalid (strict >)")

	mustFalse(t, f.mgr.CheckValid("ghost", "h"), "missing worker")
	f.registry.Put("nosess", makeState())
	mustFalse(t, f.mgr.CheckValid("nosess", "h"), "no session")
	mustFalse(t, f.mgr.CheckValid("future", "wrong"), "id mismatch")
}

func TestGetFreshExpiry(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", 12345.0)
	f.registry.Put("w1", st)
	mustEqual(t, f.mgr.GetFreshExpiry("w1", "h", 42.0), 12345.0, "live expiry")
	mustEqual(t, f.mgr.GetFreshExpiry("ghost", "h", 42.0), 42.0, "missing -> fallback")
	mustEqual(t, f.mgr.GetFreshExpiry("w1", "wrong", 7.0), 7.0, "id mismatch -> fallback")

	f.registry.Put("nosess", makeState())
	mustEqual(t, f.mgr.GetFreshExpiry("nosess", "h", 99.0), 99.0, "no session -> fallback")

	past := makeState()
	past.HijackSession = restSession("h", "o", -5.0)
	f.registry.Put("past", past)
	mustEqual(t, f.mgr.GetFreshExpiry("past", "h", 500.0), -5.0, "live even if past")
}

func TestComputeLeaseExpirations(t *testing.T) {
	now := 1000.0
	idle := NewWorkerTermState()
	b, r := ComputeLeaseExpirations(idle, now)
	mustFalse(t, b || r, "both idle")

	restPast := NewWorkerTermState()
	restPast.HijackSession = restSession("h", "o", 999.0)
	b, r = ComputeLeaseExpirations(restPast, now)
	mustFalse(t, b, "browser")
	mustTrue(t, r, "rest expired")

	restFuture := NewWorkerTermState()
	restFuture.HijackSession = restSession("h", "o", 1001.0)
	b, r = ComputeLeaseExpirations(restFuture, now)
	mustFalse(t, b || r, "rest future")

	restEq := NewWorkerTermState()
	restEq.HijackSession = restSession("h", "o", 1000.0)
	_, r = ComputeLeaseExpirations(restEq, now)
	mustTrue(t, r, "rest == now expired (<=)")

	dashPast := NewWorkerTermState()
	dashPast.HijackOwner = newBrowser("o")
	dashPast.HijackOwnerExpiresAt = f64p(999.0)
	b, r = ComputeLeaseExpirations(dashPast, now)
	mustTrue(t, b, "browser expired")
	mustFalse(t, r, "rest")

	dashEq := NewWorkerTermState()
	dashEq.HijackOwner = newBrowser("o")
	dashEq.HijackOwnerExpiresAt = f64p(1000.0)
	b, _ = ComputeLeaseExpirations(dashEq, now)
	mustTrue(t, b, "browser == now expired")

	dashNoExp := NewWorkerTermState()
	dashNoExp.HijackOwner = newBrowser("o")
	b, r = ComputeLeaseExpirations(dashNoExp, now)
	mustFalse(t, b || r, "owner without expiry not expired")

	both := NewWorkerTermState()
	both.HijackOwner = newBrowser("o")
	both.HijackOwnerExpiresAt = f64p(998.0)
	both.HijackSession = restSession("h", "o", 999.0)
	b, r = ComputeLeaseExpirations(both, now)
	mustTrue(t, b && r, "both expired")
}

func intp(v int) *int { return &v }
