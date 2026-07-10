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

func TestExpireLeasesUnderLock(t *testing.T) {
	f := makeManager(t, 45)
	_, _, _, ok := f.mgr.expireLeasesUnderLock("ghost", f.now())
	mustFalse(t, ok, "missing worker -> not ok")

	f.registry.Put("idle", makeState())
	_, _, _, ok = f.mgr.expireLeasesUnderLock("idle", f.now())
	mustFalse(t, ok, "idle -> not ok")

	rest := makeState()
	rest.HijackSession = restSession("h", "o", f.now()-1)
	f.registry.Put("rest", rest)
	r, d, resume, ok := f.mgr.expireLeasesUnderLock("rest", f.now())
	mustTrue(t, ok && r && !d && resume, "rest expired -> resume")
	mustTrue(t, rest.HijackSession == nil, "session cleared")

	dash := makeState()
	dash.HijackOwner = newBrowser("o")
	dash.HijackOwnerExpiresAt = f64p(f.now() - 1)
	f.registry.Put("dash", dash)
	r, d, resume, ok = f.mgr.expireLeasesUnderLock("dash", f.now())
	mustTrue(t, ok && !r && d && resume, "dash expired -> resume")
	mustTrue(t, dash.HijackOwner == nil && dash.HijackOwnerExpiresAt == nil, "owner cleared")

	partial := makeState()
	partial.HijackSession = restSession("h", "o", f.now()-1)
	owner := newBrowser("o")
	partial.HijackOwner = owner
	partial.HijackOwnerExpiresAt = f64p(f.now() + 30)
	f.registry.Put("partial", partial)
	r, d, resume, ok = f.mgr.expireLeasesUnderLock("partial", f.now())
	mustTrue(t, ok && r && !d && !resume, "partial -> no resume")
	mustTrue(t, partial.HijackSession == nil && partial.HijackOwner == owner, "rest cleared, dash kept")

	live := makeState()
	live.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("live", live)
	r, d, resume, ok = f.mgr.expireLeasesUnderLock("live", f.now())
	mustTrue(t, ok && !r && !d && !resume, "live -> nothing expired")
	mustTrue(t, live.HijackSession != nil, "session untouched")

	both := makeState()
	both.HijackSession = restSession("h", "o", f.now()-1)
	both.HijackOwner = newBrowser("o")
	both.HijackOwnerExpiresAt = f64p(f.now() - 1)
	f.registry.Put("both", both)
	r, d, resume, ok = f.mgr.expireLeasesUnderLock("both", f.now())
	mustTrue(t, ok && r && d && resume, "both expired -> resume")
}

func TestRecheckAndResumeIdle(t *testing.T) {
	f := makeManager(t, 45)
	f.registry.Put("w1", makeState())
	err := f.mgr.RecheckAndResume(context.Background(), "w1", 1234.5)
	mustTrue(t, err == nil, "no error")
	mustEqual(t, len(f.hub.sendWorkerCalls), 1, "one resume frame")
	call := f.hub.sendWorkerCalls[0]
	mustEqual(t, call.workerID, "w1", "worker id")
	mustDeepEqual(t, call.msg, map[string]any{
		"type": "control", "action": "resume", "owner": "lease-expired", "lease_s": 0, "ts": 1234.5,
	}, "resume frame")
	mustEqual(t, len(f.hub.notifyCalls), 1, "one notify")
	mustDeepEqual(t, f.hub.notifyCalls[0], notifyCall{"w1", false, nil}, "notify args")
}

func TestRecheckAndResumeSuppressedWhenHijacked(t *testing.T) {
	f := makeManager(t, 45)
	dash := makeState()
	dash.HijackOwner = newBrowser("o")
	dash.HijackOwnerExpiresAt = f64p(f.now() + 30)
	f.registry.Put("dash", dash)
	_ = f.mgr.RecheckAndResume(context.Background(), "dash", f.now())
	mustEqual(t, len(f.hub.sendWorkerCalls), 0, "no send when dashboard active")

	rest := makeState()
	rest.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("rest", rest)
	_ = f.mgr.RecheckAndResume(context.Background(), "rest", f.now())
	mustEqual(t, len(f.hub.sendWorkerCalls), 0, "no send when rest active")
}

