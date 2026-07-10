//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// recordedRequest captures what a handler received for later assertions.
type recordedRequest struct {
	method  string
	path    string
	rawQ    string
	body    map[string]any
	rawBody string
	headers http.Header
}

// fakeServer is an httptest-backed fake of the provide-uterm REST API. Each
// entry in routes maps "METHOD /path" to a scripted response. Requests are
// recorded so tests can assert exact paths, bodies, and headers.
type fakeServer struct {
	t      *testing.T
	srv    *httptest.Server
	mu     sync.Mutex
	routes map[string]fakeResponse
	seen   []recordedRequest
}

type fakeResponse struct {
	status int
	body   any    // JSON-encoded unless raw != ""
	raw    string // written verbatim (non-JSON path) when non-empty
}

func newFakeServer(t *testing.T) *fakeServer {
	t.Helper()
	fs := &fakeServer{t: t, routes: map[string]fakeResponse{}}
	fs.srv = httptest.NewServer(http.HandlerFunc(fs.handle))
	t.Cleanup(fs.srv.Close)
	return fs
}

func (fs *fakeServer) on(method, path string, resp fakeResponse) {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	fs.routes[method+" "+path] = resp
}

func (fs *fakeServer) handle(w http.ResponseWriter, r *http.Request) {
	raw, _ := io.ReadAll(r.Body)
	rec := recordedRequest{
		method:  r.Method,
		path:    r.URL.Path,
		rawQ:    r.URL.RawQuery,
		rawBody: string(raw),
		headers: r.Header.Clone(),
	}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &rec.body)
	}
	fs.mu.Lock()
	fs.seen = append(fs.seen, rec)
	resp, ok := fs.routes[r.Method+" "+r.URL.Path]
	fs.mu.Unlock()

	if !ok {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"error":"unrouted"}`))
		return
	}
	if resp.raw != "" {
		w.WriteHeader(resp.status)
		_, _ = w.Write([]byte(resp.raw))
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(resp.status)
	_ = json.NewEncoder(w).Encode(resp.body)
}

func (fs *fakeServer) last() recordedRequest {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	if len(fs.seen) == 0 {
		fs.t.Fatal("no request recorded")
	}
	return fs.seen[len(fs.seen)-1]
}

func (fs *fakeServer) client(opts ...Option) *HijackClient {
	return NewHijackClient(fs.srv.URL, opts...)
}

func ctx() context.Context { return context.Background() }

// -- lifecycle happy paths ------------------------------------------------

func TestAcquireDefaultsAndWire(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/hijack/acquire", fakeResponse{status: 200, body: map[string]any{
		"ok": true, "hijack_id": "hj-1", "owner": "operator",
	}})
	c := fs.client()

	data, err := c.Acquire(ctx(), "w1", AcquireOptions{})
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	if data["hijack_id"] != "hj-1" {
		t.Fatalf("hijack_id: %v", data)
	}
	req := fs.last()
	if req.method != "POST" || req.path != "/worker/w1/hijack/acquire" {
		t.Fatalf("wrong route: %s %s", req.method, req.path)
	}
	if req.body["owner"] != "operator" {
		t.Fatalf("default owner not sent: %v", req.body)
	}
	if req.body["lease_s"].(float64) != 90 {
		t.Fatalf("default lease_s not 90: %v", req.body["lease_s"])
	}
	if req.headers.Get("Content-Type") != "application/json" {
		t.Fatalf("content-type: %q", req.headers.Get("Content-Type"))
	}
}

func TestAcquireCustomOwnerLease(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/hijack/acquire", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := fs.client()
	if _, err := c.Acquire(ctx(), "w1", AcquireOptions{Owner: "tester", LeaseS: 60}); err != nil {
		t.Fatal(err)
	}
	req := fs.last()
	if req.body["owner"] != "tester" || req.body["lease_s"].(float64) != 60 {
		t.Fatalf("custom acquire body: %v", req.body)
	}
}

func TestHeartbeat(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/hijack/hj-1/heartbeat", fakeResponse{status: 200, body: map[string]any{
		"ok": true, "lease_expires_at": 123.0,
	}})
	c := fs.client()
	data, err := c.Heartbeat(ctx(), "w1", "hj-1", 120)
	if err != nil {
		t.Fatal(err)
	}
	if data["ok"] != true {
		t.Fatalf("heartbeat: %v", data)
	}
	if fs.last().body["lease_s"].(float64) != 120 {
		t.Fatalf("lease_s: %v", fs.last().body)
	}
}

func TestHeartbeatDefaultLease(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/hijack/hj-1/heartbeat", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := fs.client()
	if _, err := c.Heartbeat(ctx(), "w1", "hj-1", 0); err != nil {
		t.Fatal(err)
	}
	if fs.last().body["lease_s"].(float64) != 90 {
		t.Fatalf("default lease_s: %v", fs.last().body)
	}
}

func TestSendWithGuards(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/hijack/hj-1/send", fakeResponse{status: 200, body: map[string]any{
		"ok": true, "sent": "hello\r",
	}})
	c := fs.client()
	data, err := c.Send(ctx(), "w1", "hj-1", SendOptions{
		Keys: "hello\r", ExpectPromptID: "p1", ExpectRegex: ".*ok.*",
	})
	if err != nil {
		t.Fatal(err)
	}
	if data["sent"] != "hello\r" {
		t.Fatalf("sent: %v", data)
	}
	b := fs.last().body
	if b["keys"] != "hello\r" || b["expect_prompt_id"] != "p1" || b["expect_regex"] != ".*ok.*" {
		t.Fatalf("send body: %v", b)
	}
	if b["timeout_ms"].(float64) != 2000 || b["poll_interval_ms"].(float64) != 120 {
		t.Fatalf("send defaults: %v", b)
	}
}

func TestSendOmitsUnsetGuards(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/hijack/hj-1/send", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := fs.client()
	if _, err := c.Send(ctx(), "w1", "hj-1", SendOptions{Keys: "x", TimeoutMS: 500, PollIntervalMS: 40}); err != nil {
		t.Fatal(err)
	}
	b := fs.last().body
	if _, ok := b["expect_prompt_id"]; ok {
		t.Fatalf("expect_prompt_id should be omitted: %v", b)
	}
	if _, ok := b["expect_regex"]; ok {
		t.Fatalf("expect_regex should be omitted: %v", b)
	}
	if b["timeout_ms"].(float64) != 500 || b["poll_interval_ms"].(float64) != 40 {
		t.Fatalf("custom timeouts: %v", b)
	}
}

func TestStepAndReleaseNoBody(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/hijack/hj-1/step", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	fs.on("POST", "/worker/w1/hijack/hj-1/release", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := fs.client()

	if _, err := c.Step(ctx(), "w1", "hj-1"); err != nil {
		t.Fatal(err)
	}
	if fs.last().rawBody != "" {
		t.Fatalf("step should send no body, got %q", fs.last().rawBody)
	}
	if _, err := c.Release(ctx(), "w1", "hj-1"); err != nil {
		t.Fatal(err)
	}
	if fs.last().rawBody != "" {
		t.Fatalf("release should send no body, got %q", fs.last().rawBody)
	}
}

func TestSnapshotQuery(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/worker/w1/hijack/hj-1/snapshot", fakeResponse{status: 200, body: map[string]any{"snapshot": "s"}})
	c := fs.client()

	if _, err := c.Snapshot(ctx(), "w1", "hj-1", 50); err != nil {
		t.Fatal(err)
	}
	if fs.last().rawQ != "wait_ms=50" {
		t.Fatalf("snapshot query: %q", fs.last().rawQ)
	}
	// default wait_ms
	if _, err := c.Snapshot(ctx(), "w1", "hj-1", 0); err != nil {
		t.Fatal(err)
	}
	if fs.last().rawQ != "wait_ms=1500" {
		t.Fatalf("default snapshot query: %q", fs.last().rawQ)
	}
}

func TestEventsQuery(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/worker/w1/hijack/hj-1/events", fakeResponse{status: 200, body: map[string]any{"events": []any{}}})
	c := fs.client()
	if _, err := c.Events(ctx(), "w1", "hj-1", EventsOptions{AfterSeq: 5, Limit: 10}); err != nil {
		t.Fatal(err)
	}
	q := fs.last().rawQ
	if !strings.Contains(q, "after_seq=5") || !strings.Contains(q, "limit=10") {
		t.Fatalf("events query: %q", q)
	}
	if _, err := c.Events(ctx(), "w1", "hj-1", EventsOptions{}); err != nil {
		t.Fatal(err)
	}
	q = fs.last().rawQ
	if !strings.Contains(q, "after_seq=0") || !strings.Contains(q, "limit=200") {
		t.Fatalf("default events query: %q", q)
	}
}

// -- worker control -------------------------------------------------------

func TestSetInputModeAndDisconnect(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/input_mode", fakeResponse{status: 200, body: map[string]any{"input_mode": "open"}})
	fs.on("POST", "/worker/w1/disconnect_worker", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := fs.client()

	data, err := c.SetInputMode(ctx(), "w1", "open")
	if err != nil {
		t.Fatal(err)
	}
	if data["input_mode"] != "open" {
		t.Fatalf("input_mode: %v", data)
	}
	if fs.last().body["input_mode"] != "open" {
		t.Fatalf("input_mode body: %v", fs.last().body)
	}
	if _, err := c.DisconnectWorker(ctx(), "w1"); err != nil {
		t.Fatal(err)
	}
}
