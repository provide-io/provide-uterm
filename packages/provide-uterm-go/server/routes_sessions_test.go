//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"net/http"
	"testing"
)

func operatorHeaders() map[string]string {
	return map[string]string{"X-Subject": "op1", "X-Role": "operator"}
}

func decodeArray(t *testing.T, body []byte) []any {
	t.Helper()
	var a []any
	if err := json.Unmarshal(body, &a); err != nil {
		t.Fatalf("decode array %q: %v", string(body), err)
	}
	return a
}

func TestSessionListAndRead(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("pub", "admin1", "public")
	ts.reg.add("priv", "admin1", "private")

	// Admin sees both.
	rec := ts.do("GET", "/api/sessions", "", adminHeaders())
	if rec.Code != http.StatusOK || len(decodeArray(t, rec.Body.Bytes())) != 2 {
		t.Fatalf("admin list: %d %s", rec.Code, rec.Body.String())
	}
	// Viewer sees only the public one.
	rec = ts.do("GET", "/api/sessions", "", viewerHeaders())
	if got := decodeArray(t, rec.Body.Bytes()); len(got) != 1 {
		t.Fatalf("viewer list: %s", rec.Body.String())
	}
	// Filter by connector_type.
	rec = ts.do("GET", "/api/sessions?connector_type=nope", "", adminHeaders())
	if len(decodeArray(t, rec.Body.Bytes())) != 0 {
		t.Fatalf("connector filter: %s", rec.Body.String())
	}

	// Get: public readable by viewer, private not.
	if rec := ts.do("GET", "/api/sessions/pub", "", viewerHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("viewer read pub: %d", rec.Code)
	}
	if rec := ts.do("GET", "/api/sessions/priv", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer read priv: %d", rec.Code)
	}
	if rec := ts.do("GET", "/api/sessions/ghost", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("unknown: %d", rec.Code)
	}
	if rec := ts.do("GET", "/api/sessions/bad!id", "", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("invalid id: %d", rec.Code)
	}
}

