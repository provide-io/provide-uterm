//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net"
	"net/http"
	"net/http/httptest"
	"testing"

	"google.golang.org/grpc"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"

	pb "github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc/gen/litevirt/v1"
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

// fakeLitevirtServer implements enough of ProxyVNC for a successful RFB 3.8
// handshake (ServerInit with tiny framebuffer) then idles on Recv.
type fakeLitevirtServer struct {
	pb.UnimplementedLiteVirtServer
}

func (fakeLitevirtServer) ProxyVNC(stream grpc.BidiStreamingServer[pb.VNCData, pb.VNCData]) error {
	// ProtocolVersion
	if err := stream.Send(&pb.VNCData{Data: []byte("RFB 003.008\n")}); err != nil {
		return err
	}
	// Client version
	if _, err := stream.Recv(); err != nil {
		return err
	}
	// Security types: 1 type = None (1)
	if err := stream.Send(&pb.VNCData{Data: []byte{1, 1}}); err != nil {
		return err
	}
	// Client security choice
	if _, err := stream.Recv(); err != nil {
		return err
	}
	// SecurityResult OK
	if err := stream.Send(&pb.VNCData{Data: []byte{0, 0, 0, 0}}); err != nil {
		return err
	}
	// ClientInit
	if _, err := stream.Recv(); err != nil {
		return err
	}
	// ServerInit: 64x48, 32bpp-ish padding, name len 0
	init := make([]byte, 24)
	init[0], init[1] = 0, 64 // width
	init[2], init[3] = 0, 48 // height
	// name length already 0
	if err := stream.Send(&pb.VNCData{Data: init}); err != nil {
		return err
	}
	// Client may send SetPixelFormat + encodings + FBU request; drain forever.
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
		// Local plaintext mock: TLS-off only on loopback (production uses TLS).
		Config: map[string]any{"vm_name": "vm1", "insecure_no_tls": true},
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
	// Production stores GraphicalSessionManager wrapping the litevirt client.
	if _, ok := st.GraphicalSession.(*GraphicalSessionManager); !ok {
		t.Fatalf("expected GraphicalSessionManager, got %T", st.GraphicalSession)
	}
}

func TestGUIAttachLitevirtMetadataEndpointBlocked(t *testing.T) {
	// Cloud-metadata endpoints must be rejected even when block_private is off.
	tgt := &graphical.Definition{
		TargetID: "gt-meta", TenantID: "acme", Protocol: graphical.ProtocolLitevirt,
		Endpoint: strPtrLocal("169.254.169.254:443"), Width: 64, Height: 48,
		Config: map[string]any{"vm_name": "vm1"},
	}
	ts := attachTestServer(t, tgt)
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-meta"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("metadata attach = %d %s (want 403)", rec.Code, rec.Body.String())
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
	// This port now speaks every protocol the registry will accept — memory,
	// rfb and litevirt — so the 501 branch is no longer reachable by seeding a
	// target. It stays as a guard against a store written behind the registry's
	// validation, and is exercised by calling the dispatcher with such a value.
	//
	// rfb was this case until vnc.RFBClient landed.
	ts := attachTestServer(t)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/worker/w1/gui/attach", nil)
	unvalidated := &graphical.Definition{
		TargetID: "gt-odd", TenantID: "acme", Protocol: "vmware",
		Endpoint: strPtrLocal("127.0.0.1:5900"), Width: 64, Height: 48,
	}
	if _, _, _, ready := ts.srv.buildGraphicalSession(rec, req, unvalidated); ready {
		t.Fatal("an unknown protocol must not produce a session")
	}
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("unknown protocol = %d %s", rec.Code, rec.Body.String())
	}
}

func TestGUIAttachRfbUnreachableConsole502(t *testing.T) {
	// rfb attaches for real now; a console that is not listening is a gateway
	// failure, not a bad request. Port 1 refuses instantly.
	rfb := &graphical.Definition{
		TargetID: "gt-rfb", TenantID: "acme", Protocol: graphical.ProtocolRfb,
		Endpoint: strPtrLocal("127.0.0.1:1"), Width: 64, Height: 48,
	}
	ts := attachTestServer(t, rfb)
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-rfb"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("unreachable rfb attach = %d %s", rec.Code, rec.Body.String())
	}
}

func TestGUIAttachRfbCloudMetadataRefusedBeforeDial(t *testing.T) {
	// The egress guard runs first, so a target cannot name the metadata service
	// even with block_private_connector_targets off, which is the default.
	rfb := &graphical.Definition{
		TargetID: "gt-meta", TenantID: "acme", Protocol: graphical.ProtocolRfb,
		Endpoint: strPtrLocal("169.254.169.254:5900"), Width: 64, Height: 48,
	}
	ts := attachTestServer(t, rfb)
	ts.setupWorker(t, "w1")
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-meta"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("metadata rfb attach = %d %s", rec.Code, rec.Body.String())
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
