//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

type reservationWorker struct {
	mu       sync.Mutex
	entered  chan struct{}
	release  chan struct{}
	match    func(string) bool
	payloads []string
}

type lifecycleRecordingWorker struct {
	mu       sync.Mutex
	payloads []string
}

func (w *lifecycleRecordingWorker) SendText(_ context.Context, payload string) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.payloads = append(w.payloads, payload)
	return nil
}

func (w *lifecycleRecordingWorker) snapshot() []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]string(nil), w.payloads...)
}

type recordingTunnelWorker struct{ lifecycleRecordingWorker }

func (*recordingTunnelWorker) SendInput(context.Context, string) error { return nil }
func (*recordingTunnelWorker) SendHTTPControl(context.Context, map[string]any) error {
	return nil
}

func (w *reservationWorker) SendText(ctx context.Context, payload string) error {
	w.mu.Lock()
	w.payloads = append(w.payloads, payload)
	if w.entered == nil || (w.match != nil && !w.match(payload)) {
		w.mu.Unlock()
		return nil
	}
	entered, release := w.entered, w.release
	w.entered, w.release, w.match = nil, nil, nil
	w.mu.Unlock()
	if entered == nil {
		return nil
	}
	close(entered)
	select {
	case <-release:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (w *reservationWorker) payloadSnapshot() []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]string(nil), w.payloads...)
}

func (w *reservationWorker) blockNextMatching(t *testing.T, match func(string) bool) (<-chan struct{}, func()) {
	t.Helper()
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.entered != nil {
		t.Fatal("worker send is already blocked")
	}
	entered := make(chan struct{})
	release := make(chan struct{})
	w.entered, w.release, w.match = entered, release, match
	var once sync.Once
	return entered, func() { once.Do(func() { close(release) }) }
}

func reservationRESTFixture(t *testing.T) (*testServer, *reservationWorker, *hub.ManualClock, string) {
	t.Helper()
	clock := hub.NewManualClock(1000)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Clock = clock
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock:    clock,
			OnMetric: deps.Metrics.Inc,
			Logger:   deps.Logger,
		})
	})
	const workerID = "reserved-rest"
	ts.reg.add(workerID, "admin1", "public")
	worker := &reservationWorker{}
	if _, err := ts.hub.RegisterWorker(context.Background(), workerID, worker); err != nil {
		t.Fatalf("register worker: %v", err)
	}
	if _, _, err := ts.hub.SetInputMode(context.Background(), workerID, hub.InputModeHijack); err != nil {
		t.Fatalf("set input mode: %v", err)
	}
	recorder := ts.do("POST", "/worker/"+workerID+"/hijack/acquire", `{"owner":"tester","lease_s":120}`, adminHeaders())
	if recorder.Code != http.StatusOK {
		t.Fatalf("acquire: %d %s", recorder.Code, recorder.Body.String())
	}
	hijackID, _ := decode(t, recorder.Body.Bytes())["hijack_id"].(string)
	if hijackID == "" {
		t.Fatal("acquire returned no hijack id")
	}
	return ts, worker, clock, hijackID
}

func assertTransitionWaits(t *testing.T, transition func(context.Context) any, releaseSend func()) any {
	t.Helper()
	waitCtx, reachedWait := hub.WithReservationWaitBarrier(context.Background())
	done := make(chan any, 1)
	go func() {
		done <- transition(waitCtx)
	}()
	select {
	case <-reachedWait:
	case result := <-done:
		releaseSend()
		t.Fatalf("transition completed before reaching reservation wait: %#v", result)
	case <-time.After(5 * time.Second):
		releaseSend()
		t.Fatal("transition did not reach reservation wait")
	}
	releaseSend()
	select {
	case result := <-done:
		return result
	case <-time.After(5 * time.Second):
		t.Fatal("lifecycle transition did not resume after input delivery")
		return nil
	}
}

func TestRESTStepReleaseWaitsForReservedDelivery(t *testing.T) {
	ts, worker, _, hijackID := reservationRESTFixture(t)
	entered, releaseSend := worker.blockNextMatching(t, func(payload string) bool {
		return strings.Contains(payload, `"action":"step"`)
	})
	stepDone := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		stepDone <- ts.do("POST", "/worker/reserved-rest/hijack/"+hijackID+"/step", "", adminHeaders())
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("step did not reach worker")
	}

	result := assertTransitionWaits(t, func(ctx context.Context) any {
		released, resume, err := ts.hub.ReleaseRestHijackAndResume(ctx, "reserved-rest", hijackID)
		return struct {
			released bool
			resume   bool
			err      error
		}{released, resume, err}
	}, releaseSend).(struct {
		released bool
		resume   bool
		err      error
	})
	if !result.released || !result.resume || result.err != nil {
		t.Fatalf("release result = %+v", result)
	}
	if step := <-stepDone; step.Code != http.StatusOK {
		t.Fatalf("step status = %d, body=%s", step.Code, step.Body.String())
	}
}

