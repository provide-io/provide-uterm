//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestDefaultConnect(t *testing.T) {
	ctx := context.Background()

	// shell → real PTY-backed connector.
	shellConn, err := defaultConnect(ctx, serverconfig.SessionDefinition{
		SessionID: "sh", DisplayName: "sh", ConnectorType: "shell",
	})
	if err != nil || shellConn == nil {
		t.Fatalf("shell connect: %v %v", shellConn, err)
	}
	_ = shellConn.Stop(ctx)

	// telnet → real dial to a loopback echo server.
	host, port := startEchoServer(t, nil)
	tconn, err := defaultConnect(ctx, serverconfig.SessionDefinition{
		SessionID: "tn", DisplayName: "tn", ConnectorType: "telnet",
		ConnectorConfig: map[string]any{"host": host, "port": port},
	})
	if err != nil || tconn == nil {
		t.Fatalf("telnet connect: %v %v", tconn, err)
	}
	_ = tconn.Stop(ctx)

	// websocket → bad URL fails at dial.
	if _, err := defaultConnect(ctx, serverconfig.SessionDefinition{
		SessionID: "ws", DisplayName: "ws", ConnectorType: "websocket",
		ConnectorConfig: map[string]any{"url": "ws://127.0.0.1:1/nope"},
	}); err == nil {
		t.Fatal("expected websocket dial failure")
	}

	// unknown connector type → Build error.
	if _, err := defaultConnect(ctx, serverconfig.SessionDefinition{
		SessionID: "x", DisplayName: "x", ConnectorType: "bogus",
	}); err == nil {
		t.Fatal("expected unsupported connector_type error")
	}
}

func TestSnapshotEdgeCases(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	r := NewSessionRegistry(cfg)
	// Seed a definition with nil Tags, an explicit recording flag, and a zero
	// CreatedAt — exercising snapshotStatus/recordingEnabled/seed branches.
	rec := true
	r.seed(serverconfig.SessionDefinition{
		SessionID: "nt", DisplayName: "nt", ConnectorType: "shell",
		InputMode: "open", Visibility: "public", Tags: nil, RecordingEnabled: &rec,
	})
	st, err := r.GetSession(context.Background(), "nt")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if st.Tags == nil || len(st.Tags) != 0 {
		t.Fatalf("tags should normalize to empty slice, got %v", st.Tags)
	}
	if !st.RecordingEnabled {
		t.Fatal("explicit recording flag should be honored")
	}
	if st.CreatedAt == "" {
		t.Fatal("zero CreatedAt should be filled")
	}
}

func TestSessionIDValidDirect(t *testing.T) {
	if sessionIDValid("") {
		t.Fatal("empty id must be invalid")
	}
	if sessionIDValid("bad id") {
		t.Fatal("space must be invalid")
	}
	if !sessionIDValid("ok-1_2") {
		t.Fatal("valid id rejected")
	}
}

func TestLastSnapshotMissing(t *testing.T) {
	r := NewSessionRegistry(serverconfig.DefaultServerConfig())
	if snap, err := r.LastSnapshot(context.Background(), "missing"); err != nil || snap != nil {
		t.Fatalf("missing snapshot: %v %v", snap, err)
	}
}

func TestBuildAuthenticatorWebhookSecret(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	cfg.Auth.Mode = "jwt"
	cfg.Auth.IdentityProvider = "webhook"
	url := "https://idp.example.com/verify"
	secret := "s3cr3t-value" // pragma: allowlist secret
	cfg.Auth.WebhookIDPURL = &url
	cfg.Auth.WebhookIDPSecret = &secret
	cfg.Auth.WebhookIDPRequireSignedResponse = false
	if _, _, err := buildAuthenticator(cfg, nil); err != nil {
		t.Fatalf("webhook w/ secret: %v", err)
	}
}

func TestBuildServerWebhookAuth(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "webhook.toml")
	body := `
[server]
host = "127.0.0.1"
port = 0

[auth]
mode = "jwt"
identity_provider = "webhook"
webhook_idp_url = "https://idp.example.com/verify"
webhook_idp_secret = "webhook-idp-secret-abcdefghij0123456789"  # pragma: allowlist secret
worker_bearer_token = "worker-token-abcdefghij0123456789"  # pragma: allowlist secret
jwt_public_key_pem = "-----BEGIN PUBLIC KEY-----\nMFkwabc\n-----END PUBLIC KEY-----"
`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	// Exercises buildServer's webhook auth branch end-to-end (engine open +
	// server.New).
	b, err := buildServer(context.Background(), path, "", 0, "")
	if err != nil {
		t.Fatalf("buildServer webhook: %v", err)
	}
	_ = b.engine.Close(context.Background())
}

func TestServerCmdRunError(t *testing.T) {
	// Drives newServerCmd's RunE via Execute with a bad config path.
	var out, errw bytes.Buffer
	if code := Execute([]string{"server", "--config", "/no/such.toml"}, &out, &errw); code == 0 {
		t.Fatal("server with bad config should exit non-zero")
	}
}

func TestRunProxyLifecycle(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(100 * time.Millisecond)
		cancel()
	}()
	opts := proxyOptions{Bind: "127.0.0.1", Port: 0, Path: "/ws/terminal", Transport: "telnet"}
	if err := runProxy(ctx, opts); err != nil {
		t.Fatalf("runProxy: %v", err)
	}
}
