//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// TestSessionIDPathValidation covers the requireID 422 branches on patch/delete.
func TestSessionIDPathValidation(t *testing.T) {
	ts := newTestServer(t, nil)
	if rec := ts.do("PATCH", "/api/sessions/bad!id", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("patch bad id: %d", rec.Code)
	}
	if rec := ts.do("DELETE", "/api/sessions/bad!id", "", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("delete bad id: %d", rec.Code)
	}
}

// TestGetSessionStatusMissing covers handleGetSession's GetSession-error branch:
// the definition exists but no status is stored.
func TestGetSessionStatusMissing(t *testing.T) {
	ts := newTestServer(t, nil)
	owner := "admin1"
	// Definition present, status absent → GetDefinition ok, GetSession errors.
	ts.reg.mu.Lock()
	ts.reg.defs["nostatus"] = &serverconfig.SessionDefinition{SessionID: "nostatus", Owner: &owner, Visibility: "public", ConnectorType: "shell"}
	ts.reg.mu.Unlock()
	if rec := ts.do("GET", "/api/sessions/nostatus", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("get missing status: %d", rec.Code)
	}
}

// TestBulkDeleteFilterMisses covers the no-filter (nil floatField), state
// mismatch, and older_than-skip branches of bulk delete.
func TestBulkDeleteFilterMisses(t *testing.T) {
	ts := newTestServer(t, nil)
	// A running session (StoppedAt nil) that must be skipped by an older_than
	// filter and by a "stopped" state filter.
	ts.reg.add("running", "admin1", "public")
	// A recently-stopped session that must be skipped by a long older_than.
	ts.reg.add("recent", "admin1", "public")
	recent := ts.srv.clock.Wall() - 1
	ts.reg.statuses["recent"].LifecycleState = "stopped"
	ts.reg.statuses["recent"].StoppedAt = &recent

	// No filter key → floatField(nil) nil-map branch; nothing matches → 0.
	rec := ts.do("DELETE", "/api/sessions", `{}`, adminHeaders())
	if rec.Code != http.StatusOK || decode(t, rec.Body.Bytes())["deleted"] != float64(2) {
		t.Fatalf("no-filter bulk delete: %d %s", rec.Code, rec.Body.String())
	}

	// Re-populate and apply an older_than that the recent/running sessions miss.
	ts.reg.add("running", "admin1", "public")
	ts.reg.add("recent", "admin1", "public")
	ts.reg.statuses["recent"].LifecycleState = "stopped"
	ts.reg.statuses["recent"].StoppedAt = &recent
	rec = ts.do("DELETE", "/api/sessions", `{"filter":{"state":"stopped","older_than_s":100000}}`, adminHeaders())
	if rec.Code != http.StatusOK || decode(t, rec.Body.Bytes())["deleted"] != float64(0) {
		t.Fatalf("filtered bulk delete: %d %s", rec.Code, rec.Body.String())
	}
}

// TestFloatFieldIntForm covers the int case of floatField (only reachable with a
// Go-native int value, not JSON).
func TestFloatFieldIntForm(t *testing.T) {
	if v, ok := floatField(map[string]any{"n": 7}, "n"); !ok || v != 7 {
		t.Fatalf("floatField int: %v %v", v, ok)
	}
	if _, ok := floatField(nil, "n"); ok {
		t.Fatal("floatField nil map should be !ok")
	}
}

// TestRunBindError covers Run's net.Listen error return.
func TestRunBindError(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		// An un-bindable host makes net.Listen fail immediately.
		cfg.Server.Host = "256.256.256.256"
		cfg.Server.Port = 0
	})
	if err := ts.srv.Run(context.Background()); err == nil {
		t.Fatal("Run should return the listen error")
	}
}

// TestSSEStreamWithFilters covers the event_types + pattern query-param parsing
// branches of the SSE handler.
func TestSSEStreamWithFilters(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("f1", "admin1", "public")
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx,
		"GET", httpSrv.URL+"/api/sessions/f1/events/stream?event_types=snapshot,term&pattern=foo", http.NoBody)
	req.Header.Set("X-Subject", "admin1")
	req.Header.Set("X-Role", "admin")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	if resp.Header.Get("Content-Type") != "text/event-stream" {
		t.Fatalf("content-type: %q", resp.Header.Get("Content-Type"))
	}
	cancel()
	_ = resp.Body.Close()
}