func TestRecheckAndResumeMissingWorkerStillSends(t *testing.T) {
	f := makeManager(t, 45)
	_ = f.mgr.RecheckAndResume(context.Background(), "ghost", 77.0)
	mustEqual(t, len(f.hub.sendWorkerCalls), 1, "resume even for missing worker")
	mustEqual(t, f.hub.sendWorkerCalls[0].msg["ts"].(float64), 77.0, "ts")
}

func TestRecheckAndResumeSendError(t *testing.T) {
	f := makeManager(t, 45)
	f.hub.sendWorkerErr = errors.New("boom")
	f.registry.Put("w1", makeState())
	err := f.mgr.RecheckAndResume(context.Background(), "w1", 1.0)
	mustTrue(t, err != nil, "send error propagates")
	mustEqual(t, len(f.hub.notifyCalls), 0, "no notify after send error")
}

func TestRecheckSendPrecedesNotify(t *testing.T) {
	f := makeManager(t, 45)
	f.registry.Put("w1", makeState())
	_ = f.mgr.RecheckAndResume(context.Background(), "w1", f.now())
	mustDeepEqual(t, f.hub.seq, []string{"send_worker", "notify"}, "order")
}

func TestCleanupExpiredMissingAndIdle(t *testing.T) {
	f := makeManager(t, 45)
	ok, _ := f.mgr.CleanupExpired(context.Background(), "ghost")
	mustFalse(t, ok, "missing -> false")
	mustEqual(t, len(f.hub.seq), 0, "no pipeline")

	f.registry.Put("idle", makeState())
	ok, _ = f.mgr.CleanupExpired(context.Background(), "idle")
	mustFalse(t, ok, "idle -> false")
	mustEqual(t, len(f.hub.seq), 0, "no pipeline")
}

func TestCleanupExpiredNothingExpired(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("w1", st)
	ok, _ := f.mgr.CleanupExpired(context.Background(), "w1")
	mustFalse(t, ok, "fresh lease -> false")
	mustEqual(t, len(f.hub.metrics), 0, "no metric")
	mustEqual(t, len(f.hub.seq), 0, "no pipeline")
	mustTrue(t, st.HijackSession != nil, "session untouched")
}

func TestCleanupExpiredRestOnlyPipeline(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()-1)
	f.registry.Put("w1", st)
	ok, _ := f.mgr.CleanupExpired(context.Background(), "w1")
	mustTrue(t, ok, "cleaned")
	mustDeepEqual(t, f.hub.seq, []string{
		"metric", "recheck", "send_worker", "notify", "append_event", "broadcast", "prune",
	}, "ordered pipeline")
	mustDeepEqual(t, f.hub.metrics, []string{"hijack_lease_expiries_total"}, "metric")
	mustDeepEqual(t, f.hub.events, []eventCall{{"w1", "hijack_lease_expired"}}, "rest event")
	mustDeepEqual(t, f.hub.broadcastCalls, []string{"w1"}, "broadcast")
	mustDeepEqual(t, f.hub.pruneCalls, []string{"w1"}, "prune")
	mustEqual(t, len(f.hub.sendWorkerCalls), 1, "resume sent")
	mustEqual(t, f.hub.sendWorkerCalls[0].msg["ts"].(float64), f.now(), "ts is sweep now")
	mustDeepEqual(t, f.hub.notifyCalls, []notifyCall{{"w1", false, nil}}, "notify")
}

func TestCleanupExpiredDashboardOnly(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackOwner = newBrowser("o")
	st.HijackOwnerExpiresAt = f64p(f.now() - 1)
	f.registry.Put("w1", st)
	ok, _ := f.mgr.CleanupExpired(context.Background(), "w1")
	mustTrue(t, ok, "cleaned")
	mustDeepEqual(t, f.hub.events, []eventCall{{"w1", "hijack_owner_expired"}}, "dashboard event only")
}

func TestCleanupExpiredBothInOrder(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()-1)
	st.HijackOwner = newBrowser("o")
	st.HijackOwnerExpiresAt = f64p(f.now() - 1)
	f.registry.Put("w1", st)
	ok, _ := f.mgr.CleanupExpired(context.Background(), "w1")
	mustTrue(t, ok, "cleaned")
	mustDeepEqual(t, f.hub.events, []eventCall{
		{"w1", "hijack_lease_expired"}, {"w1", "hijack_owner_expired"},
	}, "rest before dashboard")
	mustDeepEqual(t, f.hub.seq, []string{
		"metric", "recheck", "send_worker", "notify",
		"append_event", "append_event", "broadcast", "prune",
	}, "both-event pipeline")
}

