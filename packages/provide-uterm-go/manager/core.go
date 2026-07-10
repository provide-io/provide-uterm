//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"sync"
	"time"
)

// statusSink receives broadcast status text. A dashboard WebSocket client
// implements it. Mirrors the send_text side of broadcast_status.
type statusSink interface {
	sendText(msg string) error
}

// AgentManager is the generic coordinator for an agent swarm, mirroring
// AgentManager in core.py.
type AgentManager struct {
	Config              ManagerConfig
	MaxAgents           int
	StateFile           string
	HealthCheckInterval int
	startTime           float64

	mu            sync.Mutex
	Agents        map[string]*AgentStatus
	Processes     map[string]processHandle
	DesiredAgents int
	SwarmPaused   bool
	BustRespawn   bool

	Timeseries *TimeseriesManager
	PM         *AgentProcessManager

	wsMu      sync.Mutex
	wsClients map[statusSink]struct{}

	// mcpShutdownCancel cancels a pending auto-shutdown timer.
	// Shutdown is invoked when auto-shutdown fires (e.g. to stop the server).
	Shutdown func()

	now    func() float64
	logger *slog.Logger
}

// NewAgentManager constructs a manager and its timeseries manager. now is
// injectable (nil = wall-clock).
func NewAgentManager(cfg ManagerConfig, tsPlugin TimeseriesPlugin, now func() float64) *AgentManager {
	if now == nil {
		now = nowUnix
	}
	m := &AgentManager{
		Config:              cfg,
		MaxAgents:           cfg.MaxAgents,
		StateFile:           cfg.StateFile,
		HealthCheckInterval: cfg.HealthCheckIntervalS,
		startTime:           now(),
		Agents:              map[string]*AgentStatus{},
		Processes:           map[string]processHandle{},
		wsClients:           map[statusSink]struct{}{},
		now:                 now,
		logger:              getLogger("provide.uterm.manager.core"),
	}
	tsDir := cfg.TimeseriesDir
	if tsDir == "" {
		tsDir = "logs/metrics"
	}
	interval := cfg.TimeseriesIntervalS
	if interval == 0 {
		interval = TimeseriesIntervalS
	}
	m.Timeseries = NewTimeseriesManager(m.GetSwarmStatus, tsDir, interval, tsPlugin, now)
	return m
}

// --- Delegated process management ---

// SpawnAgent forwards to the process manager.
func (m *AgentManager) SpawnAgent(ctx context.Context, configPath, agentID string) (string, error) {
	return m.PM.SpawnAgent(ctx, configPath, agentID)
}

// KillAgent forwards to the process manager.
func (m *AgentManager) KillAgent(agentID string) { m.PM.KillAgent(agentID) }

// --- Fleet operations ---

// KillAll cancels pending spawns and kills all running processes, mirroring
// kill_all.
func (m *AgentManager) KillAll() map[string]any {
	m.PM.CancelSpawn()
	killed := []string{}
	m.mu.Lock()
	ids := make([]string, 0, len(m.Processes))
	for id := range m.Processes {
		ids = append(ids, id)
	}
	m.mu.Unlock()
	for _, id := range ids {
		m.KillAgent(id)
		killed = append(killed, id)
	}
	return map[string]any{"killed": killed, "count": len(killed)}
}

// ClearSwarm kills all processes and removes all registrations, mirroring
// clear_swarm.
func (m *AgentManager) ClearSwarm() map[string]any {
	m.PM.CancelSpawn()
	m.mu.Lock()
	ids := make([]string, 0, len(m.Processes))
	for id := range m.Processes {
		ids = append(ids, id)
	}
	m.mu.Unlock()
	for _, id := range ids {
		m.KillAgent(id)
	}
	m.mu.Lock()
	count := len(m.Agents)
	m.Agents = map[string]*AgentStatus{}
	m.Processes = map[string]processHandle{}
	m.mu.Unlock()
	m.BroadcastStatus()
	return map[string]any{"cleared": count}
}