func TestRESTSendExpiryWaitsForReservedDelivery(t *testing.T) {
	ts, worker, clock, hijackID := reservationRESTFixture(t)
	entered, releaseSend := worker.blockNextMatching(t, func(payload string) bool { return payload == "id" })
	sendDone := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		sendDone <- ts.do("POST", "/worker/reserved-rest/hijack/"+hijackID+"/send", `{"keys":"id"}`, adminHeaders())
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("send did not reach worker")
	}
	clock.SetMonotonic(121)

	result := assertTransitionWaits(t, func(ctx context.Context) any {
		cleaned, err := ts.hub.CleanupExpiredHijack(ctx, "reserved-rest")
		return struct {
			cleaned bool
			err     error
		}{cleaned: cleaned, err: err}
	}, releaseSend).(struct {
		cleaned bool
		err     error
	})
	if result.err != nil || !result.cleaned {
		t.Fatalf("expiry cleanup = cleaned:%t err:%v", result.cleaned, result.err)
	}
	if send := <-sendDone; send.Code != http.StatusOK {
		t.Fatalf("send status = %d, body=%s", send.Code, send.Body.String())
	}
}

func TestBrowserReleaseWaitsForReservedInputDelivery(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := &reservationWorker{}
	browser := &browserConn{}
	state := hub.NewWorkerTermState()
	state.WorkerWS = worker
	state.Browsers[browser] = "admin"
	state.HijackOwner = browser
	expires := 1e12
	state.HijackOwnerExpiresAt = &expires
	ts.hub.Registry.Put("reserved-browser", state)
	entered, releaseSend := worker.blockNextMatching(t, func(payload string) bool { return payload == "id" })
	sendDone := make(chan struct{})
	go func() {
		ts.srv.sendBrowserInput(context.Background(), "reserved-browser", browser, "id")
		close(sendDone)
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("browser input did not reach worker")
	}

	result := assertTransitionWaits(t, func(ctx context.Context) any {
		released, restActive, err := ts.hub.ReleaseWsHijack(ctx, "reserved-browser", browser)
		return struct {
			released   bool
			restActive bool
			err        error
		}{released, restActive, err}
	}, releaseSend).(struct {
		released   bool
		restActive bool
		err        error
	})
	if !result.released || result.restActive || result.err != nil {
		t.Fatalf("browser release = %+v", result)
	}
	select {
	case <-sendDone:
	case <-time.After(5 * time.Second):
		t.Fatal("browser input did not finish")
	}
}

func TestDeadBrowserRemovalWaitsForReservedInputDelivery(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := &reservationWorker{}
	browser := &browserConn{}
	state := hub.NewWorkerTermState()
	state.WorkerWS = worker
	state.Browsers[browser] = "admin"
	expires := 1e12
	state.HijackOwner = browser
	state.HijackOwnerExpiresAt = &expires
	ts.hub.Registry.Put("reserved-dead-browser", state)
	entered, releaseSend := worker.blockNextMatching(t, func(payload string) bool { return payload == "id" })
	go ts.srv.sendBrowserInput(context.Background(), "reserved-dead-browser", browser, "id")
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("browser input did not reach worker")
	}

	result := assertTransitionWaits(t, func(ctx context.Context) any {
		changed, err := ts.hub.RemoveDeadBrowsers(ctx, "reserved-dead-browser", []hub.BrowserConn{browser})
		return struct {
			changed bool
			err     error
		}{changed, err}
	}, releaseSend).(struct {
		changed bool
		err     error
	})
	if result.err != nil || !result.changed {
		t.Fatalf("dead browser removal = changed:%t err:%v", result.changed, result.err)
	}
}

