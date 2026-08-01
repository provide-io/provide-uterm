//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"math"
	"testing"
	"time"
)

func TestTryAcquireRestEncodeError(t *testing.T) {
	// A non-finite wall clock makes the pause-frame JSON encode fail, exercising
	// the encode-error branch (otherwise unreachable with valid inputs).
	f := makeManager(t, 45)
	f.clock.SetWall(math.Inf(1))
	st := makeState()
	f.registry.Put("w1", st)
	ok, _, err := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "encode failure -> not ok")
	mustTrue(t, err != nil, "encode error returned")
	mustTrue(t, st.HijackPending == nil, "reservation rolled back")
	mustTrue(t, st.HijackSession == nil, "no session")
}

func TestTryAcquireRestSuccess(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	f.registry.Put("w1", st)
	ok, reason, err := f.mgr.TryAcquireRest(context.Background(), "w1", "alice", 77, "H-9", 1234.5)
	mustTrue(t, ok && err == nil, "acquired")
	mustEqual(t, reason, "", "no reason")
	hs := st.HijackSession
	mustTrue(t, hs != nil, "session created")
	mustEqual(t, hs.HijackID, "H-9", "hijack id")
	mustEqual(t, hs.Owner, "alice", "owner")
	mustEqual(t, hs.AcquiredAt, 1234.5, "acquired at")
	mustEqual(t, hs.LeaseExpiresAt, 1234.5+77, "lease = now+lease_s")
	mustEqual(t, hs.LastHeartbeat, 1234.5, "last heartbeat")
	mustTrue(t, st.HijackPending == nil, "pending cleared")
}

func TestTryAcquireRestPauseFrame(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	f.registry.Put("w1", st)
	_, _, _ = f.mgr.TryAcquireRest(context.Background(), "w1", "bob", 90, "HID-42", 10.0)
	ws := st.WorkerWS.(*recordingWorkerWS)
	mustEqual(t, ws.sentCount(), 1, "one send")
	frame := decodeControlPayload(t, ws.lastPayload())
	mustEqual(t, frame["type"].(string), "control", "type")
	mustEqual(t, frame["action"].(string), "pause", "action")
	mustEqual(t, frame["owner"].(string), "bob", "owner")
	mustEqual(t, frame["hijack_id"].(string), "HID-42", "hijack_id")
	mustEqual(t, jnumf(frame["ts"]), f.clock.Wall(), "ts is wall clock")
}

func TestTryAcquireRestNoWorker(t *testing.T) {
	f := makeManager(t, 45)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "ghost", "op", 90, "h", 1.0)
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "no_worker", "reason")
	mustTrue(t, f.registry.Get("ghost") == nil, "nothing registered")
}

func TestTryAcquireRestNoWorkerWs(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.WorkerWS = nil
	f.registry.Put("w1", st)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "no_worker", "reason")
	mustTrue(t, st.HijackSession == nil, "no session")
}

func TestTryAcquireRestOpenMode(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.InputMode = InputModeOpen
	f.registry.Put("w1", st)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "open_mode", "reason")
	mustTrue(t, st.HijackSession == nil, "no session")
	mustEqual(t, st.WorkerWS.(*recordingWorkerWS).sentCount(), 0, "no pause sent")
}

func TestTryAcquireRestAlreadyHijackedDashboard(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackOwner = newBrowser("o")
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	f.registry.Put("w1", st)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "already_hijacked", "reason")
	mustTrue(t, st.HijackSession == nil, "no session")
}

func TestTryAcquireRestAlreadyHijackedRest(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("existing", "other", f.now()+30)
	f.registry.Put("w1", st)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "already_hijacked", "reason")
	mustEqual(t, st.HijackSession.HijackID, "existing", "existing untouched")
}

func TestTryAcquireRestExpiredDashboardProceeds(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackOwner = newBrowser("o")
	st.HijackOwnerExpiresAt = f64p(f.now() - 1)
	f.registry.Put("w1", st)
	ok, _, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 5.0)
	mustTrue(t, ok, "acquired over stale dashboard")
	mustEqual(t, st.HijackSession.HijackID, "h", "session")
}

func TestTryAcquireRestExpiredRestProceeds(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("stale", "old", f.now()-1)
	f.registry.Put("w1", st)
	ok, _, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "new", 90, "fresh", 9.0)
	mustTrue(t, ok, "acquired over stale rest")
	mustEqual(t, st.HijackSession.HijackID, "fresh", "overwritten")
	mustEqual(t, st.HijackSession.Owner, "new", "new owner")
}

func TestTryAcquireRestSendFailure(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.WorkerWS = &recordingWorkerWS{err: errors.New("socket dead")}
	f.registry.Put("w1", st)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "no_worker", "reason")
	mustTrue(t, st.WorkerWS == nil, "worker_ws cleared")
	mustTrue(t, st.HijackSession == nil, "no session")
	mustTrue(t, st.HijackPending == nil, "pending rolled back")
}