// PruneDead removes agents in terminal states, mirroring prune_dead.
func (m *AgentManager) PruneDead() map[string]any {
	terminal := map[string]struct{}{"stopped": {}, "error": {}, "completed": {}}
	m.mu.Lock()
	var deadIDs []string
	for id, a := range m.Agents {
		if _, ok := terminal[a.State]; ok {
			deadIDs = append(deadIDs, id)
		}
	}
	m.mu.Unlock()
	for _, id := range deadIDs {
		m.mu.Lock()
		_, hasProc := m.Processes[id]
		m.mu.Unlock()
		if hasProc {
			m.KillAgent(id)
			m.mu.Lock()
			delete(m.Processes, id)
			m.mu.Unlock()
		}
		m.mu.Lock()
		delete(m.Agents, id)
		m.mu.Unlock()
	}
	m.BroadcastStatus()
	m.mu.Lock()
	remaining := len(m.Agents)
	m.mu.Unlock()
	return map[string]any{"pruned": len(deadIDs), "remaining": remaining}
}

// PauseSwarm pauses the swarm and marks active agents paused, mirroring
// pause_swarm.
func (m *AgentManager) PauseSwarm() map[string]any {
	m.mu.Lock()
	m.SwarmPaused = true
	affected := 0
	for _, a := range m.Agents {
		if a.State == "running" || a.State == "recovering" || a.State == "blocked" {
			a.Paused = true
		}
		if a.Paused {
			affected++
		}
	}
	m.mu.Unlock()
	m.BroadcastStatus()
	return map[string]any{"paused": true, "affected": affected}
}

// ResumeSwarm resumes the swarm, mirroring resume_swarm.
func (m *AgentManager) ResumeSwarm() map[string]any {
	m.mu.Lock()
	m.SwarmPaused = false
	resumed := 0
	for _, a := range m.Agents {
		if a.Paused {
			a.Paused = false
			resumed++
		}
	}
	m.mu.Unlock()
	m.BroadcastStatus()
	return map[string]any{"paused": false, "resumed": resumed}
}

// --- Timeseries delegation ---

// GetTimeseriesInfo forwards to the timeseries manager.
func (m *AgentManager) GetTimeseriesInfo() map[string]any { return m.Timeseries.GetInfo() }

// GetTimeseriesRecent forwards to the timeseries manager.
func (m *AgentManager) GetTimeseriesRecent(limit int) []map[string]any {
	return m.Timeseries.GetRecent(limit)
}

// GetTimeseriesSummary forwards to the timeseries manager.
func (m *AgentManager) GetTimeseriesSummary(windowMinutes int) map[string]any {
	return m.Timeseries.GetSummary(windowMinutes)
}

// --- Swarm status ---

// GetSwarmStatus builds the current swarm status snapshot, mirroring
// get_swarm_status (base-only builder).
func (m *AgentManager) GetSwarmStatus() *SwarmStatus {
	m.mu.Lock()
	agents := make([]*AgentStatus, 0, len(m.Agents))
	for _, a := range m.Agents {
		agents = append(agents, a)
	}
	running, completed, errs, stopped := 0, 0, 0, 0
	for _, a := range agents {
		switch a.State {
		case "running", "recovering":
			running++
		case "blocked":
			running++
			errs++
		case "completed":
			completed++
		case "error", "disconnected":
			errs++
		case "stopped":
			stopped++
		}
	}
	desired := m.DesiredAgents
	paused := m.SwarmPaused
	bust := m.BustRespawn
	m.mu.Unlock()

	tsFile := m.Timeseries.Path
	return &SwarmStatus{
		TotalAgents:               len(agents),
		Running:                   running,
		Completed:                 completed,
		Errors:                    errs,
		Stopped:                   stopped,
		UptimeSeconds:             m.now() - m.startTime,
		TimeseriesFile:            &tsFile,
		TimeseriesIntervalSeconds: m.Timeseries.IntervalS,
		TimeseriesSamples:         m.Timeseries.SamplesCount,
		SwarmPaused:               paused,
		BustRespawn:               bust,
		DesiredAgents:             desired,
		Agents:                    agents,
	}
}

// --- WebSocket broadcasting ---

// registerWSClient adds a dashboard client.
func (m *AgentManager) registerWSClient(c statusSink) {
	m.wsMu.Lock()
	m.wsClients[c] = struct{}{}
	m.wsMu.Unlock()
}

// unregisterWSClient removes a dashboard client.
func (m *AgentManager) unregisterWSClient(c statusSink) {
	m.wsMu.Lock()
	delete(m.wsClients, c)
	m.wsMu.Unlock()
}

