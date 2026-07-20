//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

// TOOLCount is the number of manager MCP tools, mirroring TOOL_COUNT in
// mcp_tools.py.
const TOOLCount = 15

// ManagerTools exposes the manager-specific MCP tool DEFINITIONS as plain Go
// funcs operating on an in-process AgentManager. Wiring these into an MCP
// server (tool registration/schema) is intentionally left to the MCP layer —
// this port provides only the behavior bodies, matching the in-process branch
// of create_manager_mcp_tools in mcp_tools.py. NOTE: the HTTP ("out-of-process")
// mode of the Python tools is not reproduced here; call the REST routes for
// that.
type ManagerTools struct {
	M *AgentManager
	// AgentTelemetryFields are per-agent field names stripped from swarm_status
	// when includeTelemetry is false (nil = strip nothing).
	AgentTelemetryFields map[string]struct{}
}

// NewManagerTools constructs the tool set bound to m.
func NewManagerTools(m *AgentManager) *ManagerTools { return &ManagerTools{M: m} }

// SwarmStatus returns the current swarm status, optionally stripping per-agent
// telemetry fields.
//
// GetSwarmStatus().toMap() JSON-round-trips agents into []any of map[string]any
// (never []*AgentStatus), so the strip path walks that shape. A typed
// []*AgentStatus branch is kept for callers that inject a raw status map.
func (t *ManagerTools) SwarmStatus(includeTelemetry bool) map[string]any {
	data := t.M.GetSwarmStatus().toMap()
	if !includeTelemetry && len(t.AgentTelemetryFields) > 0 {
		stripAgentsTelemetry(data, t.AgentTelemetryFields)
	}
	return data
}

// stripAgentsTelemetry removes the named fields from each agent row in data.
// Accepts both the toMap() []any shape and a typed []*AgentStatus slice.
func stripAgentsTelemetry(data map[string]any, fields map[string]struct{}) {
	switch agents := data["agents"].(type) {
	case []*AgentStatus:
		rows := make([]map[string]any, 0, len(agents))
		for _, a := range agents {
			row := a.toMap()
			for f := range fields {
				delete(row, f)
			}
			rows = append(rows, row)
		}
		data["agents"] = rows
	case []any:
		rows := make([]any, 0, len(agents))
		for _, item := range agents {
			row, ok := item.(map[string]any)
			if !ok {
				rows = append(rows, item)
				continue
			}
			cp := make(map[string]any, len(row))
			for k, v := range row {
				cp[k] = v
			}
			for f := range fields {
				delete(cp, f)
			}
			rows = append(rows, cp)
		}
		data["agents"] = rows
	}
}

// SwarmSpawnBatch starts a staggered batch spawn, mirroring swarm_spawn_batch
// (in-process branch), including spawn-sandbox validation.
func (t *ManagerTools) SwarmSpawnBatch(configPaths []string, groupSize int, groupDelay float64, nameStyle, nameBase string) map[string]any {
	for _, p := range configPaths {
		if _, err := validateConfigPath(p, t.M.Config.SpawnConfigDir, t.M.PM.getenv); err != nil {
			return map[string]any{"error": err.Error()}
		}
	}
	if groupSize < 1 {
		groupSize = 1
	}
	total := len(configPaths)
	t.M.PM.StartSpawnSwarm(configPaths, groupSize, groupDelay, true, nameStyle, nameBase)
	t.M.mu.Lock()
	t.M.DesiredAgents = total
	t.M.mu.Unlock()
	t.M.PM.SyncNextAgentIndex()
	groups := (total + groupSize - 1) / groupSize
	est := 0.0
	if groups > 1 {
		est = float64(groups-1) * groupDelay
	}
	return map[string]any{
		"status":                 "spawning",
		"total_agents":           total,
		"group_size":             groupSize,
		"group_delay":            groupDelay,
		"total_groups":           groups,
		"estimated_time_seconds": est,
		"desired_agents":         total,
	}
}

// SwarmPause pauses the swarm.
func (t *ManagerTools) SwarmPause() map[string]any { return t.M.PauseSwarm() }

// SwarmResume resumes the swarm.
func (t *ManagerTools) SwarmResume() map[string]any { return t.M.ResumeSwarm() }

// SwarmKillAll kills all agents.
func (t *ManagerTools) SwarmKillAll() map[string]any { return t.M.KillAll() }

// SwarmClear clears the swarm.
func (t *ManagerTools) SwarmClear() map[string]any { return t.M.ClearSwarm() }

// SwarmPrune prunes terminal agents.
func (t *ManagerTools) SwarmPrune() map[string]any { return t.M.PruneDead() }

