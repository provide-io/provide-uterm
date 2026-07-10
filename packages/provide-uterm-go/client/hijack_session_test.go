//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

// -- session API ----------------------------------------------------------

func TestSessionAPIRoutes(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/health", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	fs.on("GET", "/api/sessions", fakeResponse{status: 200, body: []any{map[string]any{"session_id": "s1"}}})
	fs.on("GET", "/api/sessions/s1", fakeResponse{status: 200, body: map[string]any{"session_id": "s1"}})
	fs.on("GET", "/api/sessions/s1/snapshot", fakeResponse{status: 200, body: map[string]any{"snapshot": "x"}})
	fs.on("GET", "/api/sessions/s1/events", fakeResponse{status: 200, body: []any{}})
	fs.on("POST", "/api/sessions/s1/mode", fakeResponse{status: 200, body: map[string]any{"input_mode": "open"}})
	fs.on("POST", "/api/sessions/s1/connect", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	fs.on("POST", "/api/sessions/s1/disconnect", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	fs.on("POST", "/api/connect", fakeResponse{status: 200, body: map[string]any{"session_id": "eph"}})
	c := fs.client()

	if h, err := c.Health(ctx()); err != nil || h["ok"] != true {
		t.Fatalf("health: %v %v", h, err)
	}
	list, err := c.ListSessions(ctx())
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := list.([]any); !ok {
		t.Fatalf("list not array: %T", list)
	}
	if s, err := c.GetSession(ctx(), "s1"); err != nil || s["session_id"] != "s1" {
		t.Fatalf("get_session: %v %v", s, err)
	}
	if _, err := c.SessionSnapshot(ctx(), "s1"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.SessionEvents(ctx(), "s1", 5); err != nil {
		t.Fatal(err)
	}
	if fs.last().rawQ != "limit=5" {
		t.Fatalf("session events limit: %q", fs.last().rawQ)
	}
	if m, err := c.SetSessionMode(ctx(), "s1", "open"); err != nil || m["input_mode"] != "open" {
		t.Fatalf("set mode: %v %v", m, err)
	}
	if _, err := c.ConnectSession(ctx(), "s1"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.DisconnectSession(ctx(), "s1"); err != nil {
		t.Fatal(err)
	}
}

func TestSessionEventsDefaultLimit(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/sessions/s1/events", fakeResponse{status: 200, body: []any{}})
	c := fs.client()
	if _, err := c.SessionEvents(ctx(), "s1", 0); err != nil {
		t.Fatal(err)
	}
	if fs.last().rawQ != "limit=100" {
		t.Fatalf("default session events limit: %q", fs.last().rawQ)
	}
}

func TestQuickConnect(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/api/connect", fakeResponse{status: 200, body: map[string]any{"session_id": "eph"}})
	c := fs.client()

	data, err := c.QuickConnect(ctx(), "shell", QuickConnectOptions{
		DisplayName: "Ephemeral", Config: map[string]any{"host": "h", "port": 23},
	})
	if err != nil || data["session_id"] != "eph" {
		t.Fatalf("quick_connect: %v %v", data, err)
	}
	b := fs.last().body
	if b["connector_type"] != "shell" || b["display_name"] != "Ephemeral" || b["host"] != "h" {
		t.Fatalf("quick_connect body: %v", b)
	}
	// without display name
	if _, err := c.QuickConnect(ctx(), "shell", QuickConnectOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, ok := fs.last().body["display_name"]; ok {
		t.Fatalf("display_name should be omitted: %v", fs.last().body)
	}
}

func TestWatchSessionEvents(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/sessions/s1/events/watch", fakeResponse{status: 200, body: []any{}})
	c := fs.client()

	if _, err := c.WatchSessionEvents(ctx(), "s1", WatchOptions{
		EventTypes: "output,error", Pattern: "ERR.*", TimeoutMS: 100, MaxEvents: 5,
	}); err != nil {
		t.Fatal(err)
	}
	q := fs.last().rawQ
	for _, want := range []string{"timeout_ms=100", "max_events=5", "event_types=output%2Cerror", "pattern=ERR"} {
		if !strings.Contains(q, want) {
			t.Fatalf("watch query %q missing %q", q, want)
		}
	}
	// defaults + omitted optional params
	if _, err := c.WatchSessionEvents(ctx(), "s1", WatchOptions{}); err != nil {
		t.Fatal(err)
	}
	q = fs.last().rawQ
	if !strings.Contains(q, "timeout_ms=5000") || !strings.Contains(q, "max_events=50") {
		t.Fatalf("watch defaults: %q", q)
	}
	if strings.Contains(q, "event_types") || strings.Contains(q, "pattern") {
		t.Fatalf("watch should omit optional params: %q", q)
	}
}

func TestPostGeneric(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/api/custom", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := fs.client()
	if _, err := c.Post(ctx(), "/api/custom", map[string]any{"a": 1}); err != nil {
		t.Fatal(err)
	}
	if fs.last().body["a"].(float64) != 1 {
		t.Fatalf("post body: %v", fs.last().body)
	}
}

// -- headers / prefix -----------------------------------------------------

func TestHeadersForwarded(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/hijack/acquire", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := fs.client(WithHeaders(map[string]string{"X-Custom": "val", "Authorization": "Bearer t"}))
	if _, err := c.Acquire(ctx(), "w1", AcquireOptions{}); err != nil {
		t.Fatal(err)
	}
	h := fs.last().headers
	if h.Get("X-Custom") != "val" || h.Get("Authorization") != "Bearer t" {
		t.Fatalf("headers not forwarded: %v", h)
	}
}

func TestCustomEntityPrefix(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/agent/w1/hijack/acquire", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := fs.client(WithEntityPrefix("/agent/"))
	if _, err := c.Acquire(ctx(), "w1", AcquireOptions{}); err != nil {
		t.Fatal(err)
	}
	if fs.last().path != "/agent/w1/hijack/acquire" {
		t.Fatalf("prefix path: %s", fs.last().path)
	}
}

func TestBaseURLTrailingSlashStripped(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/health", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := NewHijackClient(fs.srv.URL+"/", WithHTTPClient(&http.Client{}))
	if _, err := c.Health(ctx()); err != nil {
		t.Fatal(err)
	}
}

// -- error branches -------------------------------------------------------

func TestErrorStatusMapping(t *testing.T) {
	cases := []struct {
		name   string
		status int
		check  func(*APIError) bool
	}{
		{"badrequest", 400, (*APIError).IsBadRequest},
		{"forbidden", 403, (*APIError).IsForbidden},
		{"notfound", 404, (*APIError).IsNotFound},
		{"conflict", 409, (*APIError).IsConflict},
		{"ratelimited", 429, (*APIError).IsRateLimited},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fs := newFakeServer(t)
			fs.on("POST", "/worker/w1/hijack/acquire", fakeResponse{
				status: tc.status, body: map[string]any{"error": "boom"},
			})
			c := fs.client()
			_, err := c.Acquire(ctx(), "w1", AcquireOptions{})
			if err == nil {
				t.Fatal("expected error")
			}
			apiErr, ok := err.(*APIError)
			if !ok {
				t.Fatalf("not *APIError: %T", err)
			}
			if apiErr.StatusCode != tc.status || !tc.check(apiErr) {
				t.Fatalf("status %d predicate mismatch", apiErr.StatusCode)
			}
			if apiErr.Message != "boom" {
				t.Fatalf("message: %q", apiErr.Message)
			}
			body, _ := apiErr.Body.(map[string]any)
			if body["error"] != "boom" {
				t.Fatalf("body preserved: %v", apiErr.Body)
			}
			if !strings.Contains(apiErr.Error(), "boom") {
				t.Fatalf("Error(): %q", apiErr.Error())
			}
		})
	}
}

func TestErrorNonJSONBody(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("POST", "/worker/w1/hijack/hj-1/step", fakeResponse{status: 500, raw: "boom-text"})
	c := fs.client()
	_, err := c.Step(ctx(), "w1", "hj-1")
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("not *APIError: %T", err)
	}
	body, _ := apiErr.Body.(map[string]any)
	if body["raw"] != "boom-text" {
		t.Fatalf("raw fallback: %v", apiErr.Body)
	}
	if !strings.Contains(apiErr.Message, "boom-text") {
		t.Fatalf("message: %q", apiErr.Message)
	}
}

func TestNonJSONSuccessReturnsRaw(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/health", fakeResponse{status: 200, raw: "OK"})
	c := fs.client()
	data, err := c.Health(ctx())
	if err != nil {
		t.Fatal(err)
	}
	if data["raw"] != "OK" {
		t.Fatalf("raw success: %v", data)
	}
}

func TestTransportError(t *testing.T) {
	// Point at a closed port.
	c := NewHijackClient("http://127.0.0.1:1")
	_, err := c.Acquire(ctx(), "w1", AcquireOptions{})
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("not *APIError: %T", err)
	}
	if !apiErr.Transport || apiErr.StatusCode != 0 {
		t.Fatalf("expected transport error, got %+v", apiErr)
	}
	body, _ := apiErr.Body.(map[string]any)
	if _, ok := body["error"]; !ok {
		t.Fatalf("transport body missing error: %v", apiErr.Body)
	}
	if !strings.Contains(apiErr.Error(), "transport error") {
		t.Fatalf("Error(): %q", apiErr.Error())
	}
}

func TestWatchCustomTimeoutApplied(t *testing.T) {
	// Slow handler ensures the derived request timeout (timeout_ms + 5s) is in
	// force; with timeout_ms=100 the deadline is ~5.1s, well above the 20ms
	// handler delay, so the call must succeed.
	fs := newFakeServer(t)
	fs.srv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(20 * time.Millisecond)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte("[]"))
	})
	c := fs.client()
	if _, err := c.WatchSessionEvents(ctx(), "s1", WatchOptions{TimeoutMS: 100}); err != nil {
		t.Fatalf("watch with custom timeout: %v", err)
	}
}

// -- concurrency ----------------------------------------------------------

func TestConcurrentRequests(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/health", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	c := fs.client()
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := c.Health(ctx()); err != nil {
				t.Errorf("concurrent health: %v", err)
			}
		}()
	}
	wg.Wait()
}
