//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// TestBrowserWSQuotaRejected covers RegisterBrowser rejecting a second
// connection when MaxConnectionsPerPrincipal=1 (policy-violation close).
func TestBrowserWSQuotaRejected(t *testing.T) {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock:                      deps.Clock,
			OnMetric:                   deps.Metrics.Inc,
			Logger:                     deps.Logger,
			MaxConnectionsPerPrincipal: 1,
		})
	})
	ts.reg.add("q1", "admin1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	// First connection for admin1 succeeds.
	b1 := dialBrowser(t, ctx, base+"/ws/browser/q1/term", "admin1", "admin")
	defer func() { _ = b1.conn.Close(websocket.StatusNormalClosure, "") }()
	b1.waitFrame(t, "hello", 5*time.Second)

	// Second connection for the same principal is rejected at RegisterBrowser.
	conn, _, err := websocket.Dial(ctx, base+"/ws/browser/q1/term", &websocket.DialOptions{
		HTTPHeader: map[string][]string{"X-Subject": {"admin1"}, "X-Role": {"admin"}},
	})
	if err != nil {
		// Some stacks surface the rejection as a dial error; either is fine.
		return
	}
	// If dial succeeded, the server should close with policy violation shortly.
	_, _, rerr := conn.Read(ctx)
	_ = conn.Close(websocket.StatusNormalClosure, "")
	if rerr == nil {
		t.Fatal("expected second connection to be closed/rejected")
	}
}
