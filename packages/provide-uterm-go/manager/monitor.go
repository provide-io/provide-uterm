//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// stopTimeout is the graceful-kill duration used by the monitor helpers.
var stopTimeout = time.Duration(stopTimeoutS * float64(time.Second))

// runningStates are the states counted as "alive" for heartbeat/exit handling.
func isRunningState(s string) bool {
	return s == "running" || s == "recovering" || s == "blocked"
}

// CancelSpawn cancels any in-flight spawn goroutine, mirroring cancel_spawn.
func (pm *AgentProcessManager) CancelSpawn() bool {
	pm.mu.Lock()
	cancel := pm.spawnCancel
	pm.spawnCancel = nil
	pm.mu.Unlock()
	if cancel == nil {
		return false
	}
	cancel()
	pm.spawnWG.Wait()
	return true
}

// StartSpawnSwarm launches spawn_swarm in the background, mirroring
// start_spawn_swarm.
func (pm *AgentProcessManager) StartSpawnSwarm(configPaths []string, groupSize int, groupDelay float64, cancelExisting bool, nameStyle, nameBase string) {
	if cancelExisting {
		pm.CancelSpawn()
	}
	ctx, cancel := context.WithCancel(context.Background())
	pm.mu.Lock()
	pm.spawnCancel = cancel
	pm.mu.Unlock()
	pm.spawnWG.Add(1)
	go func() {
		defer pm.spawnWG.Done()
		_, _ = pm.SpawnSwarm(ctx, configPaths, groupSize, groupDelay, nameStyle, nameBase)
	}()
}

// SpawnSwarm pre-registers agents as queued then spawns them in staggered
// groups, mirroring spawn_swarm.
func (pm *AgentProcessManager) SpawnSwarm(ctx context.Context, configPaths []string, groupSize int, groupDelay float64, nameStyle, nameBase string) ([]string, error) {
	if groupSize < 1 {
		groupSize = 1
	}
	agentIDs := []string{}
	total := len(configPaths)

	pm.mu.Lock()
	pm.spawnNameStyle = nameStyle
	pm.spawnNameBase = nameBase
	pm.mu.Unlock()

	baseIndex := pm.SyncNextAgentIndex()
	pm.mu.Lock()
	pm.nextAgentIndex = baseIndex + total
	pm.mu.Unlock()

	pm.manager.mu.Lock()
	for i, cfg := range configPaths {
		agentID := fmt.Sprintf("agent_%03d", baseIndex+i)
		if _, ok := pm.manager.Agents[agentID]; !ok {
			a := newAgentStatus(agentID)
			a.PID = intPtr(0)
			a.Config = strPtr(cfg)
			a.State = "queued"
			pm.manager.Agents[agentID] = a
		}
	}
	pm.manager.mu.Unlock()
	pm.manager.BroadcastStatus()

	for groupStart := 0; groupStart < total; groupStart += groupSize {
		groupEnd := groupStart + groupSize
		if groupEnd > total {
			groupEnd = total
		}
		for i := groupStart; i < groupEnd; i++ {
			bid := fmt.Sprintf("agent_%03d", baseIndex+i)
			if _, err := pm.SpawnAgent(ctx, configPaths[i], bid); err != nil {
				pm.logger.Error("agent_spawn_failed_in_group", "agent_id", bid, "config", configPaths[i], "error", err.Error())
			} else {
				agentIDs = append(agentIDs, bid)
			}
		}
		if groupEnd < total {
			select {
			case <-ctx.Done():
				return agentIDs, ctx.Err()
			case <-time.After(time.Duration(groupDelay * float64(time.Second))):
			}
		}
	}
	pm.logger.Info("swarm_spawn_complete", "started", len(agentIDs), "total", total)
	return agentIDs, nil
}

// launchQueuedAgent spawns a queued agent and records a launch failure,
// mirroring _launch_queued_agent.
func (pm *AgentProcessManager) launchQueuedAgent(agentID, config string) {
	if _, err := pm.SpawnAgent(context.Background(), config, agentID); err != nil {
		pm.logger.Error("stale_queued_agent_launch_failed", "agent_id", agentID, "error", err.Error())
		pm.manager.mu.Lock()
		if a, ok := pm.manager.Agents[agentID]; ok {
			a.State = "error"
			a.ErrorMessage = strPtr("Launch failed: " + err.Error())
			a.ExitReason = strPtr("launch_failed")
		}
		pm.manager.mu.Unlock()
		pm.manager.BroadcastStatus()
	}
}

