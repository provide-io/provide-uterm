//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"net/http"
	"testing"
)

func newTestServer(t *testing.T, spawnCmd SpawnCommandFunc) (*Server, http.Handler) {
	t.Helper()
	m := testManager(t, spawnCmd)
	s := &Server{M: m, getenv: func(string) string { return "" }}
	return s, s.Routes()
}

func TestRouteHealthAndStatus(t *testing.T) {
	_, h := newTestServer(t, nil)
	if code, body := doJSON(t, h, "GET", "/health", ""); code != 200 || body["status"] != "ok" {
		t.Fatalf("health: %d %v", code, body)
	}
	if code, body := doJSON(t, h, "GET", "/swarm/status", ""); code != 200 || body["total_agents"] == nil {
		t.Fatalf("status: %d %v", code, body)
	}
	if code, _ := doJSON(t, h, "GET", "/swarm/timeseries/info", ""); code != 200 {
		t.Fatalf("ts info: %d", code)
	}
	if code, _ := doJSON(t, h, "GET", "/swarm/timeseries/recent?limit=10", ""); code != 200 {
		t.Fatalf("ts recent: %d", code)
	}
	if code, body := doJSON(t, h, "GET", "/swarm/timeseries/summary", ""); code != 200 || body["error"] == nil {
		t.Fatalf("ts summary: %d %v", code, body)
	}
}

func TestRouteListAgents(t *testing.T) {
	s, h := newTestServer(t, nil)
	a := newAgentStatus("agent_001")
	a.State = "running"
	a.SessionID = strPtr("sess")
	a.Config = strPtr("mcp://x")
	s.M.Agents["agent_001"] = a
	code, body := doJSON(t, h, "GET", "/agents?interactive_only=true", "")
	if code != 200 {
		t.Fatalf("code %d", code)
	}
	if int(body["total"].(float64)) != 1 {
		t.Fatalf("total = %v", body["total"])
	}
	// State filter that excludes.
	_, body2 := doJSON(t, h, "GET", "/agents?state=stopped", "")
	if int(body2["total"].(float64)) != 0 {
		t.Fatalf("filtered total = %v", body2["total"])
	}
}

func TestRouteRegister(t *testing.T) {
	s, h := newTestServer(t, nil)
	// Create.
	code, body := doJSON(t, h, "POST", "/agent/agent_001/register", `{"state":"running"}`)
	if code != 200 || body["created"] != true {
		t.Fatalf("register: %d %v", code, body)
	}
	// Operator field rejection.
	code, _ = doJSON(t, h, "POST", "/agent/agent_001/register", `{"paused":true}`)
	if code != 422 {
		t.Fatalf("operator-field register: %d", code)
	}
	// Invalid agent_id.
	code, _ = doJSON(t, h, "POST", "/agent/bad$id/register", `{}`)
	if code != 404 && code != 422 {
		// bad$id may not match the {agent_id} literal segment; either not-found
		// or 422 is acceptable rejection.
		t.Fatalf("invalid id register: %d", code)
	}
	// Max agents.
	s.M.MaxAgents = 1
	code, _ = doJSON(t, h, "POST", "/agent/agent_777/register", `{}`)
	if code != 429 {
		t.Fatalf("max agents register: %d", code)
	}
}

func TestRouteUpdateStatus(t *testing.T) {
	s, h := newTestServer(t, nil)
	// Auto-create.
	code, body := doJSON(t, h, "POST", "/agent/agent_001/status", `{"state":"running","pid":42}`)
	if code != 200 || body["ok"] != true {
		t.Fatalf("status: %d %v", code, body)
	}
	// Queue a command, then a status poll returns it.
	s.M.mu.Lock()
	queueManagerCommand(s.M.Agents["agent_001"], "set_goal", map[string]any{"goal": "x"})
	s.M.mu.Unlock()
	_, body = doJSON(t, h, "POST", "/agent/agent_001/status", `{"state":"running"}`)
	if body["manager_command"] == nil {
		t.Fatalf("expected manager_command, got %v", body)
	}
	// Stale report ignored.
	s.M.mu.Lock()
	s.M.Agents["agent_001"].StatusReportedAt = floatPtr(1000)
	s.M.mu.Unlock()
	_, body = doJSON(t, h, "POST", "/agent/agent_001/status", `{"reported_at":5}`)
	if body["ignored"] != "stale_report" {
		t.Fatalf("expected stale ignore, got %v", body)
	}
}

func TestRouteAgentControl(t *testing.T) {
	s, h := newTestServer(t, nil)
	s.M.Agents["agent_001"] = newAgentStatus("agent_001")
	for _, action := range []string{"pause", "resume", "restart"} {
		code, body := doJSON(t, h, "POST", "/agent/agent_001/"+action, "")
		if code != 200 || body["action"] != action {
			t.Fatalf("%s: %d %v", action, code, body)
		}
	}
	// 404 on missing agent.
	code, _ := doJSON(t, h, "POST", "/agent/nope/pause", "")
	if code != 404 {
		t.Fatalf("missing pause: %d", code)
	}
}

func TestRouteSetGoalDirectiveCancel(t *testing.T) {
	s, h := newTestServer(t, nil)
	s.M.Agents["agent_001"] = newAgentStatus("agent_001")
	if code, body := doJSON(t, h, "POST", "/agent/agent_001/set-goal?goal=win", ""); code != 200 || body["action"] != "set_goal" {
		t.Fatalf("set-goal: %d %v", code, body)
	}
	if code, body := doJSON(t, h, "POST", "/agent/agent_001/set-directive", `{"directive":"go","turns":5}`); code != 200 || body["action"] != "set_directive" {
		t.Fatalf("set-directive: %d %v", code, body)
	}
	// Cancel the pending command.
	code, body := doJSON(t, h, "POST", "/agent/agent_001/cancel-command", "")
	if code != 200 || body["applied"] != true {
		t.Fatalf("cancel: %d %v", code, body)
	}
	// Cancel again → no pending.
	_, body = doJSON(t, h, "POST", "/agent/agent_001/cancel-command", "")
	res := body["result"].(map[string]any)
	if res["cancelled"] != false {
		t.Fatalf("expected no pending, got %v", res)
	}
}