// SwarmSetDesired sets the desired agent count.
func (t *ManagerTools) SwarmSetDesired(count int) map[string]any {
	t.M.mu.Lock()
	t.M.DesiredAgents = count
	t.M.mu.Unlock()
	t.M.BroadcastStatus()
	return map[string]any{"desired_agents": count}
}

// AgentList lists agents, optionally filtered by state.
func (t *ManagerTools) AgentList(state string) map[string]any {
	t.M.mu.Lock()
	rows := []map[string]any{}
	for _, a := range t.M.Agents {
		if state != "" && a.State != state {
			continue
		}
		rows = append(rows, a.toMap())
	}
	t.M.mu.Unlock()
	return map[string]any{"total": len(rows), "agents": rows}
}

// AgentStatus returns one agent's status.
func (t *ManagerTools) AgentStatus(agentID string) map[string]any {
	t.M.mu.Lock()
	a, ok := t.M.Agents[agentID]
	var row map[string]any
	if ok {
		row = a.toMap()
	}
	t.M.mu.Unlock()
	if !ok {
		return map[string]any{"error": "Agent " + agentID + " not found"}
	}
	return row
}

// AgentKill terminates an agent process and removes/stops it.
func (t *ManagerTools) AgentKill(agentID string) map[string]any {
	t.M.mu.Lock()
	a, ok := t.M.Agents[agentID]
	if !ok {
		t.M.mu.Unlock()
		return map[string]any{"error": "Agent " + agentID + " not found"}
	}
	_, hasProc := t.M.Processes[agentID]
	t.M.mu.Unlock()
	if hasProc {
		t.M.KillAgent(agentID)
	} else {
		t.M.mu.Lock()
		a.State = "stopped"
		t.M.mu.Unlock()
	}
	t.M.mu.Lock()
	if t.M.DesiredAgents > 0 {
		t.M.DesiredAgents--
	}
	state := a.State
	t.M.mu.Unlock()
	t.M.BroadcastStatus()
	return map[string]any{"agent_id": agentID, "action": "kill", "state": state}
}

// AgentPause pauses a single agent.
func (t *ManagerTools) AgentPause(agentID string) map[string]any {
	return t.agentSetPaused(agentID, "pause", true)
}

// AgentResume resumes a single agent.
func (t *ManagerTools) AgentResume(agentID string) map[string]any {
	return t.agentSetPaused(agentID, "resume", false)
}

func (t *ManagerTools) agentSetPaused(agentID, action string, paused bool) map[string]any {
	t.M.mu.Lock()
	a, ok := t.M.Agents[agentID]
	if !ok {
		t.M.mu.Unlock()
		return map[string]any{"error": "Agent " + agentID + " not found"}
	}
	a.Paused = paused
	t.M.mu.Unlock()
	t.M.BroadcastStatus()
	return map[string]any{"agent_id": agentID, "action": action, "paused": paused}
}

// AgentRestart queues a restart command for an agent.
func (t *ManagerTools) AgentRestart(agentID string) map[string]any {
	t.M.mu.Lock()
	a, ok := t.M.Agents[agentID]
	if !ok {
		t.M.mu.Unlock()
		return map[string]any{"error": "Agent " + agentID + " not found"}
	}
	queued := queueManagerCommand(a, "restart", map[string]any{})
	t.M.mu.Unlock()
	t.M.BroadcastStatus()
	return map[string]any{"agent_id": agentID, "action": "restart", "queued": true, "command": queued}
}

// AgentEvents returns recent events for an agent.
func (t *ManagerTools) AgentEvents(agentID string) map[string]any {
	t.M.mu.Lock()
	a, ok := t.M.Agents[agentID]
	if !ok {
		t.M.mu.Unlock()
		return map[string]any{"error": "Agent " + agentID + " not found"}
	}
	events := []map[string]any{}
	for _, action := range a.RecentActions {
		ev := map[string]any{"type": "action"}
		for k, v := range action {
			ev[k] = v
		}
		events = append(events, ev)
	}
	if a.ErrorMessage != nil && *a.ErrorMessage != "" {
		events = append(events, map[string]any{
			"type":       "error",
			"message":    *a.ErrorMessage,
			"error_type": ptrOrNil(a.ErrorType),
			"timestamp":  floatPtrAny(a.ErrorTimestamp),
		})
	}
	state := a.State
	t.M.mu.Unlock()
	return map[string]any{"agent_id": agentID, "state": state, "events": events}
}

// floatPtrAny returns *p as any, or nil.
func floatPtrAny(p *float64) any {
	if p == nil {
		return nil
	}
	return *p
}
