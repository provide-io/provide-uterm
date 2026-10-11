//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// doCtx is testServer.do with a caller-supplied request context and body.
func (ts *testServer) doCtx(ctx context.Context, method, target string, body io.Reader, headers map[string]string) *httptest.ResponseRecorder {
	if body == nil {
		body = http.NoBody
	}
	req := httptest.NewRequestWithContext(ctx, method, target, body)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	ts.srv.Handler().ServeHTTP(rec, req)
	return rec
}

// holdInputReservation parks an in-flight input delivery on workerID, so every
// hub transition that must wait for it blocks until the returned release runs.
func holdInputReservation(t *testing.T, ts *testServer, workerID string) {
	t.Helper()
	st := ts.hub.Registry.Get(workerID)
	if st == nil {
		t.Fatalf("worker %s not registered", workerID)
	}
	done := make(chan struct{})
	st.InputSendPending = &hub.InputSendReservation{Worker: st.WorkerWS, WorkerGeneration: st.WorkerGeneration, Done: done}
	t.Cleanup(func() { close(done) })
}

func cancelledCtx() context.Context {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	return ctx
}

func assertHiddenHubFailure(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	if rec.Code != http.StatusInternalServerError || !strings.Contains(rec.Body.String(), hubOperationFailed) {
		t.Fatalf("status/body = %d %s; want 500 %q", rec.Code, rec.Body.String(), hubOperationFailed)
	}
	if strings.Contains(rec.Body.String(), "context canceled") {
		t.Fatalf("internal error leaked: %s", rec.Body.String())
	}
}

func TestHijackAcquireHubFailureIsHidden(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.setupWorker(t, "w1")
	holdInputReservation(t, ts, "w1")
	rec := ts.doCtx(cancelledCtx(), "POST", "/worker/w1/hijack/acquire", strings.NewReader(`{}`), adminHeaders())
	assertHiddenHubFailure(t, rec)
	if st := ts.hub.Registry.Get("w1"); st.HijackSession != nil || st.HijackPending != nil {
		t.Fatalf("failed acquire left lease state: %+v", st.HijackSession)
	}
}

func TestWorkerInputModeHubFailureIsHidden(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.setupWorker(t, "w1")
	holdInputReservation(t, ts, "w1")
	rec := ts.doCtx(cancelledCtx(), "POST", "/worker/w1/input_mode", strings.NewReader(`{"input_mode":"open"}`), adminHeaders())
	assertHiddenHubFailure(t, rec)
	if st := ts.hub.Registry.Get("w1"); st.InputMode != hub.InputModeHijack {
		t.Fatalf("input mode changed to %q despite failure", st.InputMode)
	}
}

func TestDisconnectWorkerHubFailureIsHidden(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.setupWorker(t, "w1")
	holdInputReservation(t, ts, "w1")
	rec := ts.doCtx(cancelledCtx(), "POST", "/worker/w1/disconnect_worker", nil, adminHeaders())
	assertHiddenHubFailure(t, rec)
	if st := ts.hub.Registry.Get("w1"); st.WorkerWS == nil {
		t.Fatal("worker disconnected despite failure")
	}
}

// hookedBody runs hook on its first Read, then serves payload. Handlers read
// the body after their lease lookup, so the hook lands between the lookup and
// the operation that relies on it.
type hookedBody struct {
	once    sync.Once
	hook    func()
	payload io.Reader
}

func (b *hookedBody) Read(p []byte) (int, error) {
	b.once.Do(b.hook)
	return b.payload.Read(p)
}

func acquireLease(t *testing.T, ts *testServer, workerID string, headers map[string]string) string {
	t.Helper()
	rec := ts.do("POST", "/worker/"+workerID+"/hijack/acquire", `{"owner":"tester","lease_s":120}`, headers)
	if rec.Code != http.StatusOK {
		t.Fatalf("acquire: %d %s", rec.Code, rec.Body.String())
	}
	hid, _ := decode(t, rec.Body.Bytes())["hijack_id"].(string)
	if hid == "" {
		t.Fatal("no hijack id")
	}
	return hid
}

func releaseLeaseHook(t *testing.T, ts *testServer, workerID, hid string) func() {
	return func() {
		if released, _, err := ts.hub.ReleaseRestHijackAndResume(context.Background(), workerID, hid); !released || err != nil {
			t.Errorf("hook release = %v, %v", released, err)
		}
	}
}

func TestHijackHeartbeatLeaseLostMidRequest(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.setupWorker(t, "w1")
	hid := acquireLease(t, ts, "w1", adminHeaders())
	body := &hookedBody{hook: releaseLeaseHook(t, ts, "w1", hid), payload: strings.NewReader(`{"lease_s":60}`)}
	rec := ts.doCtx(context.Background(), "POST", "/worker/w1/hijack/"+hid+"/heartbeat", body, adminHeaders())
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "Invalid or expired hijack session.") {
		t.Fatalf("heartbeat = %d %s; want 404", rec.Code, rec.Body.String())
	}
}

func TestHijackSendLeaseLostMidRequest(t *testing.T) {
	ts := newTestServer(t, nil)
	worker := ts.setupWorker(t, "w1")
	hid := acquireLease(t, ts, "w1", adminHeaders())
	body := &hookedBody{hook: releaseLeaseHook(t, ts, "w1", hid), payload: strings.NewReader(`{"keys":"ls\n"}`)}
	rec := ts.doCtx(context.Background(), "POST", "/worker/w1/hijack/"+hid+"/send", body, adminHeaders())
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "Invalid or expired hijack session.") {
		t.Fatalf("send = %d %s; want 404", rec.Code, rec.Body.String())
	}
	worker.mu.Lock()
	defer worker.mu.Unlock()
	for _, sent := range worker.sent {
		if strings.Contains(sent, `"ls\n"`) {
			t.Fatalf("keys reached the worker after the lease was lost: %q", sent)
		}
	}
}

