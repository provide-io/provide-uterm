//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bufio"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// sseStreamReq opens an SSE stream against a real http server and returns the
// response. The caller must close resp.Body.
func sseStreamReq(t *testing.T, base, path string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest("GET", base+path, http.NoBody)
	req.Header.Set("X-Subject", "admin1")
	req.Header.Set("X-Role", "admin")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("stream request: %v", err)
	}
	return resp
}

// TestSSEStreamDeliversEvent covers the queue-event delivery + sseWrite success
// path: an enqueued event is rendered as a `data:` SSE frame.
func TestSSEStreamDeliversEvent(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("f1", "admin1", "public")
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()

	resp := sseStreamReq(t, httpSrv.URL, "/api/sessions/f1/events/stream")
	defer func() { _ = resp.Body.Close() }()

	bus := ts.hub.EventBus()
	// Keep enqueuing until the reader observes the event (subscription races
	// with our first enqueue).
	stop := make(chan struct{})
	go func() {
		tk := time.NewTicker(20 * time.Millisecond)
		defer tk.Stop()
		for {
			select {
			case <-stop:
				return
			case <-tk.C:
				bus.Enqueue("f1", map[string]any{"type": "term", "data": map[string]any{"data": "hello-sse"}})
			}
		}
	}()

	sc := bufio.NewScanner(resp.Body)
	deadline := time.Now().Add(3 * time.Second)
	found := false
	for time.Now().Before(deadline) && sc.Scan() {
		if strings.Contains(sc.Text(), "hello-sse") {
			found = true
			break
		}
	}
	close(stop)
	if !found {
		t.Fatal("expected an SSE data frame carrying the enqueued event")
	}
}

// TestSSEStreamWorkerDisconnect covers the closed-queue / nil-sentinel branch:
// CloseWorker emits a worker_disconnected SSE frame and ends the stream.
func TestSSEStreamWorkerDisconnect(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("f2", "admin1", "public")
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()

	resp := sseStreamReq(t, httpSrv.URL, "/api/sessions/f2/events/stream")
	defer func() { _ = resp.Body.Close() }()

	go func() {
		time.Sleep(120 * time.Millisecond)
		ts.hub.EventBus().CloseWorker("f2")
	}()

	sc := bufio.NewScanner(resp.Body)
	deadline := time.Now().Add(3 * time.Second)
	found := false
	for time.Now().Before(deadline) && sc.Scan() {
		if strings.Contains(sc.Text(), "worker_disconnected") {
			found = true
			break
		}
	}
	if !found {
		t.Fatal("expected a worker_disconnected SSE frame")
	}
}
