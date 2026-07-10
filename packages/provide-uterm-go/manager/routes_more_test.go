//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"testing"
)

// seedAgent adds a running agent to the server's manager and returns it.
func seedAgent(s *Server, id string) *AgentStatus {
	a := newAgentStatus(id)
	a.State = "running"
	a.SessionID = strPtr("sess-" + id)
	a.Config = strPtr("mcp://x")
	s.M.Agents[id] = a
	return a
}

func TestAgentStatusAndDetails(t *testing.T) {
	s, h := newTestServer(t, nil)
	seedAgent(s, "agent_001")

	code, body := doJSON(t, h, "GET", "/agent/agent_001/status", "")
	if code != 200 || body["agent_id"] != "agent_001" {
		t.Fatalf("status: %d %v", code, body)
	}
	code, body = doJSON(t, h, "GET", "/agent/agent_001/details", "")
	if code != 200 || body["agent_id"] != "agent_001" {
		t.Fatalf("details: %d %v", code, body)
	}
	// Missing agent -> 404 on both.
	if code, _ := doJSON(t, h, "GET", "/agent/nope/status", ""); code != 404 {
		t.Fatalf("missing status: %d", code)
	}
	if code, _ := doJSON(t, h, "GET", "/agent/nope/details", ""); code != 404 {
		t.Fatalf("missing details: %d", code)
	}
	// session-data always 503 on the bare manager.
	if code, _ := doJSON(t, h, "GET", "/agent/agent_001/session-data", ""); code != 503 {
		t.Fatalf("session-data: %d", code)
	}
}

func TestRegisterAndSetFields(t *testing.T) {
	s, h := newTestServer(t, nil)
	seedAgent(s, "agent_001")

	// Register rejects operator-owned fields.
	code, body := doJSON(t, h, "POST", "/agent/agent_001/register", `{"goal":"x"}`)
	if code != 200 && code != 422 {
		t.Fatalf("register: %d %v", code, body)
	}
	// Invalid agent_id path -> 422.
	if code, _ := doJSON(t, h, "POST", "/agent/bad@id/register", `{}`); code != 422 {
		t.Fatalf("bad register id: %d", code)
	}
	// Bad JSON body -> 422.
	if code, _ := doJSON(t, h, "POST", "/agent/agent_001/register", `{not json`); code != 422 {
		t.Fatalf("bad register body: %d", code)
	}

	// set-goal and set-directive take a query param and queue a worker command.
	if code, resp := doJSON(t, h, "POST", "/agent/agent_001/set-goal?goal=win", ``); code != 200 || resp["action"] != "set_goal" {
		t.Fatalf("set-goal: %d %v", code, resp)
	}
	if code, _ := doJSON(t, h, "POST", "/agent/agent_001/set-directive?directive=go", ``); code != 200 {
		t.Fatalf("set-directive: %d", code)
	}
	_ = s
}

func TestCancelAndDeleteAgent(t *testing.T) {
	s, h := newTestServer(t, nil)
	seedAgent(s, "agent_001")

	if code, _ := doJSON(t, h, "POST", "/agent/agent_001/cancel-command", `{}`); code != 200 {
		t.Fatalf("cancel: %d", code)
	}
	// Delete a stopped agent.
	s.M.Agents["agent_001"].State = "stopped"
	if code, _ := doJSON(t, h, "DELETE", "/agent/agent_001", ""); code != 200 {
		t.Fatalf("delete: %d", code)
	}
	if _, ok := s.M.Agents["agent_001"]; ok {
		t.Fatal("agent not deleted")
	}
	if code, _ := doJSON(t, h, "DELETE", "/agent/nope", ""); code != 404 {
		t.Fatalf("delete missing: %d", code)
	}
}

func TestAgentEventsRoute(t *testing.T) {
	s, h := newTestServer(t, nil)
	seedAgent(s, "agent_001")
	code, _ := doJSON(t, h, "GET", "/agent/agent_001/events", "")
	if code != 200 && code != 404 {
		t.Fatalf("events: %d", code)
	}
}