func TestCleanupExpiredPartialSkipsRecheck(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()-1)
	st.HijackOwner = newBrowser("o")
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	f.registry.Put("w1", st)
	ok, _ := f.mgr.CleanupExpired(context.Background(), "w1")
	mustTrue(t, ok, "cleaned")
	mustEqual(t, len(f.hub.recheckCalls), 0, "no recheck")
	mustEqual(t, len(f.hub.sendWorkerCalls), 0, "no resume")
	mustEqual(t, len(f.hub.notifyCalls), 0, "no notify")
	mustDeepEqual(t, f.hub.metrics, []string{"hijack_lease_expiries_total"}, "metric fires")
	mustDeepEqual(t, f.hub.seq, []string{"metric", "append_event", "broadcast", "prune"}, "pipeline")
}

func TestGetRestSession(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	hs := restSession("h", "o", f.now()+100)
	st.HijackSession = hs
	f.registry.Put("w1", st)
	got, _ := f.mgr.GetRestSession(context.Background(), "w1", "h")
	mustTrue(t, got == hs, "returns live session")

	mustSessionNil := func(id, hid string) {
		g, _ := f.mgr.GetRestSession(context.Background(), id, hid)
		mustTrue(t, g == nil, "nil session for "+id)
	}
	mustSessionNil("ghost", "h")
	mustSessionNil("w1", "wrong")
}

func TestGetRestSessionRunsCleanup(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()-1)
	f.registry.Put("w1", st)
	got, _ := f.mgr.GetRestSession(context.Background(), "w1", "h")
	mustTrue(t, got == nil, "stale session swept")
	mustTrue(t, st.HijackSession == nil, "cleared by cleanup")
	mustTrue(t, containsEvent(f.hub.events, "w1", "hijack_lease_expired"), "cleanup ran")
}

func TestGetRestSessionNoCleanup(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	hs := restSession("h", "o", f.now()+100)
	st.HijackSession = hs
	f.registry.Put("w1", st)
	mustTrue(t, f.mgr.getRestSessionNoCleanup("w1", "h") == hs, "valid returns session")
	mustTrue(t, f.mgr.getRestSessionNoCleanup("ghost", "h") == nil, "missing worker")
	mustTrue(t, f.mgr.getRestSessionNoCleanup("w1", "wrong") == nil, "id mismatch")

	nosess := makeState()
	f.registry.Put("nosess", nosess)
	mustTrue(t, f.mgr.getRestSessionNoCleanup("nosess", "h") == nil, "no session")

	expired := makeState()
	staleHS := restSession("h", "o", f.now()-1)
	expired.HijackSession = staleHS
	f.registry.Put("exp", expired)
	mustTrue(t, f.mgr.getRestSessionNoCleanup("exp", "h") == nil, "expired -> nil (no sweep)")
	mustTrue(t, expired.HijackSession == staleHS, "no-cleanup leaves session in place")
	mustEqual(t, len(f.hub.events), 0, "no cleanup side effects")
}

func TestGetRestSessionBoundaryIsExpired(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()) // == now -> <= now -> expired
	f.registry.Put("w1", st)
	mustTrue(t, f.mgr.getRestSessionNoCleanup("w1", "h") == nil, "== now expired")
}

func TestGetEventsData(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.EventSeq = 9
	st.MinEventSeq = 3
	st.Events = append(st.Events, map[string]any{"seq": 5, "type": "e5"})
	f.registry.Put("w1", st)
	hs := restSession("h", "o", f.now()+30)
	data := f.mgr.GetEventsData("w1", "h", hs, 0, 100)
	mustDeepEqual(t, data, map[string]any{
		"rows":          []map[string]any{{"seq": 5, "type": "e5"}},
		"latest_seq":    9,
		"min_event_seq": 3,
		"fresh_expires": hs.LeaseExpiresAt,
	}, "exact payload")
}

func TestGetEventsDataFilterAndLimit(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.EventSeq = 6
	st.MinEventSeq = 0
	for i := 1; i <= 6; i++ {
		st.Events = append(st.Events, map[string]any{"seq": i})
	}
	f.registry.Put("w1", st)
	hs := restSession("h", "o", f.now()+30)
	data := f.mgr.GetEventsData("w1", "h", hs, 2, 2)
	rows := data["rows"].([]map[string]any)
	mustDeepEqual(t, []int{rows[0]["seq"].(int), rows[1]["seq"].(int)}, []int{3, 4}, "after_seq + limit")
}

