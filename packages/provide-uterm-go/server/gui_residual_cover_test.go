//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/grpc"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc"
)

// failingTargets is a graphical registry whose lookups fail with a non-domain
// error (a backend outage).
type failingTargets struct{ *graphical.InMemoryRegistry }

func (failingTargets) Get(graphical.Scope, string) (*graphical.Definition, error) {
	return nil, errors.New("backend down")
}

func failingTargetsServer(t *testing.T) *testServer {
	t.Helper()
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.GraphicalTargets = failingTargets{graphical.NewInMemoryRegistry()}
	})
	ts.setupWorker(t, "w1")
	return ts
}

func assertBackendFailure(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "graphical target backend failed") {
		t.Fatalf("status/body = %d %s; want 503 backend failure", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "backend down") {
		t.Fatalf("backend error leaked: %s", rec.Body.String())
	}
}

func TestGUIAttachTargetLookupFailureIs503(t *testing.T) {
	ts := failingTargetsServer(t)
	assertBackendFailure(t, ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-x"}`, tenantHeaders("admin", "acme")))
}

func TestHumanVncTargetLookupFailureIs503(t *testing.T) {
	ts := failingTargetsServer(t)
	hid := acquireLease(t, ts, "w1", tenantHeaders("admin", "acme"))
	assertBackendFailure(t, ts.do("GET", vncPath("w1", hid, "gt-x"), "", tenantHeaders("admin", "acme")))
}

func TestHumanVncWithoutTenantIsForbidden(t *testing.T) {
	ts, hid := humanVncServer(t, litevirtTarget("gt-lv", "acme", "127.0.0.1:1"))
	// Same subject as the lease holder, but no tenant claim to scope the lookup.
	rec := ts.do("GET", vncPath("w1", hid, "gt-lv"), "", map[string]string{"X-Subject": "u1", "X-Role": "admin"})
	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "graphical target access denied") {
		t.Fatalf("no tenant = %d %s; want 403", rec.Code, rec.Body.String())
	}
}

// permissiveAuthz authorizes every session mutation, even for no principal.
type permissiveAuthz struct {
	serverauth.LocalAuthorizationProvider
}

func (permissiveAuthz) CanMutateSession(*serverauth.Principal, *serverconfig.SessionDefinition, string) bool {
	return true
}

func TestResolveHumanVncWithoutPrincipalIsUnauthorized(t *testing.T) {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Authz = serverauth.NewAuthorizationServiceWith(permissiveAuthz{})
	})
	ts.reg.add("w1", "admin1", "public")
	req := httptest.NewRequest("GET", "/worker/w1/hijack/h/gui/vnc", http.NoBody)
	req.SetPathValue("worker_id", "w1")
	req.SetPathValue("hijack_id", "00000000-0000-4000-8000-000000000001")
	rec := httptest.NewRecorder()
	relay, ok := ts.srv.resolveHumanVnc(rec, req)
	if ok || relay != nil || rec.Code != http.StatusUnauthorized {
		t.Fatalf("resolve = %v, %v, %d %s; want 401", relay, ok, rec.Code, rec.Body.String())
	}
}

