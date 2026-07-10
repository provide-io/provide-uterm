//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// newLowSendRateServer builds a testServer whose hub rate-limits REST sends at
// 1 token/sec (burst 1) so the second send/step is rejected, exercising the
// rate-limit branches on live inputs (no fault injection).
func newLowSendRateServer(t *testing.T) *testServer {
	t.Helper()
	return newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock:                   deps.Clock,
			OnMetric:                deps.Metrics.Inc,
			Logger:                  deps.Logger,
			RestSendRateLimitPerSec: 1,
		})
	})
}

// acquireHijack registers a hijack-mode worker and acquires a REST hijack lease,
// returning the hijack id.
func acquireHijack(t *testing.T, ts *testServer, id string) string {
	t.Helper()
	ts.setupWorker(t, id)
	rec := ts.do("POST", "/worker/"+id+"/hijack/acquire", `{"owner":"tester","lease_s":120}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("acquire: %d %s", rec.Code, rec.Body.String())
	}
	hid, _ := decode(t, rec.Body.Bytes())["hijack_id"].(string)
	if hid == "" {
		t.Fatalf("no hijack id: %s", rec.Body.String())
	}
	return hid
}

// TestHijackSendStepRateLimited drives the REST send/step rate-limit branches.
func TestHijackSendStepRateLimited(t *testing.T) {
	ts := newLowSendRateServer(t)
	hid := acquireHijack(t, ts, "rl")

	// First step consumes the single send token.
	if rec := ts.do("POST", "/worker/rl/hijack/"+hid+"/step", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("first step: %d %s", rec.Code, rec.Body.String())
	}
	// Second step → rate limited (429).
	if rec := ts.do("POST", "/worker/rl/hijack/"+hid+"/step", "", adminHeaders()); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("step rate limit: %d", rec.Code)
	}
	// A send now is also rate limited (shared send bucket) → 429.
	if rec := ts.do("POST", "/worker/rl/hijack/"+hid+"/send", `{"keys":"x"}`, adminHeaders()); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("send rate limit: %d", rec.Code)
	}
}

// TestHijackSendKeysTooLong covers the keys-length guard.
func TestHijackSendKeysTooLong(t *testing.T) {
	ts := newTestServer(t, nil)
	hid := acquireHijack(t, ts, "kl")
	big := strings.Repeat("x", ts.hub.MaxInputChars()+1)
	rec := ts.do("POST", "/worker/kl/hijack/"+hid+"/send", `{"keys":"`+big+`"}`, adminHeaders())
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("keys too long: %d %s", rec.Code, rec.Body.String())
	}
}

// TestHijackSendGuardNotSatisfied covers the prompt-guard 409 branch: an
// expect_prompt_id that never matches within the timeout.
func TestHijackSendGuardNotSatisfied(t *testing.T) {
	ts := newTestServer(t, nil)
	hid := acquireHijack(t, ts, "gd")
	rec := ts.do("POST", "/worker/gd/hijack/"+hid+"/send",
		`{"keys":"ls\n","expect_prompt_id":"never","timeout_ms":100,"poll_interval_ms":50}`, adminHeaders())
	if rec.Code != http.StatusConflict {
		t.Fatalf("guard not satisfied: %d %s", rec.Code, rec.Body.String())
	}
	if decode(t, rec.Body.Bytes())["error"] == nil {
		t.Fatalf("expected error body: %s", rec.Body.String())
	}
}

// TestWorkerInputModeConflict covers the "cannot switch to open while hijacked"
// 409 branch and the invalid-worker-id 422 branch.
func TestWorkerInputModeConflict(t *testing.T) {
	ts := newTestServer(t, nil)
	_ = acquireHijack(t, ts, "im")
	// A REST hijack lease is active → switching to open is refused with 409.
	rec := ts.do("POST", "/worker/im/input_mode", `{"input_mode":"open"}`, adminHeaders())
	if rec.Code != http.StatusConflict {
		t.Fatalf("input_mode conflict: %d %s", rec.Code, rec.Body.String())
	}
	// Invalid worker id → 422 (bridgeParams).
	if rec := ts.do("POST", "/worker/BAD!ID/input_mode", `{"input_mode":"open"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("input_mode bad id: %d", rec.Code)
	}
	// Invalid worker id on disconnect → 422.
	if rec := ts.do("POST", "/worker/BAD!ID/disconnect_worker", "", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("disconnect bad id: %d", rec.Code)
	}
}