func TestGetEventsDataMissingSeqDefaultsZero(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.Events = append(st.Events, map[string]any{"type": "no-seq"}, map[string]any{"seq": 2, "type": "e2"})
	f.registry.Put("w1", st)
	hs := restSession("h", "o", f.now()+30)
	data := f.mgr.GetEventsData("w1", "h", hs, 0, 100)
	rows := data["rows"].([]map[string]any)
	mustEqual(t, len(rows), 1, "seqless (0) filtered out at after_seq 0")
	mustEqual(t, rows[0]["seq"].(int), 2, "only e2")

	dataNeg := f.mgr.GetEventsData("w1", "h", hs, -1, 100)
	rowsNeg := dataNeg["rows"].([]map[string]any)
	mustEqual(t, len(rowsNeg), 2, "default 0 passes when after_seq -1")
}

func TestGetEventsDataFreshExpires(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	sessionExpiry := f.now() + 99
	st.HijackSession = restSession("h", "o", sessionExpiry)
	f.registry.Put("w1", st)
	hs := restSession("h", "o", f.now()+5)
	// id match -> live session expiry
	mustEqual(t, f.mgr.GetEventsData("w1", "h", hs, 0, 100)["fresh_expires"].(float64), sessionExpiry, "live expiry")
	// id mismatch -> fallback
	mustEqual(t, f.mgr.GetEventsData("w1", "other", hs, 0, 100)["fresh_expires"].(float64), hs.LeaseExpiresAt, "fallback on mismatch")

	nosess := makeState()
	f.registry.Put("nosess", nosess)
	mustEqual(t, f.mgr.GetEventsData("nosess", "h", hs, 0, 100)["fresh_expires"].(float64), hs.LeaseExpiresAt, "fallback no session")
}

func TestGetEventsDataUnknownWorker(t *testing.T) {
	f := makeManager(t, 45)
	hs := restSession("h", "o", 42.0)
	data := f.mgr.GetEventsData("ghost", "h", hs, 0, 100)
	mustDeepEqual(t, data, map[string]any{
		"rows":          []map[string]any{},
		"latest_seq":    0,
		"min_event_seq": 0,
		"fresh_expires": hs.LeaseExpiresAt,
	}, "fallback dict")
}

func TestRemoveDeadBrowsers(t *testing.T) {
	f := makeManager(t, 45)
	changed, _ := f.mgr.RemoveDeadBrowsers(context.Background(), "ghost", []BrowserConn{newBrowser("x")})
	mustFalse(t, changed, "missing worker -> false")

	st := makeState()
	d1, d2, keep := newBrowser("d1"), newBrowser("d2"), newBrowser("keep")
	st.Browsers[d1] = "viewer"
	st.Browsers[d2] = "viewer"
	st.Browsers[keep] = "admin"
	f.registry.Put("w1", st)
	changed, _ = f.mgr.RemoveDeadBrowsers(context.Background(), "w1", []BrowserConn{d1, d2})
	mustFalse(t, changed, "non-owner deaths -> false")
	_, ok1 := st.Browsers[d1]
	_, ok2 := st.Browsers[d2]
	_, okk := st.Browsers[keep]
	mustFalse(t, ok1 || ok2, "dead popped")
	mustTrue(t, okk, "keep retained")
}

func TestRemoveDeadBrowsersPopMissingTolerated(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	f.registry.Put("w1", st)
	changed, _ := f.mgr.RemoveDeadBrowsers(context.Background(), "w1", []BrowserConn{newBrowser("absent")})
	mustFalse(t, changed, "absent socket tolerated")
}

func TestRemoveDeadBrowsersOwnerDeathResumes(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	owner := newBrowser("o")
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	st.Browsers[owner] = "admin"
	f.registry.Put("w1", st)
	changed, _ := f.mgr.RemoveDeadBrowsers(context.Background(), "w1", []BrowserConn{owner})
	mustTrue(t, changed, "owner death -> resume")
	mustTrue(t, st.HijackOwner == nil && st.HijackOwnerExpiresAt == nil, "owner cleared")
	mustEqual(t, len(f.hub.sendWorkerCalls), 1, "one resume")
	msg := f.hub.sendWorkerCalls[0].msg
	mustEqual(t, msg["action"].(string), "resume", "action")
	mustEqual(t, msg["owner"].(string), "dead-socket", "owner label")
	mustEqual(t, msg["lease_s"].(int), 0, "lease_s")
	mustEqual(t, msg["ts"].(float64), f.clock.Wall(), "ts wall")
	mustDeepEqual(t, f.hub.notifyCalls, []notifyCall{{"w1", false, nil}}, "notify")
}

