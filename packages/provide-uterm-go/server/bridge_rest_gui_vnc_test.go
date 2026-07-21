//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net/http"
	"testing"

	"google.golang.org/grpc"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc"
)

// vncPath builds GET /worker/{wid}/hijack/{hid}/gui/vnc?target_id=...
func vncPath(workerID, hijackID, targetID string) string {
	p := "/worker/" + workerID + "/hijack/" + hijackID + "/gui/vnc"
	if targetID != "" {
		p += "?target_id=" + targetID
	}
	return p
}

// humanVncServer seeds a graphical target, acquires a hijack as acme-admin (u1),
// and returns the server + hijack id.
func humanVncServer(t *testing.T, targets ...*graphical.Definition) (*testServer, string) {
	t.Helper()
	ts := attachTestServer(t, targets...)
	ts.setupWorker(t, "w1")
	hdr := tenantHeaders("admin", "acme") // X-Subject=u1
	rec := ts.do("POST", "/worker/w1/hijack/acquire", `{"owner":"vnc","lease_s":120}`, hdr)
	if rec.Code != http.StatusOK {
		t.Fatalf("acquire: %d %s", rec.Code, rec.Body.String())
	}
	hid, _ := decode(t, rec.Body.Bytes())["hijack_id"].(string)
	if hid == "" {
		t.Fatalf("no hijack id")
	}
	return ts, hid
}

func litevirtTarget(id, tenant, endpoint string) *graphical.Definition {
	return &graphical.Definition{
		TargetID: id, TenantID: tenant, Protocol: graphical.ProtocolLitevirt,
		Endpoint: strPtrLocal(endpoint), Width: 64, Height: 48,
		Config: map[string]any{"vm_name": "vm1", "insecure_no_tls": true},
	}
}

func TestHumanVncUnauthenticated401(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	ghost := "00000000-0000-4000-8000-000000000001"
	rec := ts.do("GET", vncPath("w1", ghost, "gt-mem"), "", nil)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unauth = %d %s", rec.Code, rec.Body.String())
	}
}