// setAgentExitState updates agent state fields based on a process exit code,
// mirroring _set_agent_exit_state.
func setAgentExitState(agent *AgentStatus, exitCode int) {
	now := nowUnix()
	if exitCode == 0 {
		if agent.State == "error" || (agent.ErrorMessage != nil && *agent.ErrorMessage != "") {
			agent.State = "error"
			if agent.ExitReason == nil || *agent.ExitReason == "" {
				agent.ExitReason = strPtr("reported_error_then_exit_0")
			}
		} else {
			agent.State = "completed"
			agent.CompletedAt = floatPtr(now)
			agent.StoppedAt = floatPtr(now)
			if agent.ExitReason == nil || *agent.ExitReason == "" {
				agent.ExitReason = strPtr("target_reached")
			}
		}
	} else {
		agent.State = "error"
		if agent.ExitReason == nil || *agent.ExitReason == "" {
			agent.ExitReason = strPtr(fmt.Sprintf("exit_code_%d", exitCode))
		}
		if agent.ErrorMessage == nil || *agent.ErrorMessage == "" {
			agent.ErrorMessage = strPtr(fmt.Sprintf("Process exited with code %d", exitCode))
		}
		agent.StoppedAt = floatPtr(now)
	}
}

// handleExitedProcesses updates state for any agent processes that have exited,
// mirroring _handle_exited_processes.
func (pm *AgentProcessManager) handleExitedProcesses() {
	type exitedProc struct {
		id   string
		code int
	}
	var exited []exitedProc
	pm.manager.mu.Lock()
	for bid, p := range pm.manager.Processes {
		if code, done := p.Poll(); done {
			exited = append(exited, exitedProc{bid, code})
		}
	}
	pm.manager.mu.Unlock()
	for _, e := range exited {
		pm.logger.Warn(EventAgentExited, "agent_id", e.id, "exit_code", e.code)
		pm.manager.mu.Lock()
		agent, ok := pm.manager.Agents[e.id]
		if !ok {
			delete(pm.manager.Processes, e.id)
			pm.manager.mu.Unlock()
			continue
		}
		setAgentExitState(agent, e.code)
		delete(pm.manager.Processes, e.id)
		pm.manager.mu.Unlock()
		pm.releaseAgentAccount(e.id)
		pm.manager.BroadcastStatus()
	}
}

// handleHeartbeatTimeouts marks agents that missed a heartbeat as error,
// mirroring _handle_heartbeat_timeouts.
func (pm *AgentProcessManager) handleHeartbeatTimeouts() {
	now := nowUnix()
	heartbeatTimeout := pm.manager.Config.HeartbeatTimeoutS
	type stopReq struct {
		id   string
		proc processHandle
	}
	var stops []stopReq
	var timedOut []string
	pm.manager.mu.Lock()
	for _, agent := range pm.manager.Agents {
		if isRunningState(agent.State) && now-agent.LastUpdateTime > heartbeatTimeout {
			pm.logger.Warn("agent_heartbeat_timeout", "agent_id", agent.AgentID, "timeout_s", heartbeatTimeout)
			agent.State = "error"
			agent.ErrorMessage = strPtr(fmt.Sprintf("No heartbeat in %.0fs - agent process may have crashed or is stuck", heartbeatTimeout))
			agent.ErrorType = strPtr("HeartbeatTimeout")
			agent.ErrorTimestamp = floatPtr(now)
			agent.ExitReason = strPtr("heartbeat_timeout")
			agent.StoppedAt = floatPtr(now)
			if proc, ok := pm.manager.Processes[agent.AgentID]; ok {
				delete(pm.manager.Processes, agent.AgentID)
				stops = append(stops, stopReq{agent.AgentID, proc})
			}
			timedOut = append(timedOut, agent.AgentID)
		}
	}
	pm.manager.mu.Unlock()
	for _, s := range stops {
		pm.stopProcessTree(s.id, s.proc, 0, stopTimeout)
	}
	for _, id := range timedOut {
		pm.releaseAgentAccount(id)
	}
	if len(timedOut) > 0 {
		pm.manager.BroadcastStatus()
	}
}