func TestRemoveDeadBrowsersOwnerDeathRestSuppresses(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	owner := newBrowser("o")
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	st.Browsers[owner] = "admin"
	st.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("w1", st)
	changed, _ := f.mgr.RemoveDeadBrowsers(context.Background(), "w1", []BrowserConn{owner})
	mustFalse(t, changed, "rest lease suppresses resume")
	mustTrue(t, st.HijackOwner == nil, "owner still cleared")
	mustEqual(t, len(f.hub.sendWorkerCalls), 0, "no resume")
}

func TestRemoveDeadBrowsersInactiveDashboardSkips(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	owner := newBrowser("o")
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(f.now() - 1)
	st.Browsers[owner] = "admin"
	f.registry.Put("w1", st)
	changed, _ := f.mgr.RemoveDeadBrowsers(context.Background(), "w1", []BrowserConn{owner})
	mustFalse(t, changed, "inactive dashboard -> no owner-clear")
	mustTrue(t, st.HijackOwner == owner, "owner untouched")
}

func TestRemoveDeadBrowsersConcurrentRecheckSuppresses(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	owner := newBrowser("o")
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	st.Browsers[owner] = "admin"
	f.registry.Put("w1", st)
	calls := 0
	f.hub.isHijackedOverride = func(*WorkerTermState) bool { calls++; return true }
	changed, _ := f.mgr.RemoveDeadBrowsers(context.Background(), "w1", []BrowserConn{owner})
	mustFalse(t, changed, "recheck sees concurrent hijack -> suppress")
	mustEqual(t, calls, 1, "recheck consulted is_hijacked once")
	mustEqual(t, len(f.hub.sendWorkerCalls), 0, "no resume")
}

func TestRemoveDeadBrowsersSendError(t *testing.T) {
	f := makeManager(t, 45)
	f.hub.sendWorkerErr = errors.New("boom")
	st := makeState()
	owner := newBrowser("o")
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	st.Browsers[owner] = "admin"
	f.registry.Put("w1", st)
	_, err := f.mgr.RemoveDeadBrowsers(context.Background(), "w1", []BrowserConn{owner})
	mustTrue(t, err != nil, "send error propagates")
}

func TestCleanupExpiredCallbackErrors(t *testing.T) {
	boom := errors.New("boom")
	// Each callback error aborts the pipeline and propagates.
	for _, tc := range []struct {
		name  string
		apply func(h *fakeLeaseHub)
	}{
		{"recheck", func(h *fakeLeaseHub) { h.recheckErr = boom }},
		{"append_event", func(h *fakeLeaseHub) { h.appendEventErr = boom }},
		{"broadcast", func(h *fakeLeaseHub) { h.broadcastErr = boom }},
		{"prune", func(h *fakeLeaseHub) { h.pruneErr = boom }},
	} {
		f := makeManager(t, 45)
		tc.apply(f.hub)
		st := makeState()
		st.HijackSession = restSession("h", "o", f.now()-1)
		f.registry.Put("w1", st)
		ok, err := f.mgr.CleanupExpired(context.Background(), "w1")
		mustFalse(t, ok, tc.name+" -> false")
		mustTrue(t, err == boom, tc.name+" error propagates")
	}
}

func TestCleanupExpiredDashboardAppendError(t *testing.T) {
	f := makeManager(t, 45)
	f.hub.appendEventErr = errors.New("boom")
	st := makeState()
	st.HijackOwner = newBrowser("o")
	st.HijackOwnerExpiresAt = f64p(f.now() - 1)
	f.registry.Put("w1", st)
	_, err := f.mgr.CleanupExpired(context.Background(), "w1")
	mustTrue(t, err != nil, "dashboard append error propagates")
}

func TestGetRestSessionCleanupError(t *testing.T) {
	f := makeManager(t, 45)
	f.hub.broadcastErr = errors.New("boom")
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()-1)
	f.registry.Put("w1", st)
	_, err := f.mgr.GetRestSession(context.Background(), "w1", "h")
	mustTrue(t, err != nil, "cleanup error propagates")
}

func containsEvent(events []eventCall, workerID, eventType string) bool {
	for _, e := range events {
		if e.workerID == workerID && e.eventType == eventType {
			return true
		}
	}
	return false
}
