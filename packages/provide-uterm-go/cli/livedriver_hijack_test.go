//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"testing"
	"time"
)

// liveHarness is a running LiveServer plus the plumbing a test needs to talk to
// it. Serve runs in a goroutine; Close cancels and waits.
type liveHarness struct {
	srv     *LiveServer
	cancel  context.CancelFunc
	serveCh chan error
}

// newLiveHarness stands the live-conformance server up on an ephemeral port and
// waits until it is ready in the sense the harness protocol means: the
// auto_start sessions are up and attached, so a client that arrives on the
// handshake can take a lease.
func newLiveHarness(t *testing.T) *liveHarness {
	t.Helper()
	t.Setenv("UTERM_DEV_TOKEN_PATH", filepath.Join(t.TempDir(), "dev_token"))
	ctx, cancel := context.WithCancel(context.Background())

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		cancel()
		t.Fatalf("listen: %v", err)
	}
	srv, err := NewLiveServer(ctx, ln, LiveServerOptions{AuthMode: "dev_token"})
	if err != nil {
		cancel()
		t.Fatalf("NewLiveServer: %v", err)
	}
	h := &liveHarness{srv: srv, cancel: cancel, serveCh: make(chan error, 1)}
	go func() { h.serveCh <- srv.Serve(ctx) }()
	t.Cleanup(h.close)

	readyCtx, readyCancel := context.WithTimeout(ctx, 20*time.Second)
	defer readyCancel()
	srv.WaitReady(readyCtx)
	return h
}

func (h *liveHarness) close() {
	h.cancel()
	<-h.serveCh
	_ = h.srv.Close()
}

// do performs one request against the harness, returning the status and the
// decoded JSON body. auth=false omits the bearer token.
func (h *liveHarness) do(t *testing.T, method, path string, body any, auth bool) (int, map[string]any) {
	t.Helper()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal %s %s: %v", method, path, err)
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, h.srv.BaseURL()+path, reader)
	if err != nil {
		t.Fatalf("build %s %s: %v", method, path, err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if auth && h.srv.Token() != "" {
		req.Header.Set("Authorization", "Bearer "+h.srv.Token())
	}
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read %s %s: %v", method, path, err)
	}
	out := map[string]any{}
	if len(bytes.TrimSpace(raw)) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("decode %s %s: %v (body %s)", method, path, err, raw)
		}
	}
	return resp.StatusCode, out
}

// TestLiveServerGrantsHijackLeaseOnDefaultSession is live-conformance scenario
// 006 held against the Go server: the whole lease lifecycle on the configured,
// auto-started session.
//
// It failed before the session attached a worker to the hub — acquire answered
// 409 {"error":"No worker connected for this session."}, because
// StartAutoStartSessions built a connector and nothing ever registered it with
// the hub, so the lease manager had no worker to pause and therefore nothing to
// lease.
func TestLiveServerGrantsHijackLeaseOnDefaultSession(t *testing.T) {
	h := newLiveHarness(t)

	status, body := h.do(t, http.MethodPost, "/api/sessions/provide-shell/mode",
		map[string]any{"input_mode": "hijack"}, true)
	if status != http.StatusOK || body["input_mode"] != "hijack" {
		t.Fatalf("set mode: status %d body %#v", status, body)
	}

	status, acquired := h.do(t, http.MethodPost, "/worker/provide-shell/hijack/acquire",
		map[string]any{"owner": "conformance", "lease_s": 60}, true)
	if status != http.StatusOK {
		t.Fatalf("acquire: status %d body %#v — the lease must be granted", status, acquired)
	}
	if acquired["ok"] != true || acquired["worker_id"] != "provide-shell" || acquired["owner"] != "conformance" {
		t.Fatalf("acquire body %#v", acquired)
	}
	if _, ok := acquired["lease_expires_at"].(float64); !ok {
		t.Fatalf("lease_expires_at = %#v, want a number", acquired["lease_expires_at"])
	}
	hijackID, _ := acquired["hijack_id"].(string)
	if hijackID == "" {
		t.Fatalf("hijack_id = %#v, want a non-empty id", acquired["hijack_id"])
	}

	base := "/worker/provide-shell/hijack/" + hijackID
	if status, body = h.do(t, http.MethodPost, base+"/heartbeat",
		map[string]any{"lease_s": 60}, true); status != http.StatusOK || body["ok"] != true {
		t.Fatalf("heartbeat: status %d body %#v", status, body)
	}
	if status, body = h.do(t, http.MethodGet, base+"/snapshot", nil, true); status != http.StatusOK {
		t.Fatalf("snapshot: status %d body %#v", status, body)
	}

	status, body = h.do(t, http.MethodPost, "/worker/provide-shell/hijack/acquire",
		map[string]any{"owner": "second", "lease_s": 60}, true)
	if status != http.StatusConflict || body["error"] != "Worker is already hijacked." {
		t.Fatalf("second acquire: status %d body %#v", status, body)
	}

	if status, body = h.do(t, http.MethodPost, base+"/release", map[string]any{}, true); status != http.StatusOK ||
		body["ok"] != true {
		t.Fatalf("release: status %d body %#v", status, body)
	}
	status, body = h.do(t, http.MethodPost, base+"/release", map[string]any{}, true)
	if status != http.StatusNotFound || body["error"] != "Invalid or expired hijack session." {
		t.Fatalf("release again: status %d body %#v", status, body)
	}
}