func TestSessionCreate(t *testing.T) {
	ts := newTestServer(t, nil)
	// Viewer cannot create.
	if rec := ts.do("POST", "/api/sessions", `{}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer create: %d", rec.Code)
	}
	// Admin create ok.
	rec := ts.do("POST", "/api/sessions", `{"session_id":"s1","connector_type":"shell"}`, adminHeaders())
	if rec.Code != http.StatusOK || decode(t, rec.Body.Bytes())["session_id"] != "s1" {
		t.Fatalf("admin create: %d %s", rec.Code, rec.Body.String())
	}
	// Operator owner scoping: mismatched owner rejected.
	rec = ts.do("POST", "/api/sessions", `{"owner":"someoneelse"}`, operatorHeaders())
	if rec.Code != http.StatusForbidden {
		t.Fatalf("operator owner mismatch: %d %s", rec.Code, rec.Body.String())
	}
	// Operator owner=self ok.
	if rec := ts.do("POST", "/api/sessions", `{"owner":"op1","session_id":"s2"}`, operatorHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("operator self create: %d %s", rec.Code, rec.Body.String())
	}
	// Validation + conflict branches.
	ts.reg.createErr = &SessionValidationError{Msg: "bad"}
	if rec := ts.do("POST", "/api/sessions", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("validation: %d", rec.Code)
	}
	ts.reg.createErr = &SessionConflictError{Msg: "dup"}
	if rec := ts.do("POST", "/api/sessions", `{}`, adminHeaders()); rec.Code != http.StatusConflict {
		t.Fatalf("conflict: %d", rec.Code)
	}
}

func TestSessionMutations(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")

	// Patch ok + validation + unknown.
	if rec := ts.do("PATCH", "/api/sessions/s1", `{"display_name":"x"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("patch: %d", rec.Code)
	}
	if rec := ts.do("PATCH", "/api/sessions/ghost", `{}`, adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("patch unknown: %d", rec.Code)
	}
	ts.reg.updateErr = &SessionValidationError{Msg: "bad"}
	if rec := ts.do("PATCH", "/api/sessions/s1", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("patch validation: %d", rec.Code)
	}
	ts.reg.updateErr = nil

	// Control routes.
	for _, path := range []string{"/connect", "/disconnect", "/restart", "/clear"} {
		if rec := ts.do("POST", "/api/sessions/s1"+path, "", adminHeaders()); rec.Code != http.StatusOK {
			t.Fatalf("%s: %d", path, rec.Code)
		}
	}
	// Mode ok + invalid.
	if rec := ts.do("POST", "/api/sessions/s1/mode", `{"input_mode":"open"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("mode: %d", rec.Code)
	}
	if rec := ts.do("POST", "/api/sessions/s1/mode", `{"input_mode":"bogus"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("mode invalid: %d", rec.Code)
	}
	// Delete.
	rec := ts.do("DELETE", "/api/sessions/s1", "", adminHeaders())
	if rec.Code != http.StatusOK || decode(t, rec.Body.Bytes())["ok"] != true {
		t.Fatalf("delete: %d %s", rec.Code, rec.Body.String())
	}
}

func TestSessionReadOps(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")
	ts.reg.analysis = map[string]any{"foo": "bar"}
	ts.reg.snapshot = map[string]any{"screen": "hi"}
	ts.reg.events = []map[string]any{{"seq": 1.0}}
	ts.reg.watch = map[string]any{"events": []any{}}

	for _, path := range []string{
		"/api/sessions/s1/snapshot",
		"/api/sessions/s1/events",
		"/api/sessions/s1/events/watch",
	} {
		rec := ts.do("GET", path, "", adminHeaders())
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: %d %s", path, rec.Code, rec.Body.String())
		}
	}
	// Analyze (POST) success + unknown session → 404.
	if rec := ts.do("POST", "/api/sessions/s1/analyze", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("analyze: %d", rec.Code)
	}
	if rec := ts.do("POST", "/api/sessions/ghost/analyze", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("analyze unknown: %d", rec.Code)
	}
}

func TestSessionAnnotate(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")
	// Success.
	rec := ts.do("POST", "/api/sessions/s1/annotate", `{"label":"note","severity":"warning"}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("annotate: %d %s", rec.Code, rec.Body.String())
	}
	body := decode(t, rec.Body.Bytes())
	if body["seq"] != float64(7) {
		t.Fatalf("annotate seq: %v", body)
	}
	// Missing label → 400.
	if rec := ts.do("POST", "/api/sessions/s1/annotate", `{}`, adminHeaders()); rec.Code != http.StatusBadRequest {
		t.Fatalf("annotate no label: %d", rec.Code)
	}
	// Bad severity → 400.
	if rec := ts.do("POST", "/api/sessions/s1/annotate", `{"label":"x","severity":"nope"}`, adminHeaders()); rec.Code != http.StatusBadRequest {
		t.Fatalf("annotate bad severity: %d", rec.Code)
	}
	// No runtime → 404.
	ts.reg.annotateErr = ErrNoRuntime
	if rec := ts.do("POST", "/api/sessions/s1/annotate", `{"label":"x"}`, adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("annotate no runtime: %d", rec.Code)
	}
}

func TestBulkDelete(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")
	stopped := 100.0
	ts.reg.statuses["s1"].LifecycleState = "stopped"
	ts.reg.statuses["s1"].StoppedAt = &stopped

	// Viewer forbidden.
	if rec := ts.do("DELETE", "/api/sessions", `{}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer bulk: %d", rec.Code)
	}
	// Admin delete matching state.
	rec := ts.do("DELETE", "/api/sessions", `{"filter":{"state":"stopped"}}`, adminHeaders())
	if rec.Code != http.StatusOK || decode(t, rec.Body.Bytes())["deleted"] != float64(1) {
		t.Fatalf("bulk delete: %d %s", rec.Code, rec.Body.String())
	}
}
