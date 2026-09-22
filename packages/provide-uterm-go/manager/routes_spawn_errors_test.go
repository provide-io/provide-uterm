//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"bytes"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// spawnErrorServer is a manager whose log is recorded, so a test can assert
// that what a response leaves out is still written down server-side.
func spawnErrorServer(t *testing.T, spawnCmd SpawnCommandFunc) (*Server, *AgentManager, *bytes.Buffer) {
	t.Helper()
	m := testManager(t, spawnCmd)
	var logs bytes.Buffer
	m.logger = slog.New(slog.NewTextHandler(&logs, nil))
	return &Server{M: m, getenv: func(string) string { return "" }}, m, &logs
}

// postBody sends a request and returns the status and the raw body text.
func postBody(h http.Handler, target, body string) (int, string) {
	req := httptest.NewRequest("POST", target, strings.NewReader(body))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec.Code, rec.Body.String()
}

func assertOnlyError(t *testing.T, code int, body, want string, leaked ...string) {
	t.Helper()
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d %s", code, body)
	}
	if strings.TrimSpace(body) != `{"error":"`+want+`"}` {
		t.Fatalf("body = %s, want {\"error\": %q}", body, want)
	}
	for _, s := range leaked {
		if s != "" && strings.Contains(body, s) {
			t.Fatalf("body leaks %q: %s", s, body)
		}
	}
}

func TestSpawnConfigPathRefusalsNameTheRuleNotTheSandbox(t *testing.T) {
	s, m, _ := spawnErrorServer(t, sleepCmd("30"))
	h := s.Routes()
	base := m.Config.SpawnConfigDir
	outside := filepath.Join(t.TempDir(), "secret-agent.yaml")
	txt := filepath.Join(base, "agent.txt")

	for _, route := range []string{"single", "batch"} {
		send := func(path string) (int, string) {
			if route == "single" {
				return postBody(h, "/swarm/spawn?config_path="+url.QueryEscape(path), "")
			}
			return postBody(h, "/swarm/spawn-batch", `{"config_paths":["`+filepath.ToSlash(path)+`"]}`)
		}
		code, body := send(outside)
		assertOnlyError(t, code, body, "config_path is outside the spawn config dir",
			base, filepath.ToSlash(base), "secret-agent")
		code, body = send(txt)
		assertOnlyError(t, code, body, "config_path must be a .yaml or .yml file", "agent.txt")
	}
}

func TestSpawnWithNoSandboxConfiguredSaysSo(t *testing.T) {
	s, m, _ := spawnErrorServer(t, sleepCmd("30"))
	m.Config.SpawnConfigDir = ""
	code, body := postBody(s.Routes(), "/swarm/spawn?config_path=/x/agent.yaml", "")
	assertOnlyError(t, code, body, "config dir is not configured; refusing to spawn from an unrestricted path")
}

func TestSpawnFailureIsLoggedNotReturned(t *testing.T) {
	// The launch fails on a binary that does not exist; its error names the
	// path, and so does "Config not found" / "Max agents reached" — none of
	// which the caller gets.
	missing := filepath.Join(t.TempDir(), "no-such-worker-binary")
	s, m, logs := spawnErrorServer(t, func(_, _, _ string) []string { return []string{missing} })
	h := s.Routes()
	cfg := writeConfig(t, m, "agent.yaml")

	code, body := postBody(h, "/swarm/spawn?config_path="+url.QueryEscape(cfg)+"&agent_id=agent_007", "")
	assertOnlyError(t, code, body, "agent spawn failed; the manager log has the cause",
		"no-such-worker-binary", m.Config.SpawnConfigDir, "Failed to spawn")
	if !strings.Contains(logs.String(), "swarm_spawn_failed") || !strings.Contains(logs.String(), "agent_007") {
		t.Fatalf("spawn failure not logged: %s", logs.String())
	}

	// A config that passes the sandbox check but is gone by spawn time.
	// Resolved first: a missing leaf is not symlink-resolved, and on macOS the
	// temp dir sits behind /var -> /private/var.
	gone := filepath.Join(realpath(m.Config.SpawnConfigDir), "gone.yaml")
	code, body = postBody(h, "/swarm/spawn?config_path="+url.QueryEscape(gone), "")
	assertOnlyError(t, code, body, "agent spawn failed; the manager log has the cause", "gone.yaml", "Config not found")

	// At the fleet limit the reason is only in the log.
	m.MaxAgents = 0
	code, body = postBody(h, "/swarm/spawn?config_path="+url.QueryEscape(cfg), "")
	assertOnlyError(t, code, body, "agent spawn failed; the manager log has the cause", "Max agents")
	if !strings.Contains(logs.String(), "Max agents (0) reached") {
		t.Fatalf("max-agents refusal not logged: %s", logs.String())
	}
}

func TestSpawnConfigPathErrorKeepsTheDetailForTheInProcessTool(t *testing.T) {
	// The MCP tool runs in the operator's own process, as the reference's
	// does, and keeps the full detail; only the HTTP routes trim it.
	m := testManager(t, nil)
	outside := filepath.Join(t.TempDir(), "x.yaml")
	if err := os.WriteFile(outside, []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	got := NewManagerTools(m).SwarmSpawnBatch([]string{outside}, 1, 0, "random", "")
	msg, _ := got["error"].(string)
	if !strings.Contains(msg, "outside config dir") || !strings.Contains(msg, outside) {
		t.Fatalf("tool error = %q", msg)
	}
}