func TestHumanVncDialsRegistryTargetWithoutOverride(t *testing.T) {
	ts, hid := humanVncServer(t, litevirtTarget("gt-lv", "acme", "127.0.0.1:1"))
	prevDial, prevServe := humanVncDial, humanVncServe
	t.Cleanup(func() { humanVncDial, humanVncServe = prevDial, prevServe })
	humanVncDial = nil
	var gotVM, gotSession string
	var gotConn bool
	humanVncServe = func(w http.ResponseWriter, _ *http.Request, cc grpc.ClientConnInterface, vmName string, _ vnc.PolicyEngine, sessionID, _, _, _ string) {
		gotConn, gotVM, gotSession = cc != nil, vmName, sessionID
		w.WriteHeader(http.StatusOK)
	}
	rec := ts.do("GET", vncPath("w1", hid, "gt-lv"), "", tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusOK || !gotConn || gotVM != "vm1" || gotSession != "w1" {
		t.Fatalf("relay = %d conn=%v vm=%q session=%q", rec.Code, gotConn, gotVM, gotSession)
	}
}

// fakeResolver makes every DNS name resolve to addrs, without touching DNS.
func fakeResolver(addrs ...string) EgressResolver {
	return func(context.Context, string) ([]string, error) { return addrs, nil }
}

func stubLookupIP(t *testing.T, ips []net.IP, err error) {
	t.Helper()
	orig := lookupIP
	lookupIP = func(string) ([]net.IP, error) { return ips, err }
	t.Cleanup(func() { lookupIP = orig })
}

func dialTarget(t *testing.T, ts *testServer, endpoint string, insecureNoTLS bool) (*httptest.ResponseRecorder, *grpc.ClientConn) {
	t.Helper()
	target := &graphical.Definition{
		TargetID: "gt-lv", Protocol: graphical.ProtocolLitevirt, Endpoint: &endpoint,
		Config: map[string]any{"vm_name": "vm1", "insecure_no_tls": insecureNoTLS},
	}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", http.NoBody)
	cc, vm, ok := ts.srv.dialLitevirtTarget(rec, req, target)
	if ok != (cc != nil) {
		t.Fatalf("ok=%v but cc=%v", ok, cc)
	}
	if cc != nil {
		t.Cleanup(func() { _ = cc.Close() })
		if vm != "vm1" {
			t.Fatalf("vm = %q", vm)
		}
	}
	return rec, cc
}

func TestDialLitevirtTargetEndpointWithoutPort(t *testing.T) {
	ts := newTestServer(t, nil)
	if rec, cc := dialTarget(t, ts, "127.0.0.1", true); cc == nil {
		t.Fatalf("bare loopback host refused: %d %s", rec.Code, rec.Body.String())
	}
}

func TestDialLitevirtTargetWithoutGuardUsesDefault(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.egress = nil
	if rec, cc := dialTarget(t, ts, "127.0.0.1:5900", true); cc == nil {
		t.Fatalf("loopback refused without a configured guard: %d %s", rec.Code, rec.Body.String())
	}
	if rec, cc := dialTarget(t, ts, "169.254.169.254:5900", false); cc != nil || rec.Code != http.StatusForbidden {
		t.Fatalf("default guard let metadata through: %d %s", rec.Code, rec.Body.String())
	}
}

func TestDialLitevirtTargetInsecureHostnameChecks(t *testing.T) {
	cases := []struct {
		name    string
		ips     []net.IP
		err     error
		wantOK  bool
		wantMsg string
	}{
		{"unresolvable", nil, errors.New("nxdomain"), false, "insecure_no_tls requires resolvable loopback endpoint"},
		{"empty", nil, nil, false, "insecure_no_tls requires resolvable loopback endpoint"},
		{"remote", []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("10.0.0.1")}, nil, false, "insecure_no_tls only allowed for loopback endpoints"},
		{"loopback", []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")}, nil, true, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ts := newTestServer(t, nil)
			ts.srv.egress = NewEgressGuard(fakeResolver("10.0.0.5"), nil)
			stubLookupIP(t, tc.ips, tc.err)
			rec, cc := dialTarget(t, ts, "vm.test:443", true)
			if (cc != nil) != tc.wantOK {
				t.Fatalf("ok = %v, want %v (%d %s)", cc != nil, tc.wantOK, rec.Code, rec.Body.String())
			}
			if !tc.wantOK && (rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), tc.wantMsg)) {
				t.Fatalf("refusal = %d %s; want 403 %q", rec.Code, rec.Body.String(), tc.wantMsg)
			}
		})
	}
}

func TestDialLitevirtTargetInsecureRemoteLiteralRefused(t *testing.T) {
	ts := newTestServer(t, nil)
	rec, cc := dialTarget(t, ts, "10.0.0.7:443", true)
	if cc != nil || rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "only allowed for loopback") {
		t.Fatalf("remote literal = %d %s; want 403", rec.Code, rec.Body.String())
	}
}

func TestDialLitevirtTargetTLSByDefault(t *testing.T) {
	ts := newTestServer(t, nil)
	if rec, cc := dialTarget(t, ts, "127.0.0.1:443", false); cc == nil {
		t.Fatalf("TLS dial refused: %d %s", rec.Code, rec.Body.String())
	}
}