func TestTryAcquireRestSendFailurePreservesNewWs(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	newWS := &recordingWorkerWS{}
	orig := &recordingWorkerWS{onSend: func(context.Context, string) error {
		st.WorkerWS = newWS // reconnect mid-send
		return errors.New("old socket dead")
	}}
	st.WorkerWS = orig
	f.registry.Put("w1", st)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "no_worker", "reason")
	mustTrue(t, st.WorkerWS == newWS, "fresh socket preserved")
	mustTrue(t, st.HijackPending == nil, "pending cleared")
	mustTrue(t, st.HijackSession == nil, "no session")
}

func TestTryAcquireRestSendFailureAfterWorkerRemoved(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.WorkerWS = &recordingWorkerWS{onSend: func(context.Context, string) error {
		f.registry.Pop("w1")
		return errors.New("dead")
	}}
	f.registry.Put("w1", st)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "ok")
	mustEqual(t, reason, "no_worker", "reason")
	mustTrue(t, f.registry.Get("w1") == nil, "worker gone")
	mustTrue(t, st.HijackSession == nil, "no session")
}

func TestTryAcquireRestWorkerRemovedDuringSend(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.WorkerWS = &recordingWorkerWS{onSend: func(context.Context, string) error {
		f.registry.Pop("w1") // vanish, send "succeeds"
		return nil
	}}
	f.registry.Put("w1", st)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "phase-3 st is nil")
	mustEqual(t, reason, "no_worker", "reason")
	mustTrue(t, st.HijackSession == nil, "no session")
}

func TestTryAcquireRestPendingSupersededDuringSend(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.WorkerWS = &recordingWorkerWS{onSend: func(context.Context, string) error {
		st.HijackPending = strp("other") // reservation changed
		return nil
	}}
	f.registry.Put("w1", st)
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "op", 90, "h", 1.0)
	mustFalse(t, ok, "phase-3 pending mismatch")
	mustEqual(t, reason, "no_worker", "reason")
	mustTrue(t, st.HijackSession == nil, "no session")
	mustEqual(t, *st.HijackPending, "other", "other reservation untouched")
}

func TestTryAcquireRestLockNotHeldDuringPause(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	started := make(chan struct{})
	release := make(chan struct{})
	st.WorkerWS = &recordingWorkerWS{onSend: func(context.Context, string) error {
		close(started)
		<-release
		return nil
	}}
	f.registry.Put("w1", st)

	done := make(chan struct{})
	go func() {
		_, _, _ = f.mgr.TryAcquireRest(context.Background(), "w1", "op", 60, "h", 1.0)
		close(done)
	}()
	<-started
	mustTrue(t, f.lock.TryLock(), "lock free during pause send")
	mustTrue(t, st.HijackPending != nil && *st.HijackPending == "h", "slot reserved")
	f.lock.Unlock()
	close(release)
	<-done
	mustTrue(t, st.HijackSession != nil && st.HijackSession.HijackID == "h", "finalised")
	mustTrue(t, st.HijackPending == nil, "pending cleared")
}

func TestTryAcquireRestReservationBlocksConcurrent(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	started := make(chan struct{})
	release := make(chan struct{})
	st.WorkerWS = &recordingWorkerWS{onSend: func(context.Context, string) error {
		close(started)
		<-release
		return nil
	}}
	f.registry.Put("w1", st)

	done := make(chan struct{})
	go func() {
		_, _, _ = f.mgr.TryAcquireRest(context.Background(), "w1", "a", 60, "h1", 1.0)
		close(done)
	}()
	<-started
	ok, reason, _ := f.mgr.TryAcquireRest(context.Background(), "w1", "b", 60, "h2", 2.0)
	mustFalse(t, ok, "second blocked")
	mustEqual(t, reason, "already_hijacked", "reason")
	close(release)
	<-done
	mustEqual(t, st.HijackSession.HijackID, "h1", "first won")
}

func TestTryAcquireRestCancellationRollback(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	started := make(chan struct{})
	st.WorkerWS = &recordingWorkerWS{onSend: func(ctx context.Context, _ string) error {
		close(started)
		<-ctx.Done() // block until cancelled
		return ctx.Err()
	}}
	f.registry.Put("w1", st)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_, _, _ = f.mgr.TryAcquireRest(ctx, "w1", "op", 60, "h", 1.0)
		close(done)
	}()
	<-started
	f.lock.Lock()
	mustTrue(t, st.HijackPending != nil && *st.HijackPending == "h", "reserved")
	f.lock.Unlock()
	cancel()
	<-done
	mustTrue(t, st.HijackPending == nil, "reservation rolled back")
	mustTrue(t, st.HijackSession == nil, "no session")
}

