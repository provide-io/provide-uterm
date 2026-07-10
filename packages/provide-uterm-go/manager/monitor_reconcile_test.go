//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// seedQueued adds a queued (PID 0, not-yet-started) agent to m.
func seedQueued(m *AgentManager, id, config string) *AgentStatus {
	a := newAgentStatus(id)
	a.State = "queued"
	a.PID = intPtr(0)
	if config != "" {
		a.Config = strPtr(config)
	}
	m.Agents[id] = a
	return a
}

func TestLaunchQueuedAgentFailure(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	seedQueued(m, "agent_001", "/does/not/exist.yaml")
	// A launch whose config is missing must flip the agent to error.
	m.PM.launchQueuedAgent("agent_001", "/does/not/exist.yaml")
	if m.agentState("agent_001") != "error" {
		t.Fatalf("state = %q, want error", m.agentState("agent_001"))
	}
	m.mu.Lock()
	reason := *m.Agents["agent_001"].ExitReason
	m.mu.Unlock()
	if reason != "launch_failed" {
		t.Fatalf("exit_reason = %q, want launch_failed", reason)
	}
}

func TestHandleStaleQueuedLaunchesWithConfig(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	cfg := writeConfig(t, m, "agent.yaml")
	m.PM.queuedLaunchDelay = 0
	seedQueued(m, "agent_001", cfg)

	// First pass just records queuedSince; the agent stays queued.
	m.PM.handleStaleQueued()
	if m.agentState("agent_001") != "queued" {
		t.Fatalf("after first pass state = %q, want queued", m.agentState("agent_001"))
	}
	// Second pass launches the stale queued agent (real child).
	m.PM.handleStaleQueued()
	m.PM.spawnWG.Wait()
	waitFor(t, "queued agent to launch", func() bool {
		return m.agentState("agent_001") == "running" && m.hasProcess("agent_001")
	})
	m.KillAll()
}

func TestHandleStaleQueuedNoConfigStops(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	m.PM.queuedLaunchDelay = 0
	seedQueued(m, "agent_001", "") // no config

	m.PM.handleStaleQueued()
	m.PM.handleStaleQueued()
	if m.agentState("agent_001") != "stopped" {
		t.Fatalf("state = %q, want stopped", m.agentState("agent_001"))
	}
	m.mu.Lock()
	reason := *m.Agents["agent_001"].ExitReason
	m.mu.Unlock()
	if reason != "no_config" {
		t.Fatalf("exit_reason = %q, want no_config", reason)
	}
}

func TestHandleStaleQueuedSkippedWhenDesired(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	m.PM.queuedLaunchDelay = 0
	cfg := writeConfig(t, m, "agent.yaml")
	seedQueued(m, "agent_001", cfg)
	m.mu.Lock()
	m.DesiredAgents = 2 // desired-state owns scaling; stale-launch backs off
	m.mu.Unlock()

	m.PM.handleStaleQueued()
	m.PM.handleStaleQueued()
	if m.agentState("agent_001") != "queued" {
		t.Fatalf("state = %q, want queued (desired-state should own scaling)", m.agentState("agent_001"))
	}
}

func TestHandleStaleQueuedIgnoresNonQueued(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	m.PM.queuedLaunchDelay = 0
	a := newAgentStatus("agent_001")
	a.State = "running"
	a.PID = intPtr(123)
	m.Agents["agent_001"] = a
	// Prime queuedSince, then a running agent must clear it.
	m.PM.mu.Lock()
	m.PM.queuedSince["agent_001"] = nowUnix() - 100
	m.PM.mu.Unlock()
	m.PM.handleStaleQueued()
	m.PM.mu.Lock()
	_, present := m.PM.queuedSince["agent_001"]
	m.PM.mu.Unlock()
	if present {
		t.Fatal("queuedSince should be cleared for a non-queued agent")
	}
	if m.agentState("agent_001") != "running" {
		t.Fatalf("state = %q, want running", m.agentState("agent_001"))
	}
}

func TestHandleBustRespawnKillsBustAgent(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	m.mu.Lock()
	m.BustRespawn = true
	m.Agents["agent_001"].Extra["activity_context"] = "bust" // case-insensitive
	m.mu.Unlock()

	m.PM.handleBustRespawn()
	if m.agentState("agent_001") != "stopped" {
		t.Fatalf("state = %q, want stopped", m.agentState("agent_001"))
	}
	if m.hasProcess("agent_001") {
		t.Fatal("process should be removed after bust respawn")
	}
	m.mu.Lock()
	reason := *m.Agents["agent_001"].ExitReason
	m.mu.Unlock()
	if reason != "bust_respawn" {
		t.Fatalf("exit_reason = %q, want bust_respawn", reason)
	}
}

func TestHandleBustRespawnEarlyReturns(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	a := newAgentStatus("agent_001")
	a.State = "running"
	a.Extra["activity_context"] = "BUST"
	m.Agents["agent_001"] = a

	// Bust disabled -> no-op.
	m.PM.handleBustRespawn()
	if m.agentState("agent_001") != "running" {
		t.Fatal("bust disabled should be a no-op")
	}
	// Bust enabled but swarm paused -> no-op.
	m.mu.Lock()
	m.BustRespawn = true
	m.SwarmPaused = true
	m.mu.Unlock()
	m.PM.handleBustRespawn()
	if m.agentState("agent_001") != "running" {
		t.Fatal("paused swarm should skip bust respawn")
	}
	// Enabled + unpaused but agent not flagged BUST / not running -> skipped.
	m.mu.Lock()
	m.SwarmPaused = false
	m.Agents["agent_001"].Extra["activity_context"] = "IDLE"
	m.mu.Unlock()
	m.PM.handleBustRespawn()
	if m.agentState("agent_001") != "running" {
		t.Fatal("non-BUST agent should be skipped")
	}
}