func TestBrowserDisconnectWaitsForReservedInputDelivery(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := &reservationWorker{}
	browser := &browserConn{}
	state := hub.NewWorkerTermState()
	state.WorkerWS = worker
	state.Browsers[browser] = "admin"
	expires := 1e12
	state.HijackOwner = browser
	state.HijackOwnerExpiresAt = &expires
	ts.hub.Registry.Put("reserved-disconnect", state)
	entered, releaseSend := worker.blockNextMatching(t, func(payload string) bool { return payload == "id" })
	go ts.srv.sendBrowserInput(context.Background(), "reserved-disconnect", browser, "id")
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("browser input did not reach worker")
	}

	result := assertTransitionWaits(t, func(ctx context.Context) any {
		cleanup, err := ts.hub.CleanupBrowserDisconnect(ctx, "reserved-disconnect", browser, true)
		return struct {
			cleanup map[string]any
			err     error
		}{cleanup, err}
	}, releaseSend).(struct {
		cleanup map[string]any
		err     error
	})
	if result.err != nil || result.cleanup["was_owner"] != true {
		t.Fatalf("browser disconnect = cleanup:%v err:%v", result.cleanup, result.err)
	}
}

func TestWorkerReplacementWaitsForReservedInputDelivery(t *testing.T) {
	ts := newTestServer(t, nil)
	original := &reservationWorker{}
	replacement := &reservationWorker{}
	browser := &browserConn{}
	state := hub.NewWorkerTermState()
	state.WorkerWS = original
	state.Browsers[browser] = "admin"
	expires := 1e12
	state.HijackOwner = browser
	state.HijackOwnerExpiresAt = &expires
	ts.hub.Registry.Put("reserved-replacement", state)
	entered, releaseSend := original.blockNextMatching(t, func(payload string) bool { return payload == "id" })
	go ts.srv.sendBrowserInput(context.Background(), "reserved-replacement", browser, "id")
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("browser input did not reach original worker")
	}

	result := assertTransitionWaits(t, func(ctx context.Context) any {
		previousHijacked, err := ts.hub.RegisterWorker(ctx, "reserved-replacement", replacement)
		return struct {
			previousHijacked bool
			err              error
		}{previousHijacked, err}
	}, releaseSend).(struct {
		previousHijacked bool
		err              error
	})
	if result.err != nil || !result.previousHijacked {
		t.Fatalf("worker replacement = previous_hijacked:%t err:%v", result.previousHijacked, result.err)
	}
	if !ts.hub.IsActiveWorker(context.Background(), "reserved-replacement", replacement) {
		t.Fatal("replacement worker was not installed")
	}
}

func TestCompetingAcquireWaitsForReservedInputDelivery(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := &reservationWorker{}
	owner := &browserConn{}
	competitor := &browserConn{}
	state := hub.NewWorkerTermState()
	state.WorkerWS = worker
	expires := 1e12
	state.HijackOwner = owner
	state.HijackOwnerExpiresAt = &expires
	state.Browsers[owner] = "admin"
	ts.hub.Registry.Put("reserved-competitor", state)
	entered, releaseSend := worker.blockNextMatching(t, func(payload string) bool { return payload == "id" })
	go ts.srv.sendBrowserInput(context.Background(), "reserved-competitor", owner, "id")
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("browser input did not reach worker")
	}

	result := assertTransitionWaits(t, func(ctx context.Context) any {
		ok, reason := ts.hub.TryAcquireWsHijack(ctx, "reserved-competitor", competitor)
		return struct {
			ok     bool
			reason string
		}{ok, reason}
	}, releaseSend).(struct {
		ok     bool
		reason string
	})
	if result.ok || result.reason != "already_hijacked" {
		t.Fatalf("competing acquire = ok:%t reason:%q", result.ok, result.reason)
	}
}

func TestRegisterWorkerWithTransportPublishesTunnelModeAtomically(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := &reservationWorker{}
	if _, err := ts.hub.RegisterWorkerWithTransport(context.Background(), "atomic-tunnel", worker, true); err != nil {
		t.Fatalf("register tunnel worker: %v", err)
	}
	state := ts.hub.Registry.Get("atomic-tunnel")
	if state == nil || state.WorkerWS != worker || !state.IsTunnelWorker {
		t.Fatalf("published tunnel state = %+v", state)
	}
}

