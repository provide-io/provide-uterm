//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"testing"
)

// TestHijackWriteRoutesRefuseViewer proves each REST hijack write route runs its
// own capability gate: a viewer holds no session.control.hijack, so send, step,
// heartbeat, and release are all refused before touching the lease.
func TestHijackWriteRoutesRefuseViewer(t *testing.T) {
	ts := newTestServer(t, nil)
	hid := acquireHijack(t, ts, "w1")
	base := "/worker/w1/hijack/" + hid
	cases := []struct {
		name, method, path, body string
	}{
		{"send", "POST", base + "/send", `{"keys":"ls\n"}`},
		{"step", "POST", base + "/step", `{}`},
		{"heartbeat", "POST", base + "/heartbeat", `{"lease_s":60}`},
		{"release", "POST", base + "/release", `{}`},
	}
	for _, c := range cases {
		rec := ts.do(c.method, c.path, c.body, viewerHeaders())
		if rec.Code != http.StatusForbidden {
			t.Fatalf("%s: want 403, got %d %s", c.name, rec.Code, rec.Body.String())
		}
	}
	// The lease survives every refusal.
	if rec := ts.do("POST", base+"/heartbeat", `{"lease_s":60}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("owner heartbeat after refusals: %d %s", rec.Code, rec.Body.String())
	}
}

// TestHijackReleaseRefusesNonOwner covers the lease-ownership gate, which is
// distinct from the capability gate: an operator with the hijack capability but
// without the lease (and without ownership of the session) cannot release it.
func TestHijackReleaseRefusesNonOwner(t *testing.T) {
	ts := newTestServer(t, nil)
	hid := acquireHijack(t, ts, "w1")
	other := map[string]string{"X-Subject": "op2", "X-Role": "operator"}

	rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/release", `{}`, other)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("non-owner release: want 403, got %d %s", rec.Code, rec.Body.String())
	}
	// The acquiring principal still holds it.
	if rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/release", `{}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("owner release: %d %s", rec.Code, rec.Body.String())
	}
}

// TestGUIAttachRefusesUnscopedTenant covers the tenant-scope gate on GUI attach:
// a principal with no tenant claim cannot reach any target registry scope.
func TestGUIAttachRefusesUnscopedTenant(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")

	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, adminHeaders())
	if rec.Code != http.StatusForbidden {
		t.Fatalf("attach without a tenant: want 403, got %d %s", rec.Code, rec.Body.String())
	}
}

// TestGUIAttachReusesSessionManager proves a re-attach replaces the console
// behind the existing manager rather than orphaning it, so the hijack routes
// keep resolving through one manager for the worker's lifetime.
func TestGUIAttachReusesSessionManager(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	hdr := tenantHeaders("admin", "acme")

	if rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, hdr); rec.Code != http.StatusOK {
		t.Fatalf("first attach: %d %s", rec.Code, rec.Body.String())
	}
	st, err := ts.hub.Registry.Require("w1")
	if err != nil {
		t.Fatalf("require worker: %v", err)
	}
	first := st.GraphicalSession

	if rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, hdr); rec.Code != http.StatusOK {
		t.Fatalf("second attach: %d %s", rec.Code, rec.Body.String())
	}
	st, err = ts.hub.Registry.Require("w1")
	if err != nil {
		t.Fatalf("require worker after re-attach: %v", err)
	}
	if st.GraphicalSession != first {
		t.Fatal("re-attach replaced the graphical session manager instead of reusing it")
	}
}