// TestLiveServerRefusesHijackInOpenMode is live-conformance scenario 007's
// refusals: the reasons a lease is not granted, and the envelope each is told
// in. The open-mode refusal in particular can only be reached once a worker is
// attached — before that every acquire was refused for the wrong reason.
func TestLiveServerRefusesHijackInOpenMode(t *testing.T) {
	h := newLiveHarness(t)

	status, body := h.do(t, http.MethodPost, "/worker/provide-shell/hijack/acquire",
		map[string]any{"owner": "conformance", "lease_s": 60}, true)
	if status != http.StatusConflict || body["error"] != "Hijack not available in open input mode." {
		t.Fatalf("acquire while open: status %d body %#v", status, body)
	}

	status, body = h.do(t, http.MethodPost, "/worker/no-such-worker/hijack/acquire",
		map[string]any{"owner": "conformance", "lease_s": 60}, true)
	if status != http.StatusNotFound || body["detail"] != "unknown session: no-such-worker" {
		t.Fatalf("unknown worker: status %d body %#v", status, body)
	}

	status, body = h.do(t, http.MethodPost, "/api/sessions/provide-shell/mode",
		map[string]any{"input_mode": "sideways"}, true)
	if status != http.StatusUnprocessableEntity || body["detail"] != "input_mode must be 'open' or 'hijack'" {
		t.Fatalf("undefined mode: status %d body %#v", status, body)
	}

	status, body = h.do(t, http.MethodPost, "/worker/provide-shell/hijack/acquire",
		map[string]any{"owner": "nobody", "lease_s": 60}, false)
	if status != http.StatusUnauthorized || body["detail"] != "authentication required" {
		t.Fatalf("anonymous acquire: status %d body %#v", status, body)
	}
}

// TestLiveServerDisconnectTakesTheWorkerAway is the other end of the
// attachment: a session that has been disconnected is not one a lease can be
// taken on, and the readiness the handshake waits for is no longer satisfied.
// A worker that outlived its terminal would be leased to an operator whose
// keystrokes had nowhere to go.
func TestLiveServerDisconnectTakesTheWorkerAway(t *testing.T) {
	h := newLiveHarness(t)

	if status, body := h.do(t, http.MethodPost, "/api/sessions/provide-shell/disconnect",
		nil, true); status != http.StatusOK {
		t.Fatalf("disconnect: status %d body %#v", status, body)
	}

	// The bridge tears its socket down asynchronously, so this is the outcome
	// polled for rather than asserted on the next instruction.
	deadline := time.Now().Add(10 * time.Second)
	var status int
	var body map[string]any
	for time.Now().Before(deadline) {
		status, body = h.do(t, http.MethodPost, "/worker/provide-shell/hijack/acquire",
			map[string]any{"owner": "conformance", "lease_s": 60}, true)
		if body["error"] == "No worker connected for this session." {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if status != http.StatusConflict || body["error"] != "No worker connected for this session." {
		t.Fatalf("acquire after disconnect: status %d body %#v", status, body)
	}

	// Readiness gives up on the caller's deadline rather than waiting forever
	// for a session nobody is bringing back.
	done, cancel := context.WithCancel(context.Background())
	cancel()
	h.srv.WaitReady(done)
}

// TestLiveServerReleasesHijackOnSwitchToOpen holds the Go server to the
// reference's set_mode: switching a leased session back to open force-releases
// the lease, so the session really is one everybody may type into rather than
// one still held by an operator who cannot be seen.
func TestLiveServerReleasesHijackOnSwitchToOpen(t *testing.T) {
	h := newLiveHarness(t)

	if status, body := h.do(t, http.MethodPost, "/api/sessions/provide-shell/mode",
		map[string]any{"input_mode": "hijack"}, true); status != http.StatusOK {
		t.Fatalf("set hijack: status %d body %#v", status, body)
	}
	status, acquired := h.do(t, http.MethodPost, "/worker/provide-shell/hijack/acquire",
		map[string]any{"owner": "first", "lease_s": 60}, true)
	if status != http.StatusOK {
		t.Fatalf("acquire: status %d body %#v", status, acquired)
	}

	if status, body := h.do(t, http.MethodPost, "/api/sessions/provide-shell/mode",
		map[string]any{"input_mode": "open"}, true); status != http.StatusOK {
		t.Fatalf("set open: status %d body %#v", status, body)
	}
	hijackID, _ := acquired["hijack_id"].(string)
	status, body := h.do(t, http.MethodPost, "/worker/provide-shell/hijack/"+hijackID+"/heartbeat",
		map[string]any{"lease_s": 60}, true)
	if status != http.StatusNotFound || body["error"] != "Invalid or expired hijack session." {
		t.Fatalf("heartbeat after switch to open: status %d body %#v", status, body)
	}
}
