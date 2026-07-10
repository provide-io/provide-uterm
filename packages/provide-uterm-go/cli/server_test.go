//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestApplyServerOverrides(t *testing.T) {
	cases := []struct {
		name       string
		startURL   string
		host       string
		port       int
		wantHost   string
		wantPort   int
		wantPublic string
	}{
		{"no override", "http://127.0.0.1:8780", "", 0, "127.0.0.1", 8780, "http://127.0.0.1:8780"},
		{"host only", "http://127.0.0.1:8780", "0.0.0.0", 0, "0.0.0.0", 8780, "http://0.0.0.0:8780"},
		{"port only", "http://127.0.0.1:8780", "", 9000, "127.0.0.1", 9000, "http://127.0.0.1:9000"},
		{"both", "http://127.0.0.1:8780", "example.com", 443, "example.com", 443, "http://example.com:443"},
		{"https preserved", "https://x:8780", "example.com", 8443, "example.com", 8443, "https://example.com:8443"},
		{"port zero no-op", "http://127.0.0.1:8780", "", 0, "127.0.0.1", 8780, "http://127.0.0.1:8780"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := serverconfig.DefaultServerConfig()
			cfg.Server.Host = "127.0.0.1"
			cfg.Server.Port = 8780
			cfg.Server.PublicBaseURL = tc.startURL
			applyServerOverrides(cfg, tc.host, tc.port)
			if cfg.Server.Host != tc.wantHost || cfg.Server.Port != tc.wantPort {
				t.Fatalf("host/port = %s:%d, want %s:%d", cfg.Server.Host, cfg.Server.Port, tc.wantHost, tc.wantPort)
			}
			if cfg.Server.PublicBaseURL != tc.wantPublic {
				t.Fatalf("public_base_url = %s, want %s", cfg.Server.PublicBaseURL, tc.wantPublic)
			}
		})
	}
}

func TestControlPlaneConfig(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	if c := controlPlaneConfig(cfg); string(c.Backend) != "memory" || c.DatabaseURL != "" {
		t.Fatalf("default cp config: %+v", c)
	}
	url := "file:test.db"
	cfg.ControlPlane.DatabaseURL = &url
	if c := controlPlaneConfig(cfg); c.DatabaseURL != url {
		t.Fatalf("db url not mapped: %+v", c)
	}
}

func TestBuildAuthenticatorLocal(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	cfg.Auth.Mode = "jwt"
	cfg.Auth.IdentityProvider = "local"
	auth, devToken, err := buildAuthenticator(cfg, serverauth.NewApiKeyStore())
	if err != nil || auth == nil {
		t.Fatalf("local: %v %v", auth, err)
	}
	if devToken != "" {
		t.Fatalf("local should not mint a dev token, got %q", devToken)
	}
	if _, ok := auth.(*serverauth.LocalIdentityProvider); !ok {
		t.Fatalf("want LocalIdentityProvider, got %T", auth)
	}
}

func TestBuildAuthenticatorDevToken(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig() // default mode is dev_token
	auth, devToken, err := buildAuthenticator(cfg, serverauth.NewApiKeyStore())
	if err != nil || auth == nil {
		t.Fatalf("dev_token: %v %v", auth, err)
	}
	if devToken == "" {
		t.Fatal("dev_token mode should mint a token")
	}
	if cfg.Auth.Mode != "jwt" {
		t.Fatalf("SetupDevIDP should mutate mode to jwt, got %q", cfg.Auth.Mode)
	}
}

func TestBuildAuthenticatorWebhook(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	cfg.Auth.Mode = "jwt"
	cfg.Auth.IdentityProvider = "webhook"
	url := "https://idp.example.com/verify"
	cfg.Auth.WebhookIDPURL = &url
	cfg.Auth.WebhookIDPForwardHeaders = []string{"X-Extra"}
	cfg.Auth.WebhookIDPForwardCookies = []string{"extra_cookie"}

	auth, _, err := buildAuthenticator(cfg, serverauth.NewApiKeyStore())
	if err != nil {
		t.Fatalf("webhook: %v", err)
	}
	if _, ok := auth.(*serverauth.WebhookIdentityProvider); !ok {
		t.Fatalf("want WebhookIdentityProvider, got %T", auth)
	}

	// Invalid on_failure → constructor error.
	cfg.Auth.WebhookIDPOnFailure = "bogus"
	if _, _, err := buildAuthenticator(cfg, serverauth.NewApiKeyStore()); err == nil {
		t.Fatal("expected webhook constructor error")
	}
}

func TestWebhookForwardSets(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	cfg.Auth.WebhookIDPForwardHeaders = []string{"X-Foo"}
	cfg.Auth.WebhookIDPForwardCookies = []string{"c1"}
	h := webhookForwardHeaders(cfg)
	if !h.Has("authorization") || !h.Has("x-foo") {
		t.Fatalf("forward headers missing entries: %v", h.Sorted())
	}
	c := webhookForwardCookies(cfg)
	if !c.Has("c1") || !c.Has(cfg.Auth.TokenCookie) {
		t.Fatalf("forward cookies missing entries: %v", c.Sorted())
	}
}

// writeTempConfig writes a minimal loopback config with a telnet session.
func writeTempConfig(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "server.toml")
	body := `
[server]
host = "127.0.0.1"
port = 0

[auth]
mode = "dev_token"

[[sessions]]
session_id = "s-telnet"
connector_type = "telnet"
host = "127.0.0.1"
port = 2323
`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	return path
}

func TestBuildServerAndServeHealth(t *testing.T) {
	path := writeTempConfig(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	bundle, err := buildServer(ctx, path, "", 0, "")
	if err != nil {
		t.Fatalf("buildServer: %v", err)
	}
	defer func() { _ = bundle.engine.Close(context.Background()) }()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	serveErr := make(chan error, 1)
	go func() { serveErr <- bundle.srv.Serve(ctx, ln) }()

	url := "http://" + ln.Addr().String() + "/api/health"
	var resp *http.Response
	for i := 0; i < 50; i++ {
		resp, err = http.Get(url) //nolint:noctx // test
		if err == nil {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err != nil {
		t.Fatalf("health request: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("health status = %d, want 200", resp.StatusCode)
	}

	cancel()
	if err := <-serveErr; err != nil {
		t.Fatalf("serve returned: %v", err)
	}
}

func TestBuildServerConfigError(t *testing.T) {
	if _, err := buildServer(context.Background(), "/no/such/config.toml", "", 0, ""); err == nil {
		t.Fatal("expected config load error")
	}
}

func TestRunServerLifecycle(t *testing.T) {
	path := writeTempConfig(t)
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(150 * time.Millisecond)
		cancel()
	}()
	if err := runServer(ctx, path, "", 0, ""); err != nil {
		t.Fatalf("runServer: %v", err)
	}
}

func TestRunServerBuildError(t *testing.T) {
	if err := runServer(context.Background(), "/no/such.toml", "", 0, ""); err == nil {
		t.Fatal("expected error from bad config")
	}
}
