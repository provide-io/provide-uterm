//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"testing"
)

// otherAdminHeaders is a second global admin, distinct from adminHeaders().
func otherAdminHeaders() map[string]string {
	return map[string]string{"X-Subject": "admin2", "X-Role": "admin"}
}

// createFanoutGroup creates a group owned by adminHeaders() and grants admin2
// access to it, returning the group id. A grantee can see the group but is not
// its creator, which is exactly the boundary the delete/grant gates enforce.
func createFanoutGroup(t *testing.T, ts *testServer) string {
	t.Helper()
	rec := ts.do("POST", "/api/fanout/groups",
		`{"name":"g","worker_ids":["w1"],"mode":"parallel"}`, adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("create group: %d %s", rec.Code, rec.Body.String())
	}
	groupID, _ := decode(t, rec.Body.Bytes())["group_id"].(string)
	if groupID == "" {
		t.Fatalf("no group id: %s", rec.Body.String())
	}
	rec = ts.do("POST", "/api/fanout/groups/"+groupID+"/grants",
		`{"grantee":"admin2"}`, adminHeaders())
	if rec.Code != http.StatusOK && rec.Code != http.StatusNoContent {
		t.Fatalf("grant access: %d %s", rec.Code, rec.Body.String())
	}
	return groupID
}

// TestFanoutDeleteIsCreatorOnly proves access to a group is not authority over
// it: a grantee — even a global admin — can reach the group but cannot delete
// somebody else's.
func TestFanoutDeleteIsCreatorOnly(t *testing.T) {
	ts := permissiveFanoutTestServer(t)
	groupID := createFanoutGroup(t, ts)

	rec := ts.do("DELETE", "/api/fanout/groups/"+groupID, "", otherAdminHeaders())
	if rec.Code != http.StatusForbidden {
		t.Fatalf("grantee delete: want 403, got %d %s", rec.Code, rec.Body.String())
	}
	// The group survived, and its creator can still delete it.
	rec = ts.do("DELETE", "/api/fanout/groups/"+groupID, "", adminHeaders())
	if rec.Code != http.StatusNoContent {
		t.Fatalf("creator delete: %d %s", rec.Code, rec.Body.String())
	}
}

// TestFanoutGrantIsCreatorOnly proves a grantee cannot widen the group's access
// list — otherwise one grant would transitively hand out every other grant.
func TestFanoutGrantIsCreatorOnly(t *testing.T) {
	ts := permissiveFanoutTestServer(t)
	groupID := createFanoutGroup(t, ts)

	rec := ts.do("POST", "/api/fanout/groups/"+groupID+"/grants",
		`{"grantee":"admin3"}`, otherAdminHeaders())
	if rec.Code != http.StatusForbidden {
		t.Fatalf("grantee re-grant: want 403, got %d %s", rec.Code, rec.Body.String())
	}
	// admin3 never gained access, so the group is invisible to it.
	rec = ts.do("DELETE", "/api/fanout/groups/"+groupID, "",
		map[string]string{"X-Subject": "admin3", "X-Role": "admin"})
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unrelated admin delete: want 404, got %d %s", rec.Code, rec.Body.String())
	}
}