func TestBrowserReleaseResumeCompletesBeforeCompetingAcquire(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := &reservationWorker{}
	owner := &browserConn{}
	competitor := &browserConn{}
	state := hub.NewWorkerTermState()
	state.WorkerWS = worker
	expires := 1e12
	state.HijackOwner = owner
	state.HijackOwnerExpiresAt = &expires
	ts.hub.Registry.Put("release-order", state)
	entered, releaseResume := worker.blockNextMatching(t, func(payload string) bool {
		return strings.Contains(payload, `"action":"resume"`)
	})
	releaseDone := make(chan struct{})
	go func() {
		ts.srv.browserHijackRelease(context.Background(), "release-order", owner)
		close(releaseDone)
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("release did not reach resume send")
	}

	acquireDone := make(chan bool, 1)
	waitCtx, reachedWait := hub.WithReservationWaitBarrier(context.Background())
	go func() {
		ok, _ := ts.hub.TryAcquireWsHijack(waitCtx, "release-order", competitor)
		acquireDone <- ok
	}()
	select {
	case <-reachedWait:
	case ok := <-acquireDone:
		releaseResume()
		t.Fatalf("competing acquire completed before lifecycle wait: %t", ok)
	case <-time.After(5 * time.Second):
		releaseResume()
		t.Fatal("competing acquire did not reach lifecycle wait")
	}
	releaseResume()
	if ok := <-acquireDone; !ok {
		t.Fatal("competing acquire did not succeed after resume")
	}
	<-releaseDone
}

func TestRESTAcquirePauseBlocksWorkerReplacement(t *testing.T) {
	ts := newTestServer(t, nil)
	original := &reservationWorker{}
	replacement := &reservationWorker{}
	if _, err := ts.hub.RegisterWorker(context.Background(), "pause-replace", original); err != nil {
		t.Fatal(err)
	}
	entered, releasePause := original.blockNextMatching(t, func(payload string) bool {
		return strings.Contains(payload, `"action":"pause"`)
	})
	acquireDone := make(chan error, 1)
	go func() {
		ok, reason, err := ts.hub.TryAcquireRestHijack(context.Background(), "pause-replace", "owner", 60, "h1", 1)
		if err == nil && (!ok || reason != "") {
			err = fmt.Errorf("acquire = ok:%t reason:%q", ok, reason)
		}
		acquireDone <- err
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("REST acquire did not reach pause send")
	}

	replaceDone := make(chan error, 1)
	waitCtx, reachedWait := hub.WithReservationWaitBarrier(context.Background())
	go func() {
		_, err := ts.hub.RegisterWorker(waitCtx, "pause-replace", replacement)
		replaceDone <- err
	}()
	select {
	case <-reachedWait:
	case err := <-replaceDone:
		releasePause()
		t.Fatalf("worker replacement completed before lifecycle wait: %v", err)
	case <-time.After(5 * time.Second):
		releasePause()
		t.Fatal("worker replacement did not reach lifecycle wait")
	}
	releasePause()
	if err := <-acquireDone; err != nil {
		t.Fatal(err)
	}
	if err := <-replaceDone; err == nil {
		t.Fatal("replacement under active REST lease must be rejected")
	}
}

func TestRESTAcquirePauseBlocksWorkerDisconnect(t *testing.T) {
	ts := newTestServer(t, nil)
	original := &reservationWorker{}
	if _, err := ts.hub.RegisterWorker(context.Background(), "pause-disconnect", original); err != nil {
		t.Fatal(err)
	}
	entered, releasePause := original.blockNextMatching(t, func(payload string) bool {
		return strings.Contains(payload, `"action":"pause"`)
	})
	acquireDone := make(chan error, 1)
	go func() {
		ok, reason, err := ts.hub.TryAcquireRestHijack(context.Background(), "pause-disconnect", "owner", 60, "h1", 1)
		if err == nil && (!ok || reason != "") {
			err = fmt.Errorf("acquire = ok:%t reason:%q", ok, reason)
		}
		acquireDone <- err
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("REST acquire did not reach pause send")
	}
	disconnectDone := make(chan error, 1)
	waitCtx, reachedWait := hub.WithReservationWaitBarrier(context.Background())
	go func() {
		ok, err := ts.hub.DisconnectWorker(waitCtx, "pause-disconnect")
		if err == nil && !ok {
			err = fmt.Errorf("disconnect returned false")
		}
		disconnectDone <- err
	}()
	select {
	case <-reachedWait:
	case err := <-disconnectDone:
		releasePause()
		t.Fatalf("disconnect completed before lifecycle wait: %v", err)
	case <-time.After(5 * time.Second):
		releasePause()
		t.Fatal("disconnect did not reach lifecycle wait")
	}
	releasePause()
	if err := <-acquireDone; err != nil {
		t.Fatal(err)
	}
	if err := <-disconnectDone; err != nil {
		t.Fatal(err)
	}
	state := ts.hub.Registry.Get("pause-disconnect")
	if state != nil && (state.WorkerWS != nil || state.HijackSession != nil) {
		t.Fatalf("disconnect left active state: %+v", state)
	}
}

