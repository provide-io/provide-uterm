//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import "testing"

func TestDefaultSpawnCommand(t *testing.T) {
	got := defaultSpawnCommand("mymod", "/cfg.toml", "agent_007")
	want := []string{"python3", "-m", "mymod", "--config", "/cfg.toml", "--agent-id", "agent_007"}
	if len(got) != len(want) {
		t.Fatalf("len = %d", len(got))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("arg %d = %q want %q", i, got[i], want[i])
		}
	}
}

func TestCollectSpawnConfigs(t *testing.T) {
	mk := func(cfg string) *AgentStatus {
		a := newAgentStatus("x")
		if cfg != "" {
			a.Config = strPtr(cfg)
		}
		return a
	}
	// Active configs win.
	got := collectSpawnConfigs([]*AgentStatus{mk("a"), mk("")}, []*AgentStatus{mk("d")}, "last")
	if len(got) != 1 || got[0] != "a" {
		t.Fatalf("active = %v", got)
	}
	// No active -> dead.
	got = collectSpawnConfigs([]*AgentStatus{mk("")}, []*AgentStatus{mk("d")}, "last")
	if len(got) != 1 || got[0] != "d" {
		t.Fatalf("dead = %v", got)
	}
	// Neither -> last.
	got = collectSpawnConfigs(nil, nil, "last")
	if len(got) != 1 || got[0] != "last" {
		t.Fatalf("last = %v", got)
	}
	// Nothing at all.
	if got := collectSpawnConfigs(nil, nil, ""); got != nil {
		t.Fatalf("empty = %v", got)
	}
}

func TestReleaseAgentAccountNoop(t *testing.T) {
	m := testManager(t, nil)
	m.PM.releaseAgentAccount("agent_001") // no panic, no-op on the bare manager
}

func TestKillExcess(t *testing.T) {
	m := testManager(t, nil)
	// Two running agents; kill 1 excess (highest agent_id first).
	for _, id := range []string{"agent_001", "agent_002"} {
		a := newAgentStatus(id)
		a.State = "running"
		m.Agents[id] = a
	}
	active := []*AgentStatus{m.Agents["agent_001"], m.Agents["agent_002"]}
	m.PM.killExcess(1, active)
	if len(m.Agents) != 1 {
		t.Fatalf("agents after kill = %d", len(m.Agents))
	}
	if _, ok := m.Agents["agent_002"]; ok {
		t.Fatal("agent_002 (highest id) should have been killed")
	}
	// excess larger than active is clamped.
	m.PM.killExcess(5, []*AgentStatus{m.Agents["agent_001"]})
	if len(m.Agents) != 0 {
		t.Fatalf("agents after clamp = %d", len(m.Agents))
	}
}

func TestSpawnToDesiredNoConfigsNoop(t *testing.T) {
	m := testManager(t, nil)
	// No configs anywhere -> spawnToDesired can't spawn, must not panic.
	m.PM.spawnToDesired(2, nil, nil)
}

func TestHandleDesiredStateDisabled(t *testing.T) {
	m := testManager(t, nil)
	// DesiredAgents unset (0 / disabled) -> handleDesiredState is a no-op.
	m.PM.handleDesiredState()
}
