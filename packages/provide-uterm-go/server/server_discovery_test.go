//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// TestWebhookDiscoveryAnnounce posts a NodeStatus to an httptest server and
// asserts the body + bearer header.
func TestWebhookDiscoveryAnnounce(t *testing.T) {
	type capture struct {
		auth string
		body NodeStatus
	}
	got := make(chan capture, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var st NodeStatus
		_ = json.Unmarshal(raw, &st)
		got <- capture{auth: r.Header.Get("Authorization"), body: st}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	p := NewWebhookDiscoveryProvider(srv.URL, "sekret", 0, nil)
	status := NodeStatus{NodeID: "node-a", ActiveSessions: 3, WorkerCount: 2, Timestamp: 42.0}
	if err := p.Announce(context.Background(), status); err != nil {
		t.Fatalf("announce: %v", err)
	}
	select {
	case c := <-got:
		if c.auth != "Bearer sekret" {
			t.Errorf("auth=%q", c.auth)
		}
		if c.body != status {
			t.Errorf("body=%+v want %+v", c.body, status)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no request received")
	}
}

// TestWebhookDiscoveryEgressBlocked proves a metadata target is not POSTed.
func TestWebhookDiscoveryEgressBlocked(t *testing.T) {
	p := NewWebhookDiscoveryProvider("http://169.254.169.254/announce", "", 0, nil)
	if err := p.Announce(context.Background(), NodeStatus{NodeID: "x"}); !isEgressBlocked(err) {
		t.Fatalf("want EgressBlockedError, got %v", err)
	}
}

// TestWebhookDiscoveryPostError covers the POST-failure branch: an allowed
// (non-metadata) target whose connection is refused.
func TestWebhookDiscoveryPostError(t *testing.T) {
	// 127.0.0.1:1 is a private (allowed) target where the POST will fail.
	p := NewWebhookDiscoveryProvider("http://127.0.0.1:1/announce", "", 0, nil)
	if err := p.Announce(context.Background(), NodeStatus{NodeID: "x"}); err == nil {
		t.Fatal("want POST error on refused connection")
	}
}

func TestNoOpDiscoveryAnnounce(t *testing.T) {
	if err := (NoOpDiscoveryProvider{}).Announce(context.Background(), NodeStatus{}); err != nil {
		t.Fatalf("noop announce: %v", err)
	}
}

// TestBuildDiscoveryProvider covers both branches of provider selection.
func TestBuildDiscoveryProvider(t *testing.T) {
	// no URL → NoOp
	ts := newTestServer(t, nil)
	if _, ok := ts.srv.buildDiscoveryProvider().(NoOpDiscoveryProvider); !ok {
		t.Fatal("default config should yield NoOp provider")
	}
	// webhook URL (+ secret) → WebhookDiscoveryProvider
	url := "https://registry.example/announce"
	secret := "shhh" // pragma: allowlist secret
	ts = newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Governance.DiscoveryProvider = "webhook"
		cfg.Governance.RegistryWebhookURL = &url
		cfg.Governance.RegistryWebhookSecret = &secret
	})
	if _, ok := ts.srv.buildDiscoveryProvider().(*WebhookDiscoveryProvider); !ok {
		t.Fatal("webhook config should yield WebhookDiscoveryProvider")
	}
	if st := ts.srv.nodeStatus(context.Background()); st.NodeID != ts.srv.cfg.Server.NodeID {
		t.Fatalf("nodeStatus node_id=%q", st.NodeID)
	}
}

// TestHeartbeatSweepPosts drives the heartbeat sweep end-to-end against an
// httptest server on a tiny interval.
func TestHeartbeatSweepPosts(t *testing.T) {
	hit := make(chan struct{}, 4)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		select {
		case hit <- struct{}{}:
		default:
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	url := srv.URL + "/announce"
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Governance.DiscoveryProvider = "webhook"
		cfg.Governance.RegistryWebhookURL = &url
		cfg.Governance.RegistryWebhookIntervalS = 0.02
	})
	ctx, cancel := context.WithCancel(context.Background())
	ts.srv.StartSweeps(ctx)
	defer func() { cancel(); ts.srv.sweepWG.Wait() }()

	select {
	case <-hit:
	case <-time.After(3 * time.Second):
		t.Fatal("heartbeat sweep never posted")
	}
}

// TestHeartbeatSweepNoopSkips proves the NoOp branch of the heartbeat starter
// does not launch a sweep (default config has no registry webhook URL).
func TestHeartbeatSweepNoopSkips(t *testing.T) {
	ts := newTestServer(t, nil)
	ctx, cancel := context.WithCancel(context.Background())
	ts.srv.startNodeRegistryHeartbeat(ctx)
	cancel()
	ts.srv.sweepWG.Wait() // returns immediately when no sweep was launched
}