func TestDialLitevirtTargetClientConstructionFailure(t *testing.T) {
	ts := newTestServer(t, nil)
	rec, cc := dialTarget(t, ts, "127.0.0.1:%zz", true)
	if cc != nil || rec.Code != http.StatusInternalServerError || !strings.Contains(rec.Body.String(), attachLitevirtFailed) {
		t.Fatalf("bad target = %d %s; want 500 %q", rec.Code, rec.Body.String(), attachLitevirtFailed)
	}
}

// startFakeRFB serves a minimal RFB 3.8 handshake (security None, 64x48) and
// then drains the client until it hangs up.
func startFakeRFB(t *testing.T) (string, int) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		step := func(out []byte, readN int) bool {
			if _, err := conn.Write(out); err != nil {
				return false
			}
			_, err := io.ReadFull(conn, make([]byte, readN))
			return err == nil
		}
		if !step([]byte("RFB 003.008\n"), 12) || !step([]byte{1, 1}, 1) || !step([]byte{0, 0, 0, 0}, 1) {
			return
		}
		init := make([]byte, 24)
		init[1], init[3] = 64, 48
		if _, err := conn.Write(init); err != nil {
			return
		}
		_, _ = io.Copy(io.Discard, conn)
	}()
	addr := ln.Addr().(*net.TCPAddr)
	return addr.IP.String(), addr.Port
}

func TestBuildRfbSessionInvalidEndpoint(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := httptest.NewRecorder()
	sess, closer, cancel, ok := ts.srv.buildRfbSession(rec, httptest.NewRequest("POST", "/", http.NoBody),
		&graphical.Definition{TargetID: "gt-rfb", Protocol: graphical.ProtocolRfb})
	if ok || sess != nil || closer != nil || cancel != nil {
		t.Fatal("missing endpoint accepted")
	}
	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "invalid endpoint: ") {
		t.Fatalf("invalid endpoint = %d %s", rec.Code, rec.Body.String())
	}
}

func TestBuildRfbSessionConnectsWithDefaultGuard(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.egress = nil
	host, port := startFakeRFB(t)
	endpoint := host + ":" + strconv.Itoa(port)
	rec := httptest.NewRecorder()
	sess, closer, cancel, ok := ts.srv.buildRfbSession(rec, httptest.NewRequest("POST", "/", http.NoBody),
		&graphical.Definition{TargetID: "gt-rfb", Protocol: graphical.ProtocolRfb, Endpoint: &endpoint})
	if !ok || sess == nil || closer == nil || cancel != nil {
		t.Fatalf("rfb session = %v %v %v %v (%d %s)", sess, closer, cancel != nil, ok, rec.Code, rec.Body.String())
	}
	_ = closer.Close()
}

// stuckLitevirt never finishes its loop until released and never becomes ready.
type stuckLitevirt struct {
	gui.GraphicalSession
	release chan struct{}
}

func (s *stuckLitevirt) RunHandshakeAndLoop() error    { <-s.release; return nil }
func (*stuckLitevirt) WaitReady(context.Context) error { return errors.New("not ready") }

func TestBuildLitevirtSessionBoundsTheDrainOfAStuckLoop(t *testing.T) {
	ts := newTestServer(t, nil)
	stuck := &stuckLitevirt{release: make(chan struct{})}
	t.Cleanup(func() { close(stuck.release) })
	origNew, origDrain := newLitevirtSession, litevirtDrainTimeout
	newLitevirtSession = func(context.Context, grpc.ClientConnInterface, string) (litevirtSession, error) { return stuck, nil }
	litevirtDrainTimeout = 0
	t.Cleanup(func() { newLitevirtSession, litevirtDrainTimeout = origNew, origDrain })

	endpoint := "127.0.0.1:1"
	rec := httptest.NewRecorder()
	sess, closer, cancel, ok := ts.srv.buildLitevirtSession(rec, httptest.NewRequest("POST", "/", http.NoBody),
		&graphical.Definition{TargetID: "gt-lv", Protocol: graphical.ProtocolLitevirt, Endpoint: &endpoint,
			Config: map[string]any{"vm_name": "vm1", "insecure_no_tls": true}})
	if ok || sess != nil || closer != nil || cancel != nil {
		t.Fatal("unready litevirt session accepted")
	}
	if rec.Code != http.StatusBadGateway || !strings.Contains(rec.Body.String(), attachHandshakeFailed) {
		t.Fatalf("handshake failure = %d %s", rec.Code, rec.Body.String())
	}
}
