//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/fanout"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestFanoutCreateOversizedGroupIsA400WithTheRefusal(t *testing.T) {
	ts := permissiveFanoutTestServer(t)
	ids := make([]string, 0, 51)
	for i := 0; i < 51; i++ {
		ids = append(ids, fmt.Sprintf(`"w%d"`, i))
	}
	rec := ts.do("POST", "/api/fanout/groups", `{"name":"big","worker_ids":[`+strings.Join(ids, ",")+`]}`, adminHeaders())
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (body=%s)", rec.Code, rec.Body.String())
	}
	got := decodeBody(t, rec.Body.String()).(map[string]any)
	if len(got) != 1 || got["error"] != "Group size 51 exceeds max 50" {
		t.Fatalf("body = %s", rec.Body.String())
	}
}

func TestFanoutCreateGroupErrorEchoesOnlyTheRefusal(t *testing.T) {
	var logs bytes.Buffer
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Logger = slog.New(slog.NewTextHandler(&logs, nil))
	})

	// A refusal the controller wrote for the caller keeps its message.
	_, refusal := fanout.NewController(nil, fanout.Config{MaxGroupSize: 1}).
		CreateGroup(&fanout.Group{WorkerIDs: []string{"w1", "w2"}}, "admin")
	rec := httptest.NewRecorder()
	ts.srv.writeCreateGroupError(rec, fmt.Errorf("wrapped: %w", refusal))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("refusal status = %d", rec.Code)
	}
	if got := decodeBody(t, rec.Body.String()).(map[string]any); got["error"] != "Group size 2 exceeds max 1" {
		t.Fatalf("refusal body = %s", rec.Body.String())
	}

	// Anything else is a server fault: a generic 500, detail only in the log.
	rec = httptest.NewRecorder()
	ts.srv.writeCreateGroupError(rec, errors.New("store write failed: postgres://db.internal/fanout"))
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("fault status = %d", rec.Code)
	}
	if body := strings.TrimSpace(rec.Body.String()); body != "Internal Server Error" {
		t.Fatalf("fault body = %q", body)
	}
	for _, leaked := range []string{"postgres", "db.internal", "store write"} {
		if strings.Contains(rec.Body.String(), leaked) {
			t.Fatalf("fault body leaks %q: %s", leaked, rec.Body.String())
		}
	}
	if !strings.Contains(logs.String(), "fanout_create_group_failed") || !strings.Contains(logs.String(), "db.internal") {
		t.Fatalf("fault not logged: %s", logs.String())
	}
}