func TestTryAcquireRestPauseDeliveredFinalizeFailureRollsBackResume(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	var sends int
	var payloads []string
	ctx, cancel := context.WithCancel(context.Background())
	worker := &recordingWorkerWS{onSend: func(_ context.Context, payload string) error {
		sends++
		payloads = append(payloads, payload)
		if sends == 1 {
			cancel()
		}
		return nil
	}}
	st.WorkerWS = worker
	f.registry.Put("w1", st)

	ok, reason, err := f.mgr.TryAcquireRest(ctx, "w1", "owner", 60, "h1", f.now())
	if !errors.Is(err, context.Canceled) || ok || reason != "" {
		t.Fatalf("acquire = ok:%t reason:%q err:%v", ok, reason, err)
	}
	if got := len(payloads); got != 2 {
		t.Fatalf("pause-delivered rollback sends = %d, want pause + resume", got)
	}
	resume := decodeOneControl(t, payloads[1])
	if resume["action"] != "resume" || resume["hijack_id"] != "h1" {
		t.Fatalf("rollback frame = %v", resume)
	}
}

func TestExtendLease(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	hs := &HijackSession{HijackID: "h", Owner: "o", LeaseExpiresAt: 1000, AcquiredAt: 10, LastHeartbeat: 20}
	st.HijackSession = hs
	f.registry.Put("w1", st)
	exp := f.mgr.ExtendLease("w1", "h", "o", 90, 500.0)
	mustTrue(t, exp != nil, "extended")
	mustEqual(t, *exp, 590.0, "now+lease_s")
	mustEqual(t, hs.LastHeartbeat, 500.0, "heartbeat")
	mustEqual(t, hs.LeaseExpiresAt, 590.0, "expiry")
	mustEqual(t, hs.AcquiredAt, 10.0, "acquired untouched")
	mustEqual(t, len(f.hub.metrics), 0, "no metric on success")
}

func TestExtendLeaseMissingAndMismatch(t *testing.T) {
	f := makeManager(t, 45)
	mustTrue(t, f.mgr.ExtendLease("ghost", "h", "o", 90, 500) == nil, "missing worker")

	nosess := makeState()
	f.registry.Put("nosess", nosess)
	mustTrue(t, f.mgr.ExtendLease("nosess", "h", "o", 90, 500) == nil, "no session")

	st := makeState()
	hs := &HijackSession{HijackID: "real", Owner: "o", LeaseExpiresAt: 1000, LastHeartbeat: 20}
	st.HijackSession = hs
	f.registry.Put("w1", st)
	mustTrue(t, f.mgr.ExtendLease("w1", "wrong", "o", 90, 500) == nil, "id mismatch")
	mustEqual(t, hs.LeaseExpiresAt, 1000.0, "session untouched")
	mustEqual(t, len(f.hub.metrics), 0, "no owner-mismatch metric on id mismatch")
}

func TestExtendLeaseOwnerMismatch(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	hs := &HijackSession{HijackID: "h", Owner: "real-owner", LeaseExpiresAt: 1000, LastHeartbeat: 20}
	st.HijackSession = hs
	f.registry.Put("w1", st)
	res := f.mgr.ExtendLease("w1", "h", "attacker", 90, 500)
	mustTrue(t, res == nil, "denied")
	mustDeepEqual(t, f.hub.metrics, []string{"hijack_heartbeat_denied_owner_mismatch"}, "metric")
	mustEqual(t, hs.LeaseExpiresAt, 1000.0, "not extended")
}

func TestReleaseRest(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()+30)
	f.registry.Put("w1", st)
	released, resume := f.mgr.ReleaseRest("w1", "h")
	mustTrue(t, released && resume, "released with resume")
	mustTrue(t, st.HijackSession == nil, "session cleared")
}

func TestReleaseRestNoResumeWhenDashboardActive(t *testing.T) {
	f := makeManager(t, 45)
	st := makeState()
	st.HijackSession = restSession("h", "o", f.now()+30)
	st.HijackOwner = newBrowser("o")
	st.HijackOwnerExpiresAt = f64p(f.now() + 30)
	f.registry.Put("w1", st)
	released, resume := f.mgr.ReleaseRest("w1", "h")
	mustTrue(t, released, "released")
	mustFalse(t, resume, "no resume, dashboard active")
	mustTrue(t, st.HijackSession == nil, "session cleared")
}

func TestReleaseRestFailures(t *testing.T) {
	f := makeManager(t, 45)
	released, resume := f.mgr.ReleaseRest("ghost", "h")
	mustFalse(t, released || resume, "missing worker")

	nosess := makeState()
	f.registry.Put("nosess", nosess)
	released, resume = f.mgr.ReleaseRest("nosess", "h")
	mustFalse(t, released || resume, "no session")

	st := makeState()
	hs := restSession("real", "o", f.now()+30)
	st.HijackSession = hs
	f.registry.Put("w1", st)
	released, resume = f.mgr.ReleaseRest("w1", "fake")
	mustFalse(t, released || resume, "id mismatch")
	mustTrue(t, st.HijackSession == hs, "session kept")
}

func TestTryAcquireRestDefaultTimeoutUnusedField(t *testing.T) {
	// Exercises the send-failure debug path is a no-op observable-wise beyond
	// the return; also sanity-checks a normal acquire completes promptly.
	f := makeManager(t, 45)
	f.registry.Put("w1", makeState())
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	ok, _, err := f.mgr.TryAcquireRest(ctx, "w1", "op", 30, "h", 1.0)
	mustTrue(t, ok && err == nil, "acquired")
}
