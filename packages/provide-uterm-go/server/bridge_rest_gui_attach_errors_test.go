//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// attachErrorServer seeds the targets and records the server log, so a test can
// assert that what the response leaves out is still written down server-side.
func attachErrorServer(t *testing.T, targets ...*graphical.Definition) (*testServer, *bytes.Buffer) {
	t.Helper()
	reg := graphical.NewInMemoryRegistry()
	for _, tgt := range targets {
		if err := reg.AddStatic(tgt); err != nil {
			t.Fatalf("seed target %s: %v", tgt.TargetID, err)
		}
	}
	var logs bytes.Buffer
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.GraphicalTargets = reg
		deps.Logger = slog.New(slog.NewTextHandler(&logs, nil))
	})
	ts.setupWorker(t, "w1")
	return ts, &logs
}

// assertAttachBody pins the whole body to the fixed text and checks that none
// of the internal detail reached it.
func assertAttachBody(t *testing.T, body string, want string, leaked ...string) {
	t.Helper()
	got := gtJSON(t, []byte(body))
	if len(got) != 1 || got["detail"] != want {
		t.Fatalf("body = %s, want {\"detail\": %q}", body, want)
	}
	for _, s := range leaked {
		if strings.Contains(body, s) {
			t.Fatalf("body leaks %q: %s", s, body)
		}
	}
}

func TestGUIAttachRfbDialFailureDoesNotEchoTheSocketError(t *testing.T) {
	// Port 1 refuses instantly; the socket error names the address and port,
	// and the caller named only a target id.
	rfb := &graphical.Definition{
		TargetID: "gt-rfb", TenantID: "acme", Protocol: graphical.ProtocolRfb,
		Endpoint: strPtrLocal("127.0.0.1:1"), Width: 64, Height: 48,
	}
	ts, logs := attachErrorServer(t, rfb)
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-rfb"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
	}
	assertAttachBody(t, rec.Body.String(), "rfb connect failed: the console did not accept a session",
		"127.0.0.1", ":1", "refused", "dial")
	if !strings.Contains(logs.String(), "gui_attach_rfb_failed") || !strings.Contains(logs.String(), "gt-rfb") {
		t.Fatalf("dial failure not logged with the target id: %s", logs.String())
	}
}

func TestGUIAttachRfbMetadataRefusalIsFixedText(t *testing.T) {
	rfb := &graphical.Definition{
		TargetID: "gt-meta", TenantID: "acme", Protocol: graphical.ProtocolRfb,
		Endpoint: strPtrLocal("169.254.169.254:5900"), Width: 64, Height: 48,
	}
	ts, logs := attachErrorServer(t, rfb)
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-meta"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
	}
	assertAttachBody(t, rec.Body.String(), "invalid endpoint: the target's host is not an allowed destination",
		"169.254", "metadata")
	if !strings.Contains(logs.String(), "gui_attach_egress_blocked") || !strings.Contains(logs.String(), "169.254.169.254") {
		t.Fatalf("egress refusal not logged with the host: %s", logs.String())
	}
}

func TestGUIAttachRfbEgressErrorDoesNotEchoTheGuardReason(t *testing.T) {
	// Whatever the guard fails with, the caller gets the fixed text: here a
	// resolver fault that names the host and a local path.
	rfb := &graphical.Definition{
		TargetID: "gt-rfb", TenantID: "acme", Protocol: graphical.ProtocolRfb,
		Endpoint: strPtrLocal("console.internal.example:5900"), Width: 64, Height: 48,
	}
	ts, logs := attachErrorServer(t, rfb)
	ts.srv.egress = NewEgressGuard(func(_ context.Context, host string) ([]string, error) {
		return nil, errors.New("resolver exploded for " + host + " at /etc/resolv.conf")
	}, nil)
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-rfb"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
	}
	assertAttachBody(t, rec.Body.String(), "invalid endpoint: the target's host is not an allowed destination",
		"console.internal", "resolv", "exploded")
	if !strings.Contains(logs.String(), "console.internal.example") {
		t.Fatalf("egress refusal not logged with the host: %s", logs.String())
	}
}

func TestGUIAttachLitevirtMetadataRefusalIsFixedText(t *testing.T) {
	// litevirt is this port's own protocol, but it answers through the same
	// attach route, so its egress refusal reads the same as rfb's.
	tgt := &graphical.Definition{
		TargetID: "gt-meta", TenantID: "acme", Protocol: graphical.ProtocolLitevirt,
		Endpoint: strPtrLocal("169.254.169.254:443"), Width: 64, Height: 48,
		Config: map[string]any{"vm_name": "vm1"},
	}
	ts, logs := attachErrorServer(t, tgt)
	rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-meta"}`, tenantHeaders("admin", "acme"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
	}
	assertAttachBody(t, rec.Body.String(), "invalid endpoint: the target's host is not an allowed destination",
		"169.254", "metadata")
	if !strings.Contains(logs.String(), "gui_attach_egress_blocked") || !strings.Contains(logs.String(), "gt-meta") {
		t.Fatalf("egress refusal not logged with the target id: %s", logs.String())
	}
}
