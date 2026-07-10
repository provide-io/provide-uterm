//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDefaultServerConfig(t *testing.T) {
	c := DefaultServerConfig()
	if c.Server.PublicBaseURL != "http://127.0.0.1:8780" {
		t.Errorf("public_base_url = %q", c.Server.PublicBaseURL)
	}
	if c.Auth.Mode != "dev_token" {
		t.Errorf("auth.mode = %q, want dev_token", c.Auth.Mode)
	}
	if c.ControlPlane.Backend != "memory" || c.ControlPlane.DatabaseURL != nil {
		t.Errorf("control_plane defaults wrong: %+v", c.ControlPlane)
	}
	if len(c.Sessions) != 1 || c.Sessions[0].SessionID != "provide-shell" || c.Sessions[0].ConnectorType != "shell" {
		t.Errorf("default sessions wrong: %+v", c.Sessions)
	}
	if c.Environment != "production" {
		t.Errorf("environment = %q", c.Environment)
	}
	if c.MaxWorkers != 10000 || c.MaxConnectionsPerPrincipal != 25 || c.BrowserRateLimitPerSec != 300 {
		t.Errorf("top scalar defaults wrong")
	}
}

func mustConfig(t *testing.T, data map[string]any) *UtermServerConfig {
	t.Helper()
	c, err := ConfigFromMapping(data)
	if err != nil {
		t.Fatalf("ConfigFromMapping error: %v", err)
	}
	return c
}

func TestConfigFromMappingSessionsAndPaths(t *testing.T) {
	c := mustConfig(t, map[string]any{
		"server":    map[string]any{"host": "0.0.0.0", "port": int64(9001), "public_base_url": "http://127.0.0.1:9001"},
		"ui":        map[string]any{"app_path": "ops", "assets_path": "assets"},
		"recording": map[string]any{"enabled_by_default": true, "directory": "logs"},
		"sessions": []any{map[string]any{
			"session_id": "bbs", "display_name": "Public BBS", "connector_type": "telnet",
			"input_mode": "hijack", "host": "bbs.example.com", "port": int64(23), "tags": []any{"public", "bbs"},
		}},
	})
	if c.Server.Host != "0.0.0.0" || c.Server.Port != 9001 {
		t.Errorf("server wrong: %+v", c.Server)
	}
	if c.UI.AppPath != "/ops" || c.UI.AssetsPath != "/assets" {
		t.Errorf("ui paths wrong: %+v", c.UI)
	}
	if !c.Recording.EnabledByDefault || c.Recording.Directory != "logs" {
		t.Errorf("recording wrong: %+v", c.Recording)
	}
	if len(c.Sessions) != 1 {
		t.Fatalf("sessions len = %d", len(c.Sessions))
	}
	s := c.Sessions[0]
	if s.ConnectorConfig["host"] != "bbs.example.com" {
		t.Errorf("connector_config host = %v", s.ConnectorConfig["host"])
	}
	if p, _ := s.ConnectorConfig["port"].(int64); p != 23 {
		t.Errorf("connector_config port = %v", s.ConnectorConfig["port"])
	}
}

func TestPartialOverridesPreserveDefaults(t *testing.T) {
	c := mustConfig(t, map[string]any{"auth": map[string]any{"principal_header": "x-user"}})
	if c.Auth.Mode != "dev_token" || c.Auth.PrincipalHeader != "x-user" {
		t.Errorf("auth partial override lost defaults: %+v", c.Auth)
	}
	// Parity with Python: the default's already-derived public_base_url is carried
	// through the merge, so overriding only the port keeps the default URL.
	c2 := mustConfig(t, map[string]any{"server": map[string]any{"port": int64(9999)}})
	if c2.Server.Port != 9999 || c2.Server.Host != "127.0.0.1" || c2.Server.PublicBaseURL != "http://127.0.0.1:8780" {
		t.Errorf("server partial override wrong: %+v", c2.Server)
	}
	c3 := mustConfig(t, map[string]any{"control_plane": map[string]any{"database_url": "sqlite+aiosqlite:///tmp/cp.db"}})
	if c3.ControlPlane.Backend != "memory" || c3.ControlPlane.DatabaseURL == nil {
		t.Errorf("control_plane partial override wrong: %+v", c3.ControlPlane)
	}
}