// BroadcastStatus pushes the current status to all dashboard clients, mirroring
// broadcast_status.
func (m *AgentManager) BroadcastStatus() {
	status := m.GetSwarmStatus()
	msg, err := json.Marshal(status)
	if err != nil {
		return
	}
	m.wsMu.Lock()
	clients := make([]statusSink, 0, len(m.wsClients))
	for c := range m.wsClients {
		clients = append(clients, c)
	}
	m.wsMu.Unlock()
	var disconnected []statusSink
	for _, c := range clients {
		if err := c.sendText(string(msg)); err != nil {
			disconnected = append(disconnected, c)
		}
	}
	if len(disconnected) > 0 {
		m.wsMu.Lock()
		for _, c := range disconnected {
			delete(m.wsClients, c)
		}
		m.wsMu.Unlock()
	}
}

// --- State persistence ---

// writeState writes the state dict to disk atomically, mirroring _write_state.
func (m *AgentManager) writeState(state map[string]any) {
	if m.StateFile == "" {
		return
	}
	tmp := m.StateFile + ".tmp"
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		m.logger.Error("state_save_failed", "error", err.Error(), "state_file", m.StateFile)
		return
	}
	if err := os.WriteFile(tmp, data, 0o644); err != nil { //nolint:gosec // operator-owned state file
		m.logger.Error("state_save_failed", "error", err.Error(), "state_file", m.StateFile)
		_ = os.Remove(tmp)
		return
	}
	if err := os.Rename(tmp, m.StateFile); err != nil {
		m.logger.Error("state_save_failed", "error", err.Error(), "state_file", m.StateFile)
		_ = os.Remove(tmp)
	}
}

// snapshotState builds the persisted state dict.
func (m *AgentManager) snapshotState() map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	agents := map[string]any{}
	for id, a := range m.Agents {
		agents[id] = a.toMap()
	}
	return map[string]any{
		"timestamp":      m.now(),
		"desired_agents": m.DesiredAgents,
		"swarm_paused":   m.SwarmPaused,
		"bust_respawn":   m.BustRespawn,
		"agents":         agents,
	}
}

// restoreAgent restores one agent from saved state, mirroring _restore_agent.
func (m *AgentManager) restoreAgent(agentID string, data map[string]any) {
	if _, ok := m.Agents[agentID]; ok {
		return
	}
	savedState, _ := data["state"].(string)
	if savedState == "" {
		savedState = "stopped"
	}
	switch savedState {
	case "running", "recovering", "disconnected", "queued":
		data["state"] = "stopped"
	}
	if _, ok := data["agent_id"]; !ok {
		data["agent_id"] = agentID
	}
	m.Agents[agentID] = agentStatusFromMap(data)
}

// LoadState loads swarm state from the state file, mirroring _load_state.
func (m *AgentManager) LoadState() {
	if m.StateFile == "" {
		return
	}
	if _, err := os.Stat(m.StateFile); err != nil {
		return
	}
	data, err := os.ReadFile(m.StateFile) //nolint:gosec // operator-owned state file
	if err != nil {
		m.logger.Error("state_load_failed", "error", err.Error())
		return
	}
	var state map[string]any
	if err := json.Unmarshal(data, &state); err != nil {
		m.logger.Error("state_load_failed", "error", err.Error())
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if v, ok := state["desired_agents"]; ok {
		m.DesiredAgents = asInt(v)
	}
	if v, ok := state["swarm_paused"].(bool); ok {
		m.SwarmPaused = v
	}
	if v, ok := state["bust_respawn"].(bool); ok {
		m.BustRespawn = v
	}
	if agents, ok := state["agents"].(map[string]any); ok {
		for id, raw := range agents {
			if ad, ok := raw.(map[string]any); ok {
				m.restoreAgent(id, ad)
			}
		}
	}
	m.logger.Info("agents_loaded_from_state", "count", len(m.Agents), "desired_agents", m.DesiredAgents, "swarm_paused", m.SwarmPaused, "state_file", m.StateFile)
}

// StartBackground launches the monitor, timeseries, and save loops, mirroring
// the background tasks in run(). It returns when ctx is cancelled (after the
// loops observe cancellation) if wait is called via the returned WaitGroup.
func (m *AgentManager) StartBackground(ctx context.Context, wg *sync.WaitGroup) {
	wg.Add(3)
	go func() { defer wg.Done(); m.PM.MonitorProcesses(ctx) }()
	go func() { defer wg.Done(); m.Timeseries.Loop(ctx) }()
	go func() {
		defer wg.Done()
		interval := m.Config.SaveIntervalS
		if interval <= 0 {
			interval = SaveIntervalS
		}
		ticker := time.NewTicker(time.Duration(interval * float64(time.Second)))
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				m.writeState(m.snapshotState())
			}
		}
	}()
}
