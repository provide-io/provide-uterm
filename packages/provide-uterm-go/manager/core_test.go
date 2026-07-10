//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"encoding/json"
	"os"
	"sync"
	"testing"
	"time"
)

func TestGetSwarmStatusCounts(t *testing.T) {
	m := testManager(t, nil)
	add := func(id, state string) {
		a := newAgentStatus(id)
		a.State = state
		m.Agents[id] = a
	}
	add("agent_001", "running")
	add("agent_002", "completed")
	add("agent_003", "error")
	add("agent_004", "stopped")
	add("agent_005", "blocked")
	st := m.GetSwarmStatus()
	if st.TotalAgents != 5 {
		t.Fatalf("total = %d", st.TotalAgents)
	}
	// running counts running + blocked; errors counts error + blocked.
	if st.Running != 2 || st.Completed != 1 || st.Errors != 2 || st.Stopped != 1 {
		t.Fatalf("counts running=%d completed=%d errors=%d stopped=%d", st.Running, st.Completed, st.Errors, st.Stopped)
	}
}

func TestPauseResumePruneFleet(t *testing.T) {
	m := testManager(t, nil)
	a := newAgentStatus("agent_001")
	a.State = "running"
	m.Agents["agent_001"] = a
	dead := newAgentStatus("agent_002")
	dead.State = "error"
	m.Agents["agent_002"] = dead

	if res := m.PauseSwarm(); res["affected"].(int) != 1 {
		t.Fatalf("pause affected = %v", res["affected"])
	}
	if !m.SwarmPaused || !a.Paused {
		t.Fatal("swarm/agent should be paused")
	}
	if res := m.ResumeSwarm(); res["resumed"].(int) != 1 {
		t.Fatalf("resume = %v", res["resumed"])
	}
	if res := m.PruneDead(); res["pruned"].(int) != 1 {
		t.Fatalf("pruned = %v", res["pruned"])
	}
	if _, ok := m.Agents["agent_002"]; ok {
		t.Fatal("dead agent should be pruned")
	}
}

func TestBroadcastToSink(t *testing.T) {
	m := testManager(t, nil)
	sink := &recordingSink{}
	m.registerWSClient(sink)
	m.Agents["agent_001"] = newAgentStatus("agent_001")
	m.BroadcastStatus()
	if sink.count() == 0 {
		t.Fatal("sink should have received a status")
	}
	// A failing sink is removed.
	failing := &recordingSink{fail: true}
	m.registerWSClient(failing)
	m.BroadcastStatus()
	m.wsMu.Lock()
	_, stillThere := m.wsClients[failing]
	m.wsMu.Unlock()
	if stillThere {
		t.Fatal("failing sink should be removed")
	}
	m.unregisterWSClient(sink)
}

type recordingSink struct {
	mu   sync.Mutex
	msgs []string
	fail bool
}

func (r *recordingSink) sendText(msg string) error {
	if r.fail {
		return errWaitTimeout
	}
	r.mu.Lock()
	r.msgs = append(r.msgs, msg)
	r.mu.Unlock()
	return nil
}
func (r *recordingSink) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.msgs)
}

func TestStatePersistRoundTrip(t *testing.T) {
	m := testManager(t, nil)
	m.DesiredAgents = 3
	m.SwarmPaused = true
	a := newAgentStatus("agent_001")
	a.State = "running" // should be downgraded to stopped on restore
	m.Agents["agent_001"] = a
	m.writeState(m.snapshotState())

	// Fresh manager loads the persisted state.
	m2 := NewAgentManager(m.Config, nil, nil)
	m2.PM = NewAgentProcessManager(m2, nil, m.Config.LogDir)
	m2.LoadState()
	if m2.DesiredAgents != 3 || !m2.SwarmPaused {
		t.Fatalf("restored desired=%d paused=%v", m2.DesiredAgents, m2.SwarmPaused)
	}
	if m2.agentState("agent_001") != "stopped" {
		t.Fatalf("running agent should restore as stopped, got %q", m2.agentState("agent_001"))
	}
}

func TestStartBackgroundStops(t *testing.T) {
	m := testManager(t, nil)
	m.Config.SaveIntervalS = 0.05
	m.HealthCheckInterval = 0
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	m.StartBackground(ctx, &wg)
	time.Sleep(80 * time.Millisecond)
	cancel()
	doneCh := make(chan struct{})
	go func() { wg.Wait(); close(doneCh) }()
	select {
	case <-doneCh:
	case <-time.After(3 * time.Second):
		t.Fatal("background loops did not stop")
	}
	// State file should have been written by the save loop.
	if _, err := os.Stat(m.StateFile); err != nil {
		t.Fatalf("state file not written: %v", err)
	}
}

func TestSnapshotStateSerializable(t *testing.T) {
	m := testManager(t, nil)
	m.Agents["agent_001"] = newAgentStatus("agent_001")
	if _, err := json.Marshal(m.snapshotState()); err != nil {
		t.Fatalf("snapshot not serializable: %v", err)
	}
}