func TestRESTAcquirePauseFencesSetInputMode(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := &reservationWorker{}
	if _, err := ts.hub.RegisterWorker(context.Background(), "pause-mode", worker); err != nil {
		t.Fatal(err)
	}
	entered, releasePause := worker.blockNextMatching(t, func(payload string) bool {
		return strings.Contains(payload, `"action":"pause"`)
	})
	acquireDone := make(chan error, 1)
	go func() {
		_, _, err := ts.hub.TryAcquireRestHijack(context.Background(), "pause-mode", "owner", 60, "h1", 1)
		acquireDone <- err
	}()
	<-entered
	waitCtx, reachedWait := hub.WithReservationWaitBarrier(context.Background())
	type modeResult struct {
		ok     bool
		reason string
		err    error
	}
	modeDone := make(chan modeResult, 1)
	go func() {
		ok, reason, err := ts.hub.SetInputMode(waitCtx, "pause-mode", hub.InputModeOpen)
		modeDone <- modeResult{ok, reason, err}
	}()
	select {
	case <-reachedWait:
	case <-time.After(5 * time.Second):
		releasePause()
		t.Fatal("set_input_mode did not reach lifecycle wait")
	}
	releasePause()
	if err := <-acquireDone; err != nil {
		t.Fatal(err)
	}
	result := <-modeDone
	if result.err != nil || result.ok || result.reason != "active_hijack" {
		t.Fatalf("set mode after acquire = %+v", result)
	}
}

func TestRESTAcquirePauseFencesWorkerHelloMode(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := &reservationWorker{}
	if _, err := ts.hub.RegisterWorker(context.Background(), "pause-hello", worker); err != nil {
		t.Fatal(err)
	}
	entered, releasePause := worker.blockNextMatching(t, func(payload string) bool {
		return strings.Contains(payload, `"action":"pause"`)
	})
	acquireDone := make(chan error, 1)
	go func() {
		_, _, err := ts.hub.TryAcquireRestHijack(context.Background(), "pause-hello", "owner", 60, "h1", 1)
		acquireDone <- err
	}()
	<-entered
	waitCtx, reachedWait := hub.WithReservationWaitBarrier(context.Background())
	helloDone := make(chan bool, 1)
	go func() {
		ok, _ := ts.hub.SetWorkerHello(waitCtx, "pause-hello", hub.InputModeOpen, nil)
		helloDone <- ok
	}()
	select {
	case <-reachedWait:
	case <-time.After(5 * time.Second):
		releasePause()
		t.Fatal("worker hello did not reach lifecycle wait")
	}
	releasePause()
	if err := <-acquireDone; err != nil {
		t.Fatal(err)
	}
	if ok := <-helloDone; ok {
		t.Fatal("worker hello lowered mode after acquire")
	}
}

func TestTunnelRESTStepFailsWithoutSuccessEffects(t *testing.T) {
	ts, _, _, hijackID := reservationRESTFixture(t)
	state := ts.hub.Registry.Get("reserved-rest")
	state.IsTunnelWorker = true
	beforeMetric := ts.metrics.Snapshot()["hijack_steps_total"]
	beforeEvents := state.EventSeq
	rec := ts.do("POST", "/worker/reserved-rest/hijack/"+hijackID+"/step", "", adminHeaders())
	if rec.Code >= 200 && rec.Code < 300 {
		t.Fatalf("tunnel step unexpectedly succeeded: %d %s", rec.Code, rec.Body.String())
	}
	if got := ts.metrics.Snapshot()["hijack_steps_total"]; got != beforeMetric {
		t.Fatalf("tunnel step success metric changed: %d -> %d", beforeMetric, got)
	}
	if state.EventSeq != beforeEvents {
		t.Fatalf("tunnel step appended success event: %d -> %d", beforeEvents, state.EventSeq)
	}
}