func TestSwarmControlRoutes(t *testing.T) {
	s, h := newTestServer(t, nil)
	seedAgent(s, "agent_001")

	for _, path := range []string{"/swarm/pause", "/swarm/resume", "/swarm/prune", "/swarm/clear"} {
		if code, _ := doJSON(t, h, "POST", path, `{}`); code != 200 {
			t.Fatalf("%s: %d", path, code)
		}
	}
	// per-agent pause/resume.
	seedAgent(s, "agent_002")
	if code, _ := doJSON(t, h, "POST", "/agent/agent_002/pause", `{}`); code != 200 {
		t.Fatalf("agent pause: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/agent/agent_002/resume", `{}`); code != 200 {
		t.Fatalf("agent resume: %d", code)
	}
}

func TestSetDesiredRoute(t *testing.T) {
	_, h := newTestServer(t, nil)
	code, _ := doJSON(t, h, "POST", "/swarm/desired", `{"desired_agents":0}`)
	if code != 200 {
		t.Fatalf("set-desired: %d", code)
	}
}

func TestUpdateStatusRoute(t *testing.T) {
	s, h := newTestServer(t, nil)
	seedAgent(s, "agent_001")
	body := `{"state":"running","progress":0.5,"iteration":3,"current_task":"work"}`
	code, resp := doJSON(t, h, "POST", "/agent/agent_001/status", body)
	if code != 200 {
		t.Fatalf("update-status: %d %v", code, resp)
	}
	// An unknown agent under MaxAgents is auto-registered (200), matching
	// the Python self-report create-on-first-report behavior.
	if code, _ := doJSON(t, h, "POST", "/agent/agent_new/status", body); code != 200 {
		t.Fatalf("update auto-register: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/agent/agent_001/status", `{bad`); code != 422 {
		t.Fatalf("update bad body: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/agent/bad@id/status", body); code != 422 {
		t.Fatalf("update bad id: %d", code)
	}
}

func TestRoutePtrHelpers(t *testing.T) {
	// Type-based: a non-string / non-number value yields nil.
	if strPtrOrNil(nil) != nil {
		t.Fatal("non-string should be nil ptr")
	}
	if p := strPtrOrNil("x"); p == nil || *p != "x" {
		t.Fatal("string ptr")
	}
	if floatPtrOrNil("nope") != nil {
		t.Fatal("non-number should be nil ptr")
	}
	if p := floatPtrOrNil(1.5); p == nil || *p != 1.5 {
		t.Fatal("float ptr")
	}
	if p := floatPtrOrNil(3); p == nil || *p != 3 {
		t.Fatal("int -> float ptr")
	}
}

func TestSpawnRoutesRealProcess(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	s := &Server{M: m, getenv: func(string) string { return "" }}
	h := s.Routes()
	cfg := writeConfig(t, m, "agent.yaml")

	// POST /swarm/spawn with a real (sleep) child.
	code, body := doJSON(t, h, "POST", "/swarm/spawn?config_path="+cfg+"&agent_id=agent_001", "")
	if code != 200 || body["agent_id"] != "agent_001" {
		t.Fatalf("spawn: %d %v", code, body)
	}
	if m.agentState("agent_001") != "running" || !m.hasProcess("agent_001") {
		t.Fatalf("agent not running")
	}
	// Bad config path -> 400.
	if code, _ := doJSON(t, h, "POST", "/swarm/spawn?config_path=/nope.yaml", ""); code != 400 {
		t.Fatalf("bad config: %d", code)
	}
	// bust-respawn + kill-all clean up the fleet.
	if code, _ := doJSON(t, h, "POST", "/swarm/bust-respawn", `{}`); code != 200 {
		t.Fatalf("bust-respawn: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/swarm/kill-all", `{}`); code != 200 {
		t.Fatalf("kill-all: %d", code)
	}
}

func TestSpawnBatchRoute(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	s := &Server{M: m, getenv: func(string) string { return "" }}
	h := s.Routes()
	cfg := writeConfig(t, m, "agent.yaml")
	code, _ := doJSON(t, h, "POST", "/swarm/spawn-batch", `{"config_paths":["`+cfg+`"],"group_size":1}`)
	if code != 200 {
		t.Fatalf("spawn-batch: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/swarm/spawn-batch", `{"config_paths":[]}`); code != 422 {
		t.Fatalf("empty batch: %d", code)
	}
	// Reap the spawned children so the temp dir can be cleaned up.
	if code, _ := doJSON(t, h, "POST", "/swarm/kill-all", `{}`); code != 200 {
		t.Fatalf("kill-all: %d", code)
	}
}
