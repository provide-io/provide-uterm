//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// --- config ---

func TestDefaultConfig(t *testing.T) {
	cfg := DefaultManagerConfig()
	if cfg.Host != "127.0.0.1" || cfg.Port != 2272 || cfg.MaxAgents != 200 {
		t.Fatalf("unexpected defaults: %+v", cfg)
	}
	if cfg.AuthTokenEnvVar != "UTERM_MANAGER_API_TOKEN" {
		t.Fatalf("token env var = %q", cfg.AuthTokenEnvVar)
	}
	if len(cfg.CORSOrigins) != 1 {
		t.Fatalf("cors = %v", cfg.CORSOrigins)
	}
}

// --- models ---

func TestAgentStatusJSONShape(t *testing.T) {
	a := newAgentStatus("agent_001")
	b, _ := json.Marshal(a)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	if m["state"] != "unknown" {
		t.Fatalf("state = %v", m["state"])
	}
	if m["session_id"] != nil {
		t.Fatalf("session_id should be null, got %v", m["session_id"])
	}
	if _, ok := m["recent_actions"].([]any); !ok {
		t.Fatalf("recent_actions should be [], got %v", m["recent_actions"])
	}
	if _, ok := m["pending_command_payload"].(map[string]any); !ok {
		t.Fatalf("pending_command_payload should be {}, got %v", m["pending_command_payload"])
	}
}

func TestAgentStatusFromMapExtra(t *testing.T) {
	a := agentStatusFromMap(map[string]any{
		"agent_id": "agent_002",
		"state":    "running",
		"credits":  1234.0,
	})
	if a.State != "running" {
		t.Fatalf("state = %q", a.State)
	}
	if a.Extra["credits"] != 1234.0 {
		t.Fatalf("extra credits = %v", a.Extra["credits"])
	}
	b, _ := json.Marshal(a)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	if m["credits"] != 1234.0 {
		t.Fatalf("marshalled extra = %v", m["credits"])
	}
}

// --- ext / policy gates ---

func TestNoOpGateAllows(t *testing.T) {
	if !(NoOpAgentSpawnPolicyGate{}).InterceptSpawn(context.Background(), "a", "c", nil) {
		t.Fatal("noop gate should allow")
	}
}

func TestWebhookGateAllowDeny(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Signature") == "" {
			t.Error("missing signature")
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"allow": true})
	}))
	defer srv.Close()
	gate := NewWebhookAgentSpawnPolicyGate(srv.URL, "sec", 2.0)
	if !gate.InterceptSpawn(context.Background(), "a", "c.yaml", map[string]any{"k": "v"}) {
		t.Fatal("expected allow")
	}

	deny := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(403)
	}))
	defer deny.Close()
	gate2 := NewWebhookAgentSpawnPolicyGate(deny.URL, "", 2.0)
	if gate2.InterceptSpawn(context.Background(), "a", "c.yaml", nil) {
		t.Fatal("expected deny on 403")
	}
	// Unreachable URL → deny.
	gate3 := NewWebhookAgentSpawnPolicyGate("http://127.0.0.1:0", "", 1.0)
	if gate3.InterceptSpawn(context.Background(), "a", "c.yaml", nil) {
		t.Fatal("expected deny on error")
	}
}

func TestDeriveAgentTokenStable(t *testing.T) {
	got := deriveAgentToken("secret", "agent_001")
	if len(got) != len("sha256=")+64 {
		t.Fatalf("token shape = %q", got)
	}
	if deriveAgentToken("secret", "agent_001") != got {
		t.Fatal("derivation must be deterministic")
	}
	if deriveAgentToken("secret", "agent_002") == got {
		t.Fatal("different agent must yield different token")
	}
}

// --- validateConfigPath ---

func TestValidateConfigPath(t *testing.T) {
	dir := t.TempDir()
	good := filepath.Join(dir, "a.yaml")
	_ = os.WriteFile(good, []byte("{}"), 0o644)
	if _, err := validateConfigPath(good, dir, func(string) string { return "" }); err != nil {
		t.Fatalf("valid path errored: %v", err)
	}
	// Wrong suffix.
	txt := filepath.Join(dir, "a.txt")
	_ = os.WriteFile(txt, []byte("{}"), 0o644)
	if _, err := validateConfigPath(txt, dir, func(string) string { return "" }); err == nil {
		t.Fatal("expected suffix error")
	}
	// Outside dir.
	if _, err := validateConfigPath("/etc/passwd.yaml", dir, func(string) string { return "" }); err == nil {
		t.Fatal("expected outside-dir error")
	}
	// No config dir configured.
	if _, err := validateConfigPath(good, "", func(string) string { return "" }); err == nil {
		t.Fatal("expected unconfigured error")
	}
	// Config dir from env.
	if _, err := validateConfigPath(good, "", func(k string) string {
		if k == ConfigDirEnvVar {
			return dir
		}
		return ""
	}); err != nil {
		t.Fatalf("env config dir errored: %v", err)
	}
}

// --- mcp tools ---

func TestManagerTools(t *testing.T) {
	m := testManager(t, nil)
	tools := NewManagerTools(m)
	m.Agents["agent_001"] = newAgentStatus("agent_001")

	// toMap() JSON-round-trips, so numeric fields come back as float64
	// (both serialize to the same JSON number the MCP client sees).
	if got := tools.SwarmStatus(false); got["total_agents"] != float64(1) {
		t.Fatalf("swarm status total = %v (%T)", got["total_agents"], got["total_agents"])
	}
	if got := tools.AgentList(""); int(got["total"].(int)) != 1 {
		t.Fatalf("agent list total = %v", got["total"])
	}
	if got := tools.AgentStatus("agent_001"); got["agent_id"] != "agent_001" {
		t.Fatalf("agent status = %v", got)
	}
	if got := tools.AgentStatus("nope"); got["error"] == nil {
		t.Fatal("expected error for missing agent")
	}
	if got := tools.AgentPause("agent_001"); got["paused"] != true {
		t.Fatalf("pause = %v", got)
	}
	if got := tools.AgentResume("agent_001"); got["paused"] != false {
		t.Fatalf("resume = %v", got)
	}
	if got := tools.AgentRestart("agent_001"); got["queued"] != true {
		t.Fatalf("restart = %v", got)
	}
	if got := tools.AgentKill("agent_001"); got["action"] != "kill" {
		t.Fatalf("kill = %v", got)
	}
	if got := tools.SwarmSetDesired(5); int(got["desired_agents"].(int)) != 5 {
		t.Fatalf("set desired = %v", got)
	}
	_ = tools.SwarmPause()
	_ = tools.SwarmResume()
	_ = tools.SwarmKillAll()
	_ = tools.SwarmClear()
	_ = tools.SwarmPrune()
	if TOOLCount != 15 {
		t.Fatalf("TOOLCount = %d", TOOLCount)
	}
}

func TestManagerToolsEvents(t *testing.T) {
	m := testManager(t, nil)
	tools := NewManagerTools(m)
	a := newAgentStatus("agent_001")
	a.ErrorMessage = strPtr("boom")
	a.RecentActions = []map[string]any{{"action": "MOVE"}}
	m.Agents["agent_001"] = a
	got := tools.AgentEvents("agent_001")
	evs := got["events"].([]map[string]any)
	if len(evs) != 2 {
		t.Fatalf("events = %d", len(evs))
	}
	if tools.AgentEvents("nope")["error"] == nil {
		t.Fatal("expected error for missing agent events")
	}
}