// guardFailWorker cancels the request (via cancel) and fails the send whenever
// the hub asks it for a snapshot, with an error that carries no message.
type guardFailWorker struct {
	fakeWorkerWS
	cancel context.CancelFunc
}

func (w *guardFailWorker) SendText(ctx context.Context, payload string) error {
	if strings.Contains(payload, "snapshot_req") && w.cancel != nil {
		w.cancel()
		return errors.New("")
	}
	return w.fakeWorkerWS.SendText(ctx, payload)
}

func TestHijackSendGuardErrorFallsBackToGenericReason(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("w1", "admin1", "public")
	worker := &guardFailWorker{}
	if _, err := ts.hub.RegisterWorker(context.Background(), "w1", worker); err != nil {
		t.Fatal(err)
	}
	if _, _, err := ts.hub.SetInputMode(context.Background(), "w1", hub.InputModeHijack); err != nil {
		t.Fatal(err)
	}
	hid := acquireLease(t, ts, "w1", adminHeaders())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	worker.cancel = cancel
	rec := ts.doCtx(ctx, "POST", "/worker/w1/hijack/"+hid+"/send", strings.NewReader(`{"keys":"x"}`), adminHeaders())
	if rec.Code != http.StatusConflict {
		t.Fatalf("send = %d %s; want 409", rec.Code, rec.Body.String())
	}
	if got := decode(t, rec.Body.Bytes())["error"]; got != "prompt_guard_not_satisfied" {
		t.Fatalf("error = %v; want the generic guard reason", got)
	}
}

func scopedAdmin(subject, workerID string) map[string]string {
	return map[string]string{"X-Subject": subject, "X-Role": "admin", "X-Admin-Session-Scope": workerID}
}

func TestHijackReleaseByAnotherScopedAdminIsForbidden(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.setupWorker(t, "w1")
	hid := acquireLease(t, ts, "w1", scopedAdmin("sa1", "w1"))
	rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/release", "", scopedAdmin("sa2", "w1"))
	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "Not the lease owner.") {
		t.Fatalf("release = %d %s; want 403", rec.Code, rec.Body.String())
	}
	if st := ts.hub.Registry.Get("w1"); st.HijackSession == nil || st.HijackSession.HijackID != hid {
		t.Fatal("refused release dropped the lease")
	}
}

// resumeFailWorker accepts everything except the resume frame.
type resumeFailWorker struct{ fakeWorkerWS }

func (w *resumeFailWorker) SendText(ctx context.Context, payload string) error {
	if strings.Contains(payload, `"resume"`) {
		return errors.New("socket gone")
	}
	return w.fakeWorkerWS.SendText(ctx, payload)
}

func TestHijackReleaseResumeFailureIsConflict(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("w1", "admin1", "public")
	if _, err := ts.hub.RegisterWorker(context.Background(), "w1", &resumeFailWorker{}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := ts.hub.SetInputMode(context.Background(), "w1", hub.InputModeHijack); err != nil {
		t.Fatal(err)
	}
	hid := acquireLease(t, ts, "w1", adminHeaders())
	rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/release", "", adminHeaders())
	if rec.Code != http.StatusConflict || !strings.Contains(rec.Body.String(), "Unable to finalize hijack release.") {
		t.Fatalf("release = %d %s; want 409", rec.Code, rec.Body.String())
	}
}

// hookRegistry runs onDefinition before every GetDefinition, letting a test act
// at a precise point inside a handler.
type hookRegistry struct {
	*fakeRegistry
	mu           sync.Mutex
	onDefinition func()
}

func (r *hookRegistry) GetDefinition(ctx context.Context, id string) (*serverconfig.SessionDefinition, bool) {
	r.mu.Lock()
	hook := r.onDefinition
	r.mu.Unlock()
	if hook != nil {
		hook()
	}
	return r.fakeRegistry.GetDefinition(ctx, id)
}

func (r *hookRegistry) setHook(fn func()) {
	r.mu.Lock()
	r.onDefinition = fn
	r.mu.Unlock()
}

func TestHijackReleaseLeaseLostDuringOwnershipCheck(t *testing.T) {
	var hr *hookRegistry
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		hr = &hookRegistry{fakeRegistry: deps.Registry.(*fakeRegistry)}
		deps.Registry = hr
	})
	ts.setupWorker(t, "w1")
	hid := acquireLease(t, ts, "w1", adminHeaders()) // AcquiredBy = admin1
	calls := 0
	release := releaseLeaseHook(t, ts, "w1", hid)
	// Call 1 is the route's authorization; call 2 is mayReleaseLease, which runs
	// only because admin2 did not acquire the lease. Lose the lease there.
	hr.setHook(func() {
		calls++
		if calls == 2 {
			release()
		}
	})
	rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/release", "", map[string]string{"X-Subject": "admin2", "X-Role": "admin"})
	hr.setHook(nil)
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "Invalid or expired hijack session.") {
		t.Fatalf("release = %d %s; want 404", rec.Code, rec.Body.String())
	}
	if calls != 2 {
		t.Fatalf("GetDefinition calls = %d, want 2", calls)
	}
}

func TestHijackReadRoutesRequireSessionRead(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("w1", "admin1", "private")
	hid := "00000000-0000-4000-8000-000000000001"
	for _, path := range []string{"/worker/w1/hijack/" + hid + "/events", "/worker/w1/hijack/" + hid + "/gui/screenshot"} {
		rec := ts.do("GET", path, "", viewerHeaders())
		if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "insufficient privileges") {
			t.Fatalf("%s = %d %s; want 403", path, rec.Code, rec.Body.String())
		}
	}
}