func TestConfigFromMappingErrors(t *testing.T) {
	cases := []struct {
		name string
		data map[string]any
		want string
	}{
		{"sqlite needs db", map[string]any{"control_plane": map[string]any{"backend": "sqlite"}}, "control_plane.database_url"},
		{"reap interval", map[string]any{"control_plane": map[string]any{"reap_interval_s": int64(0)}}, "reap_interval_s must be > 0"},
		{"reap retention", map[string]any{"control_plane": map[string]any{"reap_retention_s": int64(-1)}}, "reap_retention_s must be >= 0"},
		{"tunnel ttl", map[string]any{"tunnel": map[string]any{"token_ttl_s": int64(59)}}, "token_ttl_s must be >= 60"},
		{"max_bytes", map[string]any{"recording": map[string]any{"max_bytes": int64(-1)}}, "max_bytes"},
		{"retention_s", map[string]any{"recording": map[string]any{"retention_s": int64(-1)}}, "retention_s must be >= 0"},
		{"ctrl channel mode", map[string]any{"recording": map[string]any{"control_channel_mode": "bogus"}}, "exclude"},
		{"unknown section", map[string]any{"bogus": map[string]any{"x": int64(1)}}, "Extra inputs are not permitted"},
		{"unknown nested", map[string]any{"server": map[string]any{"host": "127.0.0.1", "bogus": true}}, "Extra inputs are not permitted"},
		{"non-dict section", map[string]any{"server": []any{}}, "[server] must be a table (got list)"},
		{"empty session_id", map[string]any{"sessions": []any{map[string]any{"session_id": "", "connector_type": "shell"}}}, "session_id is required"},
		{"bad session_id", map[string]any{"sessions": []any{map[string]any{"session_id": "bad id!", "connector_type": "shell"}}}, "session_id must match"},
		{"bad input_mode", map[string]any{"sessions": []any{map[string]any{"session_id": "s1", "connector_type": "shell", "input_mode": "bad"}}}, "invalid input_mode"},
		{"bad visibility", map[string]any{"sessions": []any{map[string]any{"session_id": "s1", "connector_type": "shell", "visibility": "test"}}}, "invalid visibility"},
		{"proxy secret", map[string]any{"auth": map[string]any{"require_upstream_proxy_secret": true}}, "upstream_proxy_secret is required"},
		{"webhook http routable", map[string]any{"auth": map[string]any{"webhook_idp_url": "http://evil.example.com/x"}}, "must use https://"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ConfigFromMapping(tc.data)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got err=%v, want contains %q", err, tc.want)
			}
		})
	}
}

func TestNonDictSectionMentionsType(t *testing.T) {
	_, err := ConfigFromMapping(map[string]any{"server": []any{"a"}})
	if err == nil || !strings.Contains(err.Error(), "list") || strings.Contains(err.Error(), "NoneType") {
		t.Fatalf("got %v", err)
	}
}

func TestEmptySessionsClearsDefault(t *testing.T) {
	c := mustConfig(t, map[string]any{"sessions": []any{}})
	if len(c.Sessions) != 0 {
		t.Fatalf("sessions = %+v, want empty", c.Sessions)
	}
}

func TestNonDictSessionEntrySkipped(t *testing.T) {
	c := mustConfig(t, map[string]any{"sessions": []any{"not-a-dict", map[string]any{"session_id": "s1", "connector_type": "shell"}}})
	if len(c.Sessions) != 1 || c.Sessions[0].SessionID != "s1" {
		t.Fatalf("sessions = %+v", c.Sessions)
	}
}

func TestSessionCollectsUnknownFields(t *testing.T) {
	c := mustConfig(t, map[string]any{"sessions": []any{map[string]any{"session_id": "bbs", "connector_type": "telnet", "host": "bbs.example.com"}}})
	s := c.Sessions[0]
	if s.DisplayName != "bbs" {
		t.Errorf("display_name = %q, want bbs", s.DisplayName)
	}
	if s.ConnectorConfig["host"] != "bbs.example.com" {
		t.Errorf("connector_config = %v", s.ConnectorConfig)
	}
}

func TestEphemeralNotInConnectorConfig(t *testing.T) {
	c := mustConfig(t, map[string]any{"sessions": []any{map[string]any{"session_id": "e", "connector_type": "shell", "ephemeral": true}}})
	s := c.Sessions[0]
	if !s.Ephemeral {
		t.Errorf("ephemeral not set")
	}
	if _, present := s.ConnectorConfig["ephemeral"]; present {
		t.Errorf("ephemeral leaked into connector_config")
	}
}

