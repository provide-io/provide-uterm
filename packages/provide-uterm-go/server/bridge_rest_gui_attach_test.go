//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net"
	"net/http"
	"testing"

	"google.golang.org/grpc"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc"

	pb "github.com/litevirt/litevirt/gen/litevirt/v1"
)

// seedTarget adds an immutable static graphical target to a fresh registry, then
// installs that registry on the server deps via the newTestServer opt hook.
func attachTestServer(t *testing.T, targets ...*graphical.Definition) *testServer {
	t.Helper()
	reg := graphical.NewInMemoryRegistry()
	for _, tgt := range targets {
		if err := reg.AddStatic(tgt); err != nil {
			t.Fatalf("seed target %s: %v", tgt.TargetID, err)
		}
	}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.GraphicalTargets = reg
	})
	return ts
}

func memoryTarget(id, tenant string) *graphical.Definition {
	return &graphical.Definition{
		TargetID: id, TenantID: tenant, Protocol: graphical.ProtocolMemory,
		Width: 64, Height: 48,
	}
}

// fakeLitevirtServer implements just enough of the LiteVirt gRPC service to let a
// ProxyVNC bidi stream be established (it blocks on Recv, sending nothing).
type fakeLitevirtServer struct {
	pb.UnimplementedLiteVirtServer
}

func (fakeLitevirtServer) ProxyVNC(stream grpc.BidiStreamingServer[pb.VNCData, pb.VNCData]) error {
	for {
		if _, err := stream.Recv(); err != nil {
			return err
		}
	}
}

// startLitevirtGRPC brings up an in-process LiteVirt gRPC server on a loopback
// port and returns its host:port. Torn down at test end.
func startLitevirtGRPC(t *testing.T) string {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := grpc.NewServer()
	pb.RegisterLiteVirtServer(srv, fakeLitevirtServer{})
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	return lis.Addr().String()
}

func TestGUIAttachMemorySuccess(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")

	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusOK {
		t.Fatalf("attach = %d %s", rec.Code, rec.Body.String())
	}
	body := decode(t, rec.Body.Bytes())
	if body["ok"] != true || body["target_id"] != "gt-mem" {
		t.Fatalf("attach body = %v", body)
	}
	st := ts.hub.Registry.Get("w1")
	if st == nil || st.GraphicalSession == nil {
		t.Fatalf("graphical session not stored on worker state")
	}
}

func TestGUIAttachLitevirtRegistryDriven(t *testing.T) {
	addr := startLitevirtGRPC(t)
	tgt := &graphical.Definition{
		TargetID: "gt-lv", TenantID: "acme", Protocol: graphical.ProtocolLitevirt,
		Endpoint: strPtrLocal(addr), Width: 64, Height: 48,
		Config: map[string]any{"vm_name": "vm1"},
	}
	ts := attachTestServer(t, tgt)
	ts.setupWorker(t, "w1")

	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-lv"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusOK {
		t.Fatalf("litevirt attach = %d %s", rec.Code, rec.Body.String())
	}
	st := ts.hub.Registry.Get("w1")
	if st == nil {
		t.Fatalf("worker state missing")
	}
	if _, ok := st.GraphicalSession.(*vnc.LitevirtAIClient); !ok {
		t.Fatalf("expected litevirt session, got %T", st.GraphicalSession)
	}
}

func TestGUIAttachUnknownTarget404(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"nope"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown target = %d %s", rec.Code, rec.Body.String())
	}
}

func TestGUIAttachCapabilityDenied403(t *testing.T) {
	// Viewer lacks graphical.session.attach → 403 before the hijack gate.
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, tenantHeaders("viewer", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("viewer attach = %d", rec.Code)
	}
}

func TestGUIAttachHijackDenied403(t *testing.T) {
	// Operator has graphical.session.attach but not session.control.hijack → 403.
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, tenantHeaders("operator", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("operator attach = %d", rec.Code)
	}
}

func TestGUIAttachCrossTenantDenied404(t *testing.T) {
	// Target belongs to tenant "acme"; admin scoped to "beta" cannot see it → 404.
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, tenantHeaders("admin", "beta"))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant attach = %d %s", rec.Code, rec.Body.String())
	}
}

func TestGUIAttachWrongProtocol501(t *testing.T) {
	// rfb is a valid target protocol but this port ships no RFB session client.
	rfb := &graphical.Definition{
		TargetID: "gt-rfb", TenantID: "acme", Protocol: graphical.ProtocolRfb,
		Endpoint: strPtrLocal("127.0.0.1:5900"), Width: 64, Height: 48,
	}
	ts := attachTestServer(t, rfb)
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-rfb"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("rfb attach = %d %s", rec.Code, rec.Body.String())
	}
}

func TestGUIAttachMissingTargetID422(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("missing target_id = %d", rec.Code)
	}
}

func TestGUIAttachNoTenantScope403(t *testing.T) {
	// Admin without a tenant claim: capability + hijack pass, but no tenant scope.
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, adminHeaders())
	if rec.Code != http.StatusForbidden {
		t.Fatalf("no-tenant attach = %d %s", rec.Code, rec.Body.String())
	}
}

func TestGUIAttachBadWorkerID422(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	rec := ts.do("POST", "/worker/bad%20worker/gui/attach", `{"target_id":"gt-mem"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad worker_id = %d", rec.Code)
	}
}

func TestSeedGraphicalTargetsLitevirt(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{
		// vm_name via the [graphical_targets.config] table.
		{TargetID: "gt-lv1", TenantID: "acme", Protocol: "litevirt", TargetAddress: "dns:///vm.local:9000",
			Enabled: true, Config: map[string]any{"vm_name": "cfgvm"}},
		// vm_name via the legacy top-level field folds into config.
		{TargetID: "gt-lv2", TenantID: "acme", Protocol: "litevirt", TargetAddress: "vm.local:9001",
			Enabled: true, VMName: strPtrLocal("topvm")},
	}
	reg, err := SeedGraphicalTargets(cfg)
	if err != nil {
		t.Fatalf("seed: %v", err)
	}
	scope, _ := graphical.ScopeForTenant("acme")

	one, _ := reg.Get(scope, "gt-lv1")
	if one == nil || one.Endpoint == nil || *one.Endpoint != "vm.local:9000" || one.Config["vm_name"] != "cfgvm" {
		t.Fatalf("gt-lv1 seed wrong: %+v", one)
	}
	two, _ := reg.Get(scope, "gt-lv2")
	if two == nil || two.Config["vm_name"] != "topvm" {
		t.Fatalf("gt-lv2 top-level vm_name not folded: %+v", two)
	}
}
