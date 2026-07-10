//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// decodeBody unmarshals a recorder body into a generic value.
func decodeBody(t *testing.T, body string) any {
	t.Helper()
	var v any
	if err := json.Unmarshal([]byte(body), &v); err != nil {
		t.Fatalf("decode body %q: %v", body, err)
	}
	return v
}

func TestFanoutCreateListSendDelete(t *testing.T) {
	ts := newTestServer(t, nil)
	adm := adminHeaders()

	// Create a group with two (unconnected) worker ids.
	rec := ts.do("POST", "/api/fanout/groups", `{"name":"test-group","worker_ids":["w1","w2"],"mode":"parallel","divergence_threshold":0.75,"quiesce_ms":20,"max_response_ms":50,"stop_on_first_error":true}`, adm)
	if rec.Code != 200 {
		t.Fatalf("create status = %d, body=%s", rec.Code, rec.Body.String())
	}
	created := decodeBody(t, rec.Body.String()).(map[string]any)
	if created["name"] != "test-group" || created["session_count"].(float64) != 2 {
		t.Fatalf("create body = %+v", created)
	}
	groupID := created["group_id"].(string)

	// List includes the new group.
	rec = ts.do("GET", "/api/fanout/groups", "", adm)
	if rec.Code != 200 {
		t.Fatalf("list status = %d", rec.Code)
	}
	list := decodeBody(t, rec.Body.String()).([]any)
	found := false
	for _, g := range list {
		if g.(map[string]any)["group_id"] == groupID {
			found = true
		}
	}
	if !found {
		t.Fatalf("group %s not in list %+v", groupID, list)
	}

	// Send: workers not connected → all fail.
	rec = ts.do("POST", "/api/fanout/groups/"+groupID+"/send", `{"data":"echo hello\n"}`, adm)
	if rec.Code != 200 {
		t.Fatalf("send status = %d, body=%s", rec.Code, rec.Body.String())
	}
	result := decodeBody(t, rec.Body.String()).(map[string]any)
	if result["group_id"] != groupID {
		t.Fatalf("send group_id = %v", result["group_id"])
	}
	results := result["results"].([]any)
	if len(results) != 2 {
		t.Fatalf("results len = %d", len(results))
	}
	for _, r := range results {
		if r.(map[string]any)["ok"].(bool) {
			t.Fatalf("expected all sessions failed: %+v", r)
		}
	}
	failed := result["failed_sessions"].([]any)
	if len(failed) != 2 {
		t.Fatalf("failed_sessions = %v", failed)
	}

	// Delete → 204, then gone from list.
	rec = ts.do("DELETE", "/api/fanout/groups/"+groupID, "", adm)
	if rec.Code != 204 {
		t.Fatalf("delete status = %d", rec.Code)
	}
	rec = ts.do("GET", "/api/fanout/groups", "", adm)
	for _, g := range decodeBody(t, rec.Body.String()).([]any) {
		if g.(map[string]any)["group_id"] == groupID {
			t.Fatal("group still present after delete")
		}
	}
}

