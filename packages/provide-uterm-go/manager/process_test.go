//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestSpawnAndKillLifecycle(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	cfg := writeConfig(t, m, "agent.yaml")

	id, err := m.SpawnAgent(context.Background(), cfg, "agent_001")
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	if id != "agent_001" {
		t.Fatalf("id = %q", id)
	}
	if m.agentState("agent_001") != "running" {
		t.Fatalf("state = %q, want running", m.agentState("agent_001"))
	}
	if !m.hasProcess("agent_001") {
		t.Fatal("expected a live process")
	}
	m.mu.Lock()
	pid := *m.Agents["agent_001"].PID
	m.mu.Unlock()
	if pid <= 0 {
		t.Fatalf("pid = %d", pid)
	}

	m.KillAgent("agent_001")
	if m.agentState("agent_001") != "stopped" {
		t.Fatalf("post-kill state = %q, want stopped", m.agentState("agent_001"))
	}
	if m.hasProcess("agent_001") {
		t.Fatal("process should be removed after kill")
	}
}

func TestSpawnMaxAgents(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	m.MaxAgents = 0
	cfg := writeConfig(t, m, "agent.yaml")
	_, err := m.SpawnAgent(context.Background(), cfg, "agent_001")
	if err == nil || !strings.Contains(err.Error(), "Max agents (0) reached") {
		t.Fatalf("err = %v", err)
	}
}

func TestSpawnConfigNotFound(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	_, err := m.SpawnAgent(context.Background(), "/does/not/exist.yaml", "agent_001")
	if err == nil || !strings.Contains(err.Error(), "Config not found") {
		t.Fatalf("err = %v", err)
	}
}

func TestSpawnRejectedByPolicy(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	m.PM.SetPolicyGate(denyGate{})
	cfg := writeConfig(t, m, "agent.yaml")
	_, err := m.SpawnAgent(context.Background(), cfg, "agent_001")
	if err == nil || !strings.Contains(err.Error(), "rejected by policy") {
		t.Fatalf("err = %v", err)
	}
}

type denyGate struct{}

func (denyGate) InterceptSpawn(context.Context, string, string, map[string]any) bool { return false }

func TestSpawnUnknownWorkerType(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	// Empty registry → unknown worker type.
	m.PM.workerRegistry = map[string]WorkerRegistryEntry{}
	cfg := writeConfig(t, m, "agent.yaml")
	_, err := m.SpawnAgent(context.Background(), cfg, "agent_001")
	if err == nil || !strings.Contains(err.Error(), "Unknown worker_type") {
		t.Fatalf("err = %v", err)
	}
}

func TestMonitorHandlesCompletedExit(t *testing.T) {
	m := testManager(t, scriptCmd("exit 0"))
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	waitFor(t, "process exit", func() bool {
		m.mu.Lock()
		defer m.mu.Unlock()
		p := m.Processes["agent_001"]
		_, done := p.Poll()
		return done
	})
	m.PM.handleExitedProcesses()
	if m.agentState("agent_001") != "completed" {
		t.Fatalf("state = %q, want completed", m.agentState("agent_001"))
	}
	if m.hasProcess("agent_001") {
		t.Fatal("process should be removed after exit")
	}
}

func TestMonitorHandlesErrorExit(t *testing.T) {
	m := testManager(t, scriptCmd("exit 3"))
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	waitFor(t, "process exit", func() bool {
		m.mu.Lock()
		defer m.mu.Unlock()
		if p := m.Processes["agent_001"]; p != nil {
			_, done := p.Poll()
			return done
		}
		return false
	})
	m.PM.handleExitedProcesses()
	if m.agentState("agent_001") != "error" {
		t.Fatalf("state = %q, want error", m.agentState("agent_001"))
	}
	m.mu.Lock()
	reason := *m.Agents["agent_001"].ExitReason
	m.mu.Unlock()
	if reason != "exit_code_3" {
		t.Fatalf("exit_reason = %q", reason)
	}
}

func TestHeartbeatTimeout(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	m.Config.HeartbeatTimeoutS = 0.01
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	// Backdate the last heartbeat well past the timeout.
	m.mu.Lock()
	m.Agents["agent_001"].LastUpdateTime = nowUnix() - 10
	m.mu.Unlock()
	m.PM.handleHeartbeatTimeouts()
	if m.agentState("agent_001") != "error" {
		t.Fatalf("state = %q, want error", m.agentState("agent_001"))
	}
	m.mu.Lock()
	et := *m.Agents["agent_001"].ErrorType
	m.mu.Unlock()
	if et != "HeartbeatTimeout" {
		t.Fatalf("error_type = %q", et)
	}
}

func TestAllocateAndNoteAgentID(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	id, err := m.PM.AllocateAgentID()
	if err != nil {
		t.Fatalf("allocate: %v", err)
	}
	if id != "agent_000" {
		t.Fatalf("id = %q, want agent_000", id)
	}
	m.PM.NoteAgentID("agent_010")
	id2, _ := m.PM.AllocateAgentID()
	if id2 != "agent_011" {
		t.Fatalf("id2 = %q, want agent_011", id2)
	}
	m.PM.NoteAgentID("not-a-number")
	if _, ok := parseAgentIndex("not-a-number"); ok {
		t.Fatal("parseAgentIndex should reject non-numeric")
	}
}

func TestScopeWorkerTokens(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	m.PM.getenv = func(k string) string {
		if k == "UTERM_MANAGER_WORKER_TOKEN" {
			return "fleet-secret"
		}
		return ""
	}
	env := map[string]string{"UTERM_MANAGER_WORKER_TOKEN": "fleet-secret"}
	m.PM.scopeWorkerTokens(env, "agent_005")
	if _, present := env["UTERM_MANAGER_WORKER_TOKEN"]; present {
		t.Fatal("raw worker token must be stripped from child env")
	}
	got := env["UTERM_MANAGER_API_TOKEN"]
	want := deriveAgentToken("fleet-secret", "agent_005")
	if got != want {
		t.Fatalf("derived token = %q, want %q", got, want)
	}
}

func TestStopProcessTreeFallbackPid(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	// A resolved pid <= 0 is a no-op.
	m.PM.stopProcessTree("agent_x", nil, 0, 10*time.Millisecond)
}

func TestSpawnSwarmPreRegistersAndSpawns(t *testing.T) {
	m := testManager(t, scriptCmd("exit 0"))
	c1 := writeConfig(t, m, "a.yaml")
	c2 := writeConfig(t, m, "b.yaml")
	ids, err := m.PM.SpawnSwarm(context.Background(), []string{c1, c2}, 5, 0, "random", "")
	if err != nil {
		t.Fatalf("spawn swarm: %v", err)
	}
	if len(ids) != 2 {
		t.Fatalf("spawned %d, want 2", len(ids))
	}
}

func TestKillAllAndClear(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	cfg := writeConfig(t, m, "agent.yaml")
	_, _ = m.SpawnAgent(context.Background(), cfg, "agent_001")
	res := m.KillAll()
	if res["count"].(int) != 1 {
		t.Fatalf("killed count = %v", res["count"])
	}
	_, _ = m.SpawnAgent(context.Background(), cfg, "agent_002")
	cleared := m.ClearSwarm()
	if cleared["cleared"].(int) == 0 {
		t.Fatal("expected cleared > 0")
	}
	m.mu.Lock()
	n := len(m.Agents)
	m.mu.Unlock()
	if n != 0 {
		t.Fatalf("agents remaining = %d", n)
	}
}