func TestCleanupOldWorkerLogs(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	logDir := m.PM.logDir
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	// An active agent whose .log must be preserved.
	m.Agents["agent_active"] = newAgentStatus("agent_active")

	old := time.Now().Add(-10 * 24 * time.Hour)
	recent := time.Now()
	write := func(name string, mtime time.Time) string {
		p := filepath.Join(logDir, name)
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
		if err := os.Chtimes(p, mtime, mtime); err != nil {
			t.Fatalf("chtimes %s: %v", name, err)
		}
		return p
	}
	stalePrev := write("agent_009.log.prev", old)     // stale .prev -> deleted
	orphanLog := write("agent_050.log", old)          // stale orphan .log -> deleted
	activeLog := write("agent_active.log", old)       // stale but active -> kept
	recentPrev := write("agent_010.log.prev", recent) // fresh .prev -> kept
	// A subdirectory is skipped.
	if err := os.MkdirAll(filepath.Join(logDir, "sub"), 0o755); err != nil {
		t.Fatalf("mkdir sub: %v", err)
	}

	deleted := m.PM.cleanupOldWorkerLogs()
	if deleted != 2 {
		t.Fatalf("deleted = %d, want 2", deleted)
	}
	if _, err := os.Stat(stalePrev); !os.IsNotExist(err) {
		t.Fatal("stale .prev should be deleted")
	}
	if _, err := os.Stat(orphanLog); !os.IsNotExist(err) {
		t.Fatal("stale orphan .log should be deleted")
	}
	if _, err := os.Stat(activeLog); err != nil {
		t.Fatal("active agent .log should be kept")
	}
	if _, err := os.Stat(recentPrev); err != nil {
		t.Fatal("fresh .prev should be kept")
	}
}

func TestCleanupOldWorkerLogsMissingDir(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	// logDir does not exist yet -> a clean no-op returning 0.
	if n := m.PM.cleanupOldWorkerLogs(); n != 0 {
		t.Fatalf("deleted = %d, want 0 for missing dir", n)
	}
}

func TestHandleDesiredStateDisabledAndPaused(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	// Paused (with a positive desired) is a no-op.
	m.mu.Lock()
	m.DesiredAgents = 2
	m.SwarmPaused = true
	m.mu.Unlock()
	m.PM.handleDesiredState() // must not panic / spawn
}

func TestHandleDesiredStateScalesUp(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	m.mu.Lock()
	m.DesiredAgents = 2 // deficit of 1 -> one more agent spawned from the active config
	m.mu.Unlock()

	m.PM.handleDesiredState()
	m.PM.spawnWG.Wait()
	waitFor(t, "scale-up spawn", func() bool {
		m.mu.Lock()
		defer m.mu.Unlock()
		running := 0
		for _, a := range m.Agents {
			if a.State == "running" {
				running++
			}
		}
		return running >= 2
	})
	m.KillAll()
}

func TestHandleDesiredStateScalesDown(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	cfg := writeConfig(t, m, "agent.yaml")
	for _, id := range []string{"agent_001", "agent_002"} {
		if _, err := m.SpawnAgent(context.Background(), cfg, id); err != nil {
			t.Fatalf("spawn %s: %v", id, err)
		}
	}
	m.mu.Lock()
	m.DesiredAgents = 1 // excess of 1 -> highest id killed
	m.mu.Unlock()

	m.PM.handleDesiredState()
	m.mu.Lock()
	_, stillThere := m.Agents["agent_002"]
	m.mu.Unlock()
	if stillThere {
		t.Fatal("agent_002 (highest id) should have been killed as excess")
	}
	m.KillAll()
}

func TestHandleDesiredStatePrunesTerminal(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	// A terminal (error) agent with no live process must be pruned.
	dead := newAgentStatus("agent_dead")
	dead.State = "error"
	m.mu.Lock()
	m.Agents["agent_dead"] = dead
	m.DesiredAgents = 1
	m.mu.Unlock()

	m.PM.handleDesiredState()
	m.PM.spawnWG.Wait()
	m.mu.Lock()
	_, deadPresent := m.Agents["agent_dead"]
	m.mu.Unlock()
	if deadPresent {
		t.Fatal("terminal agent should be pruned by desired-state")
	}
	m.KillAll()
}

func TestStopProcessTreeForcesKill(t *testing.T) {
	m := testManager(t, scriptCmd(`trap "" TERM; sleep 30`))
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	m.mu.Lock()
	proc := m.Processes["agent_001"]
	delete(m.Processes, "agent_001")
	m.mu.Unlock()
	// The child ignores SIGTERM, so the graceful wait times out and the tree
	// is force-killed with SIGKILL.
	m.PM.stopProcessTree("agent_001", proc, 0, 100*time.Millisecond)
	if err := proc.WaitExit(2 * time.Second); err != nil {
		t.Fatalf("process still alive after force kill: %v", err)
	}
}

func TestWaitExitTimeout(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	m.mu.Lock()
	proc := m.Processes["agent_001"]
	m.mu.Unlock()
	if err := proc.WaitExit(1 * time.Millisecond); err != errWaitTimeout {
		t.Fatalf("WaitExit err = %v, want timeout", err)
	}
	m.KillAll()
}