func TestRouteDeleteAgent(t *testing.T) {
	s, h := newTestServer(t, nil)
	// 404.
	if code, _ := doJSON(t, h, "DELETE", "/agent/nope", ""); code != 404 {
		t.Fatalf("delete missing: %d", code)
	}
	// Terminal remove.
	a := newAgentStatus("agent_001")
	a.State = "completed"
	s.M.Agents["agent_001"] = a
	code, body := doJSON(t, h, "DELETE", "/agent/agent_001", "")
	if code != 200 || body["state"] != "removed" {
		t.Fatalf("delete terminal: %d %v", code, body)
	}
	if s.M.agentState("agent_001") != "" {
		t.Fatal("agent should be removed")
	}
}

func TestRouteDeleteRunningAgent(t *testing.T) {
	s, h := newTestServer(t, sleepCmd("30"))
	cfg := writeConfig(t, s.M, "agent.yaml")
	_, _ = s.M.SpawnAgent(context.Background(), cfg, "agent_001")
	s.M.mu.Lock()
	s.M.DesiredAgents = 2
	s.M.mu.Unlock()
	code, body := doJSON(t, h, "DELETE", "/agent/agent_001", "")
	if code != 200 {
		t.Fatalf("delete running: %d %v", code, body)
	}
	res := body["result"].(map[string]any)
	if res["killed"] != "agent_001" {
		t.Fatalf("expected killed, got %v", res)
	}
}

func TestRouteEvents(t *testing.T) {
	s, h := newTestServer(t, nil)
	if code, _ := doJSON(t, h, "GET", "/agent/nope/events", ""); code != 404 {
		t.Fatalf("events missing: %d", code)
	}
	a := newAgentStatus("agent_001")
	a.State = "error"
	a.ErrorMessage = strPtr("boom")
	a.ErrorType = strPtr("Kaboom")
	a.ErrorTimestamp = floatPtr(123)
	a.RecentActions = []map[string]any{{"time": 100.0, "action": "MOVE"}}
	s.M.Agents["agent_001"] = a
	code, body := doJSON(t, h, "GET", "/agent/agent_001/events", "")
	if code != 200 {
		t.Fatalf("events: %d", code)
	}
	evs := body["events"].([]any)
	if len(evs) != 2 {
		t.Fatalf("events len = %d", len(evs))
	}
}

func TestRouteSessionData503(t *testing.T) {
	_, h := newTestServer(t, nil)
	if code, _ := doJSON(t, h, "GET", "/agent/agent_001/session-data", ""); code != 503 {
		t.Fatalf("session-data: %d", code)
	}
}

func TestRouteSwarmControls(t *testing.T) {
	_, h := newTestServer(t, nil)
	for _, path := range []string{"/swarm/kill-all", "/swarm/clear", "/swarm/prune", "/swarm/pause", "/swarm/resume"} {
		if code, _ := doJSON(t, h, "POST", path, ""); code != 200 {
			t.Fatalf("%s: %d", path, code)
		}
	}
	if code, body := doJSON(t, h, "POST", "/swarm/bust-respawn", `{"enabled":true}`); code != 200 || body["bust_respawn"] != true {
		t.Fatalf("bust: %d %v", code, body)
	}
}

func TestRouteSetDesired(t *testing.T) {
	_, h := newTestServer(t, nil)
	if code, body := doJSON(t, h, "POST", "/swarm/desired", `{"count":3}`); code != 200 || int(body["desired_agents"].(float64)) != 3 {
		t.Fatalf("desired: %d %v", code, body)
	}
	if code, _ := doJSON(t, h, "POST", "/swarm/desired", `{"count":-1}`); code != 400 {
		t.Fatalf("negative desired: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/swarm/desired", `{"count":"x"}`); code != 400 {
		t.Fatalf("bad desired: %d", code)
	}
}

func TestRouteSpawnValidation(t *testing.T) {
	s, h := newTestServer(t, scriptCmd("exit 0"))
	// Bad path (outside sandbox).
	if code, _ := doJSON(t, h, "POST", "/swarm/spawn?config_path=/etc/passwd", ""); code != 400 {
		t.Fatalf("bad spawn: %d", code)
	}
	// Valid path.
	cfg := writeConfig(t, s.M, "agent.yaml")
	code, body := doJSON(t, h, "POST", "/swarm/spawn?config_path="+cfg, "")
	if code != 200 || body["agent_id"] == nil {
		t.Fatalf("spawn: %d %v", code, body)
	}
}

func TestRouteSpawnBatch(t *testing.T) {
	s, h := newTestServer(t, scriptCmd("exit 0"))
	// Empty config_paths → 422.
	if code, _ := doJSON(t, h, "POST", "/swarm/spawn-batch", `{"config_paths":[]}`); code != 422 {
		t.Fatalf("empty batch: %d", code)
	}
	cfg := writeConfig(t, s.M, "agent.yaml")
	code, body := doJSON(t, h, "POST", "/swarm/spawn-batch", `{"config_paths":["`+cfg+`"],"group_size":1}`)
	if code != 200 || body["status"] != "spawning" {
		t.Fatalf("batch: %d %v", code, body)
	}
	s.M.PM.CancelSpawn()
}