// handleStaleQueued launches agents that have been queued too long, mirroring
// _handle_stale_queued.
func (pm *AgentProcessManager) handleStaleQueued() {
	now := nowUnix()
	pm.manager.mu.Lock()
	agents := make([]*AgentStatus, 0, len(pm.manager.Agents))
	for _, a := range pm.manager.Agents {
		agents = append(agents, a)
	}
	desired := pm.manager.DesiredAgents
	pm.manager.mu.Unlock()

	for _, agent := range agents {
		pidZero := agent.PID != nil && *agent.PID == 0
		if agent.State != "queued" || !pidZero || agent.StartedAt != nil {
			pm.mu.Lock()
			delete(pm.queuedSince, agent.AgentID)
			pm.mu.Unlock()
			continue
		}
		pm.mu.Lock()
		queuedSince, seen := pm.queuedSince[agent.AgentID]
		if !seen {
			pm.queuedSince[agent.AgentID] = now
			pm.mu.Unlock()
			continue
		}
		pm.mu.Unlock()
		if now-queuedSince >= pm.queuedLaunchDelay {
			if desired > 0 {
				pm.mu.Lock()
				delete(pm.queuedSince, agent.AgentID)
				pm.mu.Unlock()
				continue
			}
			pm.logger.Warn("stale_queued_agent_launching", "agent_id", agent.AgentID, "queued_s", int(now-queuedSince))
			pm.mu.Lock()
			delete(pm.queuedSince, agent.AgentID)
			pm.mu.Unlock()
			if agent.Config != nil && *agent.Config != "" {
				cfg := *agent.Config
				id := agent.AgentID
				pm.spawnWG.Add(1)
				go func() {
					defer pm.spawnWG.Done()
					pm.launchQueuedAgent(id, cfg)
				}()
			} else {
				pm.manager.mu.Lock()
				agent.State = "stopped"
				agent.ExitReason = strPtr("no_config")
				pm.manager.mu.Unlock()
			}
		}
	}
}

// handleBustRespawn kills running agents flagged BUST when bust_respawn is on,
// mirroring _handle_bust_respawn. The bare AgentStatus has no activity_context,
// so this reads it from Extra.
func (pm *AgentProcessManager) handleBustRespawn() {
	pm.manager.mu.Lock()
	bust := pm.manager.BustRespawn
	paused := pm.manager.SwarmPaused
	pm.manager.mu.Unlock()
	if !bust || paused {
		return
	}
	now := nowUnix()
	type stopReq struct {
		id   string
		proc processHandle
		pid  int
	}
	var stops []stopReq
	pm.manager.mu.Lock()
	for _, agent := range pm.manager.Agents {
		if agent.State != "running" {
			continue
		}
		ctxVal, _ := agent.Extra["activity_context"].(string)
		if strings.ToUpper(ctxVal) != "BUST" {
			continue
		}
		pm.logger.Info("bust_respawn_killing_agent", "agent_id", agent.AgentID)
		agent.State = "stopped"
		agent.ExitReason = strPtr("bust_respawn")
		agent.StoppedAt = floatPtr(now)
		proc := pm.manager.Processes[agent.AgentID]
		delete(pm.manager.Processes, agent.AgentID)
		pid := 0
		if agent.PID != nil && *agent.PID > 0 {
			pid = *agent.PID
		}
		stops = append(stops, stopReq{agent.AgentID, proc, pid})
	}
	pm.manager.mu.Unlock()
	for _, s := range stops {
		pm.releaseAgentAccount(s.id)
		pm.stopProcessTree(s.id, s.proc, s.pid, stopTimeout)
	}
	pm.manager.BroadcastStatus()
}

// cleanupOldWorkerLogs deletes stale .prev and orphan worker log files,
// mirroring _cleanup_old_worker_logs.
func (pm *AgentProcessManager) cleanupOldWorkerLogs() int {
	logDir := pm.logDir
	if logDir == "" {
		logDir = "logs/workers"
	}
	info, err := os.Stat(logDir)
	if err != nil || !info.IsDir() {
		return 0
	}
	cutoff := nowUnix() - WorkerLogRetentionS
	pm.manager.mu.Lock()
	activeIDs := map[string]struct{}{}
	for id := range pm.manager.Agents {
		activeIDs[id] = struct{}{}
	}
	pm.manager.mu.Unlock()
	entries, err := os.ReadDir(logDir)
	if err != nil {
		return 0
	}
	deleted := 0
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		fi, err := e.Info()
		if err != nil {
			continue
		}
		if float64(fi.ModTime().UnixNano())/1e9 >= cutoff {
			continue
		}
		name := e.Name()
		stem := strings.TrimSuffix(name, filepath.Ext(name))
		_, active := activeIDs[stem]
		if strings.HasSuffix(name, ".prev") || (strings.HasSuffix(name, ".log") && !active) {
			if os.Remove(filepath.Join(logDir, name)) == nil {
				deleted++
			}
		}
	}
	if deleted > 0 {
		pm.logger.Info("worker_log_cleanup", "deleted", deleted)
	}
	return deleted
}

// MonitorProcesses runs the supervision loop until ctx is cancelled, mirroring
// monitor_processes.
func (pm *AgentProcessManager) MonitorProcesses(ctx context.Context) {
	iter := 0
	for {
		pm.handleExitedProcesses()
		pm.handleHeartbeatTimeouts()
		pm.handleStaleQueued()
		pm.handleBustRespawn()
		pm.handleDesiredState()
		iter++
		if iter%360 == 0 {
			pm.cleanupOldWorkerLogs()
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Duration(pm.manager.HealthCheckInterval) * time.Second):
		}
	}
}

