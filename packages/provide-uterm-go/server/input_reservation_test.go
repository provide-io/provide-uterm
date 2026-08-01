//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
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
	mu      sync.Mutex
	entered chan struct{}
	release chan struct{}
	match   func(string) bool
}

func (w *reservationWorker) SendText(ctx context.Context, payload string) error {
	w.mu.Lock()
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

func assertTransitionWaits(t *testing.T, transition func() any, releaseSend func()) any {
	t.Helper()
	started := make(chan struct{})
	done := make(chan any, 1)
	go func() {
		close(started)
		done <- transition()
	}()
	<-started
	select {
	case result := <-done:
		releaseSend()
		t.Fatalf("lifecycle transition completed during reserved input delivery: %#v", result)
	case <-time.After(100 * time.Millisecond):
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

	result := assertTransitionWaits(t, func() any {
		return ts.do("POST", "/worker/reserved-rest/hijack/"+hijackID+"/release", "", adminHeaders())
	}, releaseSend).(*httptest.ResponseRecorder)
	if result.Code != http.StatusOK {
		t.Fatalf("release status = %d, body=%s", result.Code, result.Body.String())
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

	result := assertTransitionWaits(t, func() any {
		cleaned, err := ts.hub.CleanupExpiredHijack(context.Background(), "reserved-rest")
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

	result := assertTransitionWaits(t, func() any {
		released, restActive := ts.hub.TryReleaseWsHijack(context.Background(), "reserved-browser", browser)
		return [2]bool{released, restActive}
	}, releaseSend).([2]bool)
	if !result[0] || result[1] {
		t.Fatalf("browser release = %v", result)
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

	result := assertTransitionWaits(t, func() any {
		changed, err := ts.hub.RemoveDeadBrowsers(context.Background(), "reserved-dead-browser", []hub.BrowserConn{browser})
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

	result := assertTransitionWaits(t, func() any {
		cleanup, err := ts.hub.CleanupBrowserDisconnect(context.Background(), "reserved-disconnect", browser, true)
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

	result := assertTransitionWaits(t, func() any {
		previousHijacked, err := ts.hub.RegisterWorker(context.Background(), "reserved-replacement", replacement)
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
	state.Browsers[owner] = "admin"
	state.Browsers[competitor] = "admin"
	expires := 1e12
	state.HijackOwner = owner
	state.HijackOwnerExpiresAt = &expires
	ts.hub.Registry.Put("reserved-competitor", state)
	entered, releaseSend := worker.blockNextMatching(t, func(payload string) bool { return payload == "id" })
	go ts.srv.sendBrowserInput(context.Background(), "reserved-competitor", owner, "id")
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("browser input did not reach worker")
	}

	result := assertTransitionWaits(t, func() any {
		ok, reason := ts.hub.TryAcquireWsHijack(context.Background(), "reserved-competitor", competitor)
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