func TestFailedRESTAcquireDoesNotResumeCompetingDashboardLease(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("acquire-competitor", "admin1", "public")
	worker := &lifecycleRecordingWorker{}
	if _, err := ts.hub.RegisterWorker(context.Background(), "acquire-competitor", worker); err != nil {
		t.Fatal(err)
	}
	state := ts.hub.Registry.Get("acquire-competitor")
	competitor := &browserConn{}
	state.Browsers[competitor] = "admin"
	state.HijackOwner = competitor
	expires := ts.srv.clock.Monotonic() + 60
	state.HijackOwnerExpiresAt = &expires
	state.InputMode = hub.InputModeOpen

	rec := ts.do("POST", "/worker/acquire-competitor/hijack/acquire", `{"owner":"challenger"}`, adminHeaders())
	if rec.Code != http.StatusConflict {
		t.Fatalf("failed acquire status = %d, body=%s", rec.Code, rec.Body.String())
	}
	for _, payload := range worker.snapshot() {
		if strings.Contains(payload, `"action":"resume"`) {
			t.Fatalf("failed acquire resumed competing lease: %q", payload)
		}
	}
}

func TestTunnelRESTAcquireRejectedWithoutSuccessEffects(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("tunnel-acquire", "admin1", "public")
	worker := &recordingTunnelWorker{}
	if _, err := ts.hub.RegisterWorkerWithTransport(context.Background(), "tunnel-acquire", worker, true); err != nil {
		t.Fatal(err)
	}
	state := ts.hub.Registry.Get("tunnel-acquire")
	beforeMetric := ts.metrics.Snapshot()["hijack_acquires_total"]
	beforeEvents := state.EventSeq
	rec := ts.do("POST", "/worker/tunnel-acquire/hijack/acquire", `{"owner":"tester"}`, adminHeaders())
	if rec.Code >= 200 && rec.Code < 300 {
		t.Fatalf("tunnel acquire unexpectedly succeeded: %d %s", rec.Code, rec.Body.String())
	}
	if state.HijackSession != nil {
		t.Fatalf("tunnel acquire published lease: %+v", state.HijackSession)
	}
	if got := ts.metrics.Snapshot()["hijack_acquires_total"]; got != beforeMetric {
		t.Fatalf("acquire metric changed: %d -> %d", beforeMetric, got)
	}
	if state.EventSeq != beforeEvents {
		t.Fatalf("acquire event changed: %d -> %d", beforeEvents, state.EventSeq)
	}
}

func TestBrowserInputUnparkedByApprovalContinuesThroughRealHandler(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := &reservationWorker{}
	browser := &browserConn{}
	if _, err := ts.hub.RegisterWorker(context.Background(), "approval-unpark", worker); err != nil {
		t.Fatal(err)
	}
	if _, err := ts.hub.RegisterBrowser(context.Background(), "approval-unpark", browser, "admin", true); err != nil {
		t.Fatal(err)
	}
	if ok, reason := ts.hub.TryAcquireWsHijack(context.Background(), "approval-unpark", browser); !ok {
		t.Fatalf("acquire browser owner: %s", reason)
	}
	generation, ok := ts.hub.BrowserInputFence("approval-unpark", browser)
	if !ok {
		t.Fatal("browser fence unavailable")
	}
	reqID, err := ts.hub.ParkBrowserForApproval(context.Background(), "approval-unpark", browser,
		"command\n", hub.PolicyDecision{Action: "hold", TimeoutS: 60}, generation)
	if err != nil {
		t.Fatal(err)
	}
	commandEntered, releaseCommand := worker.blockNextMatching(t, func(payload string) bool {
		return payload == "command\n"
	})
	resolveDone := make(chan error, 1)
	go func() {
		_, err := ts.hub.ResolveApproval(context.Background(), reqID, true, nil, nil)
		resolveDone <- err
	}()
	<-commandEntered
	waitCtx, reachedWait := hub.WithReservationWaitBarrier(context.Background())
	handlerDone := make(chan struct{})
	go func() {
		ts.srv.browserInputGated(waitCtx, "approval-unpark", browser,
			map[string]any{"type": "input", "data": "fresh\n"})
		close(handlerDone)
	}()
	select {
	case <-reachedWait:
	case <-handlerDone:
		releaseCommand()
		t.Fatal("real input handler dropped input after approval unpark")
	case <-time.After(5 * time.Second):
		releaseCommand()
		t.Fatal("real input handler did not reach approval reservation wait")
	}
	releaseCommand()
	if err := <-resolveDone; err != nil {
		t.Fatal(err)
	}
	<-handlerDone
	payloads := worker.payloadSnapshot()
	if len(payloads) < 2 || payloads[len(payloads)-2] != "command\n" || payloads[len(payloads)-1] != "fresh\n" {
		t.Fatalf("real handler delivery order = %q", payloads)
	}
}