func TestFanoutCreateExceedsMaxSize(t *testing.T) {
	ts := newTestServer(t, nil)
	ids := make([]string, 0, 60)
	for i := 0; i < 60; i++ {
		ids = append(ids, fmt.Sprintf(`"w%d"`, i))
	}
	body := `{"name":"big","worker_ids":[` + strings.Join(ids, ",") + `]}`
	rec := ts.do("POST", "/api/fanout/groups", body, adminHeaders())
	if rec.Code != 400 {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
	errMsg := decodeBody(t, rec.Body.String()).(map[string]any)["error"].(string)
	if !strings.Contains(strings.ToLower(errMsg), "exceeds max") {
		t.Fatalf("error = %q", errMsg)
	}
}

func TestFanoutCreateInvalidErrorPattern(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := ts.do("POST", "/api/fanout/groups",
		`{"name":"g","worker_ids":["w1"],"error_pattern":"(unterminated"}`, adminHeaders())
	if rec.Code != 400 {
		t.Fatalf("status = %d, want 400 (body=%s)", rec.Code, rec.Body.String())
	}
}

func TestFanoutGrant(t *testing.T) {
	ts := newTestServer(t, nil)
	adm := adminHeaders()
	rec := ts.do("POST", "/api/fanout/groups", `{"name":"grant-test","worker_ids":["w1"]}`, adm)
	groupID := decodeBody(t, rec.Body.String()).(map[string]any)["group_id"].(string)

	rec = ts.do("POST", "/api/fanout/groups/"+groupID+"/grants", `{"grantee":"other-user"}`, adm)
	if rec.Code != 204 {
		t.Fatalf("grant status = %d", rec.Code)
	}
}

func TestFanoutReadAuthz(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("priv1", "alice", "private")
	ts.reg.add("pub1", "alice", "public")

	// Viewer bob cannot read alice's private session → 403.
	rec := ts.do("POST", "/api/fanout/groups", `{"name":"g","worker_ids":["priv1"]}`, viewerHeaders())
	if rec.Code != 403 {
		t.Fatalf("private status = %d, want 403 (body=%s)", rec.Code, rec.Body.String())
	}
	errMsg := decodeBody(t, rec.Body.String()).(map[string]any)["error"].(string)
	if !strings.Contains(errMsg, "priv1") {
		t.Fatalf("error = %q", errMsg)
	}

	// Public session is readable → 200.
	rec = ts.do("POST", "/api/fanout/groups", `{"name":"g","worker_ids":["pub1"]}`, viewerHeaders())
	if rec.Code != 200 {
		t.Fatalf("public status = %d, want 200 (body=%s)", rec.Code, rec.Body.String())
	}
}

func TestFanoutRequiresAuth(t *testing.T) {
	ts := newTestServer(t, nil)
	// No X-Subject header → anonymous → 401.
	for _, tc := range []struct{ method, path string }{
		{"POST", "/api/fanout/groups"},
		{"GET", "/api/fanout/groups"},
		{"POST", "/api/fanout/groups/x/send"},
	} {
		rec := ts.do(tc.method, tc.path, "{}", nil)
		if rec.Code != 401 {
			t.Fatalf("%s %s status = %d, want 401", tc.method, tc.path, rec.Code)
		}
	}
}

func TestFanoutSendNotFound(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := ts.do("POST", "/api/fanout/groups/does-not-exist/send", `{"data":"x"}`, adminHeaders())
	if rec.Code != 404 {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	errMsg := decodeBody(t, rec.Body.String()).(map[string]any)["error"].(string)
	if !strings.Contains(strings.ToLower(errMsg), "not found") {
		t.Fatalf("error = %q", errMsg)
	}
}

func TestFanoutDeleteBranches(t *testing.T) {
	ts := newTestServer(t, nil)
	adm := adminHeaders()

	// Missing group → 404.
	if rec := ts.do("DELETE", "/api/fanout/groups/nope", "", adm); rec.Code != 404 {
		t.Fatalf("delete missing status = %d, want 404", rec.Code)
	}

	// Non-creator (but grantee) → 403.
	rec := ts.do("POST", "/api/fanout/groups", `{"name":"g","worker_ids":["w1"]}`, adm)
	groupID := decodeBody(t, rec.Body.String()).(map[string]any)["group_id"].(string)
	// Grant view1 so GetGroup resolves for it, but it is not the creator.
	if rec := ts.do("POST", "/api/fanout/groups/"+groupID+"/grants", `{"grantee":"view1"}`, adm); rec.Code != 204 {
		t.Fatalf("grant status = %d", rec.Code)
	}
	if rec := ts.do("DELETE", "/api/fanout/groups/"+groupID, "", viewerHeaders()); rec.Code != 403 {
		t.Fatalf("delete by grantee status = %d, want 403 (body=%s)", rec.Code, rec.Body.String())
	}
}

func TestFanoutGrantBranches(t *testing.T) {
	ts := newTestServer(t, nil)
	adm := adminHeaders()

	// Missing group → 404.
	if rec := ts.do("POST", "/api/fanout/groups/nope/grants", `{"grantee":"x"}`, adm); rec.Code != 404 {
		t.Fatalf("grant missing status = %d, want 404", rec.Code)
	}

	// Non-creator grantee cannot grant further → 403.
	rec := ts.do("POST", "/api/fanout/groups", `{"name":"g","worker_ids":["w1"]}`, adm)
	groupID := decodeBody(t, rec.Body.String()).(map[string]any)["group_id"].(string)
	if rec := ts.do("POST", "/api/fanout/groups/"+groupID+"/grants", `{"grantee":"view1"}`, adm); rec.Code != 204 {
		t.Fatalf("first grant status = %d", rec.Code)
	}
	if rec := ts.do("POST", "/api/fanout/groups/"+groupID+"/grants", `{"grantee":"carol"}`, viewerHeaders()); rec.Code != 403 {
		t.Fatalf("grant by grantee status = %d, want 403 (body=%s)", rec.Code, rec.Body.String())
	}
}