func TestServerBindDerivesURL(t *testing.T) {
	s := ServerBindConfig{Host: "10.0.0.1", Port: 9090}
	deriveServerURL(&s)
	if s.PublicBaseURL != "http://10.0.0.1:9090" {
		t.Errorf("derived = %q", s.PublicBaseURL)
	}
	explicit := ServerBindConfig{Host: "10.0.0.1", Port: 9090, PublicBaseURL: "https://proxy.example.com"}
	deriveServerURL(&explicit)
	if explicit.PublicBaseURL != "https://proxy.example.com" {
		t.Errorf("explicit url overwritten: %q", explicit.PublicBaseURL)
	}
}

func TestCleanPath(t *testing.T) {
	cases := map[[2]string]string{
		{"admin", "/fallback"}:    "/admin",
		{"/admin/", "/fallback"}:  "/admin",
		{"/adminX/", "/fallback"}: "/adminX",
		{"", "/fallback"}:         "/fallback",
		{"///", "/fallback"}:      "/",
	}
	for in, want := range cases {
		if got := cleanPath(in[0], in[1]); got != want {
			t.Errorf("cleanPath(%q,%q) = %q, want %q", in[0], in[1], got, want)
		}
	}
}

func TestRealTOMLFilesLoad(t *testing.T) {
	repoRoot := filepath.Join("..", "..", "..")

	docker, err := LoadServerConfig(filepath.Join(repoRoot, "docker", "server.toml"))
	if err != nil {
		t.Fatalf("docker/server.toml load: %v", err)
	}
	if docker.Server.Host != "0.0.0.0" || docker.Server.Port != 27780 {
		t.Errorf("docker server bind wrong: %+v", docker.Server)
	}
	if docker.Auth.Mode != "jwt" || !docker.Auth.RequireJWTInProduction {
		t.Errorf("docker auth wrong: mode=%q", docker.Auth.Mode)
	}
	if len(docker.Auth.JWTAlgorithms) != 1 || docker.Auth.JWTAlgorithms[0] != "RS256" {
		t.Errorf("docker jwt_algorithms wrong: %v", docker.Auth.JWTAlgorithms)
	}
	if len(docker.Sessions) != 1 || docker.Sessions[0].SessionID != "shell-demo" {
		t.Errorf("docker sessions wrong: %+v", docker.Sessions)
	}
	// recording.directory is absolute in docker (/tmp/uterm-recordings) so it is kept.
	if docker.Recording.Directory != "/tmp/uterm-recordings" {
		t.Errorf("docker recording dir = %q", docker.Recording.Directory)
	}

	example, err := LoadServerConfig(filepath.Join(repoRoot, "scripts", "uterm-server.example.toml"))
	if err != nil {
		t.Fatalf("example toml load: %v", err)
	}
	if example.Server.Host != "127.0.0.1" || example.Server.Port != 8780 {
		t.Errorf("example server bind wrong: %+v", example.Server)
	}
	if example.Auth.Mode != "dev" {
		t.Errorf("example auth.mode = %q, want dev (string, accepted at config layer)", example.Auth.Mode)
	}
	if len(example.Sessions) != 3 {
		t.Fatalf("example sessions len = %d, want 3", len(example.Sessions))
	}
	// ops-shell ssh session carries host/port/username in connector_config.
	var ops *SessionDefinition
	for i := range example.Sessions {
		if example.Sessions[i].SessionID == "ops-shell" {
			ops = &example.Sessions[i]
		}
	}
	if ops == nil {
		t.Fatal("ops-shell session missing")
	}
	if ops.ConnectorType != "ssh" || ops.ConnectorConfig["username"] != "operator" {
		t.Errorf("ops-shell wrong: %+v", ops)
	}
	// recording.directory ".uterm-recordings" resolves against the config dir.
	if !filepath.IsAbs(example.Recording.Directory) || !strings.HasSuffix(example.Recording.Directory, ".uterm-recordings") {
		t.Errorf("example recording dir not resolved: %q", example.Recording.Directory)
	}
}

func TestLoadResolvesRelativeRecordingPath(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "server.toml")
	content := "[recording]\ndirectory = \"logs\"\n\n[[sessions]]\nsession_id = \"provide-shell\"\nconnector_type = \"shell\"\n"
	if err := os.WriteFile(cfgPath, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := LoadServerConfig(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(dir, "logs")
	if c.Recording.Directory != want {
		t.Errorf("recording dir = %q, want %q", c.Recording.Directory, want)
	}
}