// collectSpawnConfigs mirrors _collect_spawn_configs.
func collectSpawnConfigs(active, dead []*AgentStatus, last string) []string {
	pick := func(list []*AgentStatus) []string {
		var out []string
		for _, a := range list {
			if a.Config != nil && *a.Config != "" {
				out = append(out, *a.Config)
			}
		}
		return out
	}
	configs := pick(active)
	if len(configs) == 0 {
		configs = pick(dead)
	}
	if len(configs) == 0 && last != "" {
		configs = []string{last}
	}
	return configs
}

// handleDesiredState enforces the desired agent count, mirroring
// _handle_desired_state.
func (pm *AgentProcessManager) handleDesiredState() {
	pm.manager.mu.Lock()
	desired := pm.manager.DesiredAgents
	paused := pm.manager.SwarmPaused
	pm.manager.mu.Unlock()
	if desired <= 0 || paused {
		return
	}
	activeStates := map[string]struct{}{"running": {}, "queued": {}, "recovering": {}, "blocked": {}}

	// Prune terminal agents.
	type stopReq struct {
		id   string
		proc processHandle
	}
	var pruneStops []stopReq
	var dead []*AgentStatus
	terminal := map[string]struct{}{"error": {}, "stopped": {}, "completed": {}}
	pm.manager.mu.Lock()
	for _, a := range pm.manager.Agents {
		if _, ok := terminal[a.State]; ok {
			dead = append(dead, a)
		}
	}
	for _, d := range dead {
		if proc, ok := pm.manager.Processes[d.AgentID]; ok {
			delete(pm.manager.Processes, d.AgentID)
			pruneStops = append(pruneStops, stopReq{d.AgentID, proc})
		}
		delete(pm.manager.Agents, d.AgentID)
	}
	var active []*AgentStatus
	for _, a := range pm.manager.Agents {
		if _, ok := activeStates[a.State]; ok {
			active = append(active, a)
		}
	}
	deficit := desired - len(active)
	pm.manager.mu.Unlock()
	for _, d := range dead {
		pm.releaseAgentAccount(d.AgentID)
	}

	if deficit > 0 {
		pm.spawnToDesired(deficit, active, dead)
	} else if deficit < 0 {
		pm.killExcess(-deficit, active)
	}
	for _, s := range pruneStops {
		pm.stopProcessTree(s.id, s.proc, 0, stopTimeout)
	}
}

// spawnToDesired spawns deficit new agents, mirroring _spawn_to_desired.
func (pm *AgentProcessManager) spawnToDesired(deficit int, active, dead []*AgentStatus) {
	pm.mu.Lock()
	last := pm.lastSpawnConfig
	pm.mu.Unlock()
	configs := collectSpawnConfigs(active, dead, last)
	for i := 0; i < deficit; i++ {
		if len(configs) == 0 {
			break
		}
		config := configs[0]
		newID, err := pm.AllocateAgentID()
		if err != nil {
			return
		}
		pm.manager.mu.Lock()
		if _, ok := pm.manager.Agents[newID]; !ok {
			a := newAgentStatus(newID)
			a.PID = intPtr(0)
			a.Config = strPtr(config)
			a.State = "queued"
			pm.manager.Agents[newID] = a
		}
		desired := pm.manager.DesiredAgents
		pm.manager.mu.Unlock()
		pm.logger.Info("desired_state_spawning", "agent_id", newID, "deficit", deficit, "desired", desired)
		id := newID
		pm.spawnWG.Add(1)
		go func() {
			defer pm.spawnWG.Done()
			pm.launchQueuedAgent(id, config)
		}()
	}
}

// killExcess trims down to the desired count, mirroring _kill_excess.
func (pm *AgentProcessManager) killExcess(excess int, active []*AgentStatus) {
	sorted := make([]*AgentStatus, len(active))
	copy(sorted, active)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].AgentID > sorted[j].AgentID })
	if excess > len(sorted) {
		excess = len(sorted)
	}
	desired := pm.manager.DesiredAgents
	for _, agent := range sorted[:excess] {
		pm.logger.Info("desired_state_killing", "agent_id", agent.AgentID, "excess", excess, "desired", desired)
		pm.KillAgent(agent.AgentID)
		pm.releaseAgentAccount(agent.AgentID)
		pm.manager.mu.Lock()
		delete(pm.manager.Agents, agent.AgentID)
		delete(pm.manager.Processes, agent.AgentID)
		pm.manager.mu.Unlock()
	}
}