func TestHumanVncWrongRole403(t *testing.T) {
	// Viewer lacks session.control.hijack.
	ts, hid := humanVncServer(t, memoryTarget("gt-mem", "acme"))
	rec := ts.do("GET", vncPath("w1", hid, "gt-mem"), "", tenantHeaders("viewer", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("viewer = %d %s", rec.Code, rec.Body.String())
	}
	// Operator also lacks session.control.hijack (admin-only capability).
	rec = ts.do("GET", vncPath("w1", hid, "gt-mem"), "", tenantHeaders("operator", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("operator = %d %s", rec.Code, rec.Body.String())
	}
}

func TestHumanVncMissingHijack404(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	ghost := "00000000-0000-4000-8000-000000000099"
	rec := ts.do("GET", vncPath("w1", ghost, "gt-mem"), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("missing hijack = %d %s", rec.Code, rec.Body.String())
	}
}

func TestHumanVncNonOwner403(t *testing.T) {
	ts, hid := humanVncServer(t, memoryTarget("gt-mem", "acme"))
	// Different admin subject — must not open the human relay for another's lease.
	other := tenantHeaders("admin", "acme")
	other["X-Subject"] = "other-admin"
	rec := ts.do("GET", vncPath("w1", hid, "gt-mem"), "", other)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("non-owner = %d %s", rec.Code, rec.Body.String())
	}
}

func TestHumanVncMemoryTarget501(t *testing.T) {
	ts, hid := humanVncServer(t, memoryTarget("gt-mem", "acme"))
	rec := ts.do("GET", vncPath("w1", hid, "gt-mem"), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("memory target = %d %s", rec.Code, rec.Body.String())
	}
}

func TestHumanVncRfbTarget501(t *testing.T) {
	rfb := &graphical.Definition{
		TargetID: "gt-rfb", TenantID: "acme", Protocol: graphical.ProtocolRfb,
		Endpoint: strPtrLocal("127.0.0.1:5900"), Width: 64, Height: 48,
	}
	ts, hid := humanVncServer(t, rfb)
	rec := ts.do("GET", vncPath("w1", hid, "gt-rfb"), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("rfb target = %d %s", rec.Code, rec.Body.String())
	}
}

func TestHumanVncMissingTargetID422(t *testing.T) {
	ts, hid := humanVncServer(t, memoryTarget("gt-mem", "acme"))
	rec := ts.do("GET", vncPath("w1", hid, ""), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("missing target_id = %d %s", rec.Code, rec.Body.String())
	}
}

func TestHumanVncUnknownTarget404(t *testing.T) {
	ts, hid := humanVncServer(t, memoryTarget("gt-mem", "acme"))
	rec := ts.do("GET", vncPath("w1", hid, "nope"), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown target = %d %s", rec.Code, rec.Body.String())
	}
}

func TestHumanVncBadWorkerID422(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ghost := "00000000-0000-4000-8000-000000000001"
	rec := ts.do("GET", vncPath("bad%20id", ghost, "gt-mem"), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad worker_id = %d", rec.Code)
	}
}

func TestHumanVncMetadataEndpointBlocked403(t *testing.T) {
	// Litevirt target pointing at cloud metadata must fail egress before upgrade.
	tgt := litevirtTarget("gt-meta", "acme", "169.254.169.254:443")
	// Force TLS (no insecure_no_tls) so we hit egress, not the loopback TLS gate.
	tgt.Config = map[string]any{"vm_name": "vm1"}
	ts, hid := humanVncServer(t, tgt)
	rec := ts.do("GET", vncPath("w1", hid, "gt-meta"), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("metadata endpoint = %d %s", rec.Code, rec.Body.String())
	}
}

func TestHumanVncLitevirtStubbedSuccessPath(t *testing.T) {
	// Stub dial + serve so we assert the gate resolves ownership/lease correctly
	// without a real ProxyVNC stream.
	tgt := litevirtTarget("gt-lv", "acme", "127.0.0.1:9")
	ts, hid := humanVncServer(t, tgt)

	var (
		gotSession, gotLease, gotPrincipal, gotRole, gotVM string
		dialed                                             bool
	)
	prevDial, prevServe := humanVncDial, humanVncServe
	t.Cleanup(func() {
		humanVncDial = prevDial
		humanVncServe = prevServe
	})
	humanVncDial = func(_ *Server, w http.ResponseWriter, _ *http.Request, target *graphical.Definition) (grpc.ClientConnInterface, string, func() error, bool) {
		dialed = true
		vm, _ := target.Config["vm_name"].(string)
		return nil, vm, func() error { return nil }, true
	}
	humanVncServe = func(w http.ResponseWriter, r *http.Request, _ grpc.ClientConnInterface, vmName string, _ vnc.PolicyEngine, sessionID, leaseID, principalID, principalRole string) {
		gotSession, gotLease, gotPrincipal, gotRole, gotVM = sessionID, leaseID, principalID, principalRole, vmName
		// Pre-upgrade path already passed; write a plain 200 (not a real WS).
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}

	rec := ts.do("GET", vncPath("w1", hid, "gt-lv"), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusOK {
		t.Fatalf("stubbed litevirt = %d %s", rec.Code, rec.Body.String())
	}
	if !dialed {
		t.Fatal("expected dial to be invoked")
	}
	if gotSession != "w1" || gotLease != hid || gotPrincipal != "u1" || gotRole != "admin" || gotVM != "vm1" {
		t.Fatalf("serve args session=%q lease=%q principal=%q role=%q vm=%q",
			gotSession, gotLease, gotPrincipal, gotRole, gotVM)
	}
}

func TestPrincipalPolicyRole(t *testing.T) {
	if principalPolicyRole(nil) != "viewer" {
		t.Fatal("nil → viewer")
	}
	if principalPolicyRole(&serverauth.Principal{Roles: serverauth.NewSet("viewer")}) != "viewer" {
		t.Fatal("viewer")
	}
	if principalPolicyRole(&serverauth.Principal{Roles: serverauth.NewSet("operator")}) != "operator" {
		t.Fatal("operator")
	}
	if principalPolicyRole(&serverauth.Principal{Roles: serverauth.NewSet("admin", "viewer")}) != "admin" {
		t.Fatal("admin ranks highest")
	}
}

func TestResolveHumanVncLeaseEmptyWhenUnbound(t *testing.T) {
	// Manually clear AcquiredBy after acquire → view-only (leaseID empty) allowed.
	ts, hid := humanVncServer(t, litevirtTarget("gt-lv", "acme", "127.0.0.1:9"))
	hs, err := ts.hub.GetRestSession(context.Background(), "w1", hid)
	if err != nil || hs == nil {
		t.Fatalf("get session: %v %v", err, hs)
	}
	hs.AcquiredBy = nil

	prevDial, prevServe := humanVncDial, humanVncServe
	t.Cleanup(func() {
		humanVncDial = prevDial
		humanVncServe = prevServe
	})
	var gotLease string
	humanVncDial = func(_ *Server, w http.ResponseWriter, _ *http.Request, _ *graphical.Definition) (grpc.ClientConnInterface, string, func() error, bool) {
		return nil, "vm1", func() error { return nil }, true
	}
	humanVncServe = func(w http.ResponseWriter, r *http.Request, _ grpc.ClientConnInterface, _ string, _ vnc.PolicyEngine, _, leaseID, _, _ string) {
		gotLease = leaseID
		w.WriteHeader(http.StatusOK)
	}

	rec := ts.do("GET", vncPath("w1", hid, "gt-lv"), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusOK {
		t.Fatalf("unbound lease = %d %s", rec.Code, rec.Body.String())
	}
	if gotLease != "" {
		t.Fatalf("expected empty leaseID for unbound AcquiredBy, got %q", gotLease)
	}
}
