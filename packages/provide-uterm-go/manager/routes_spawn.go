//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"encoding/json"
	"net/http"
	"time"
)

// handleSpawn serves POST /swarm/spawn, mirroring spawn.
func (s *Server) handleSpawn(w http.ResponseWriter, r *http.Request) {
	configPath := r.URL.Query().Get("config_path")
	agentID := r.URL.Query().Get("agent_id")
	if _, err := validateConfigPath(configPath, s.M.Config.SpawnConfigDir, s.getenv); err != nil {
		jsonError(w, http.StatusBadRequest, err.Error())
		return
	}
	var err error
	if agentID == "" {
		agentID, err = s.M.PM.AllocateAgentID()
		if err != nil {
			jsonError(w, http.StatusBadRequest, err.Error())
			return
		}
	} else {
		s.M.PM.NoteAgentID(agentID)
	}
	agentID, err = s.M.SpawnAgent(r.Context(), configPath, agentID)
	if err != nil {
		jsonError(w, http.StatusBadRequest, err.Error())
		return
	}
	s.M.mu.Lock()
	var pid any
	if a, ok := s.M.Agents[agentID]; ok && a.PID != nil {
		pid = *a.PID
	}
	s.M.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"agent_id": agentID, "pid": pid})
}

// handleSpawnBatch serves POST /swarm/spawn-batch, mirroring spawn_batch.
func (s *Server) handleSpawnBatch(w http.ResponseWriter, r *http.Request) {
	req := SpawnBatchRequest{GroupSize: 1, GroupDelay: 12.0, NameStyle: "random"}
	if r.Body != nil {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil && err.Error() != "EOF" {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": "invalid request body"})
			return
		}
	}
	if len(req.ConfigPaths) < 1 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": "config_paths must not be empty"})
		return
	}
	if req.GroupSize <= 0 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": "group_size must be > 0"})
		return
	}
	if req.GroupDelay < 0 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": "group_delay must be >= 0"})
		return
	}
	for _, p := range req.ConfigPaths {
		if _, err := validateConfigPath(p, s.M.Config.SpawnConfigDir, s.getenv); err != nil {
			jsonError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	total := len(req.ConfigPaths)
	groups := (total + req.GroupSize - 1) / req.GroupSize

	s.M.PM.StartSpawnSwarm(req.ConfigPaths, req.GroupSize, req.GroupDelay, true, req.NameStyle, req.NameBase)

	s.M.mu.Lock()
	s.M.DesiredAgents = total
	s.M.mu.Unlock()
	s.M.PM.SyncNextAgentIndex()

	writeJSON(w, http.StatusOK, map[string]any{
		"status":                 "spawning",
		"total_agents":           total,
		"group_size":             req.GroupSize,
		"group_delay":            req.GroupDelay,
		"total_groups":           groups,
		"estimated_time_seconds": float64(groups-1) * req.GroupDelay,
		"desired_agents":         total,
	})
}

// handleSetDesired serves POST /swarm/desired, mirroring set_desired.
func (s *Server) handleSetDesired(w http.ResponseWriter, r *http.Request) {
	body, ok := decodeJSONMap(r)
	if !ok {
		jsonError(w, http.StatusBadRequest, "count must be an integer")
		return
	}
	raw, present := body["count"]
	count := 0
	if present {
		f, isNum := raw.(float64)
		if !isNum {
			jsonError(w, http.StatusBadRequest, "count must be an integer")
			return
		}
		count = int(f)
	}
	if count < 0 {
		jsonError(w, http.StatusBadRequest, "count must be >= 0")
		return
	}
	s.M.mu.Lock()
	s.M.DesiredAgents = count
	s.M.mu.Unlock()
	s.M.PM.SyncNextAgentIndex()
	writeJSON(w, http.StatusOK, map[string]any{"desired_agents": count})
}

// handleBustRespawn serves POST /swarm/bust-respawn, mirroring
// toggle_bust_respawn.
func (s *Server) handleBustRespawn(w http.ResponseWriter, r *http.Request) {
	body, _ := decodeJSONMap(r)
	s.M.mu.Lock()
	enabled := !s.M.BustRespawn
	if v, ok := body["enabled"].(bool); ok {
		enabled = v
	}
	s.M.BustRespawn = enabled
	s.M.mu.Unlock()
	s.M.BroadcastStatus()
	writeJSON(w, http.StatusOK, map[string]any{"bust_respawn": enabled})
}

// handleKillAll serves POST /swarm/kill-all.
func (s *Server) handleKillAll(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.M.KillAll())
}

// handleClear serves POST /swarm/clear.
func (s *Server) handleClear(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.M.ClearSwarm())
}

// handlePrune serves POST /swarm/prune.
func (s *Server) handlePrune(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.M.PruneDead())
}

// handlePauseSwarm serves POST /swarm/pause.
func (s *Server) handlePauseSwarm(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.M.PauseSwarm())
}

// handleResumeSwarm serves POST /swarm/resume.
func (s *Server) handleResumeSwarm(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.M.ResumeSwarm())
}

// agentControl is the shared body of pause/resume: it sets paused then queues
// the worker command (the bare manager has no local-runtime plugin).
func (s *Server) agentControl(w http.ResponseWriter, r *http.Request, action string, paused bool) {
	agentID := r.PathValue("agent_id")
	s.M.mu.Lock()
	agent, ok := s.M.Agents[agentID]
	if !ok {
		s.M.mu.Unlock()
		jsonError(w, http.StatusNotFound, "Agent "+agentID+" not found")
		return
	}
	agent.Paused = paused
	queued := queueManagerCommand(agent, action, map[string]any{})
	state := agent.State
	s.M.mu.Unlock()
	s.M.BroadcastStatus()
	writeJSON(w, http.StatusOK, buildActionResponse(agentID, action, "worker_queue", false, true,
		map[string]any{"paused": paused, "queued_command": queued}, state))
}

// handlePauseAgent serves POST /agent/{agent_id}/pause.
func (s *Server) handlePauseAgent(w http.ResponseWriter, r *http.Request) {
	s.agentControl(w, r, "pause", true)
}

// handleResumeAgent serves POST /agent/{agent_id}/resume.
func (s *Server) handleResumeAgent(w http.ResponseWriter, r *http.Request) {
	s.agentControl(w, r, "resume", false)
}

// handleRestartAgent serves POST /agent/{agent_id}/restart, mirroring
// restart_agent (worker_queue path).
func (s *Server) handleRestartAgent(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	s.M.mu.Lock()
	agent, ok := s.M.Agents[agentID]
	if !ok {
		s.M.mu.Unlock()
		jsonError(w, http.StatusNotFound, "Agent "+agentID+" not found")
		return
	}
	queued := queueManagerCommand(agent, "restart", map[string]any{})
	agent.Paused = false
	state := agent.State
	var config string
	if agent.Config != nil {
		config = *agent.Config
	}
	s.M.mu.Unlock()
	if config != "" {
		go s.respawnAfterRestartExit(agentID, config, 60.0, 0.5)
	}
	s.M.BroadcastStatus()
	writeJSON(w, http.StatusOK, buildActionResponse(agentID, "restart", "worker_queue", false, true,
		map[string]any{"queued_command": queued}, state))
}

// respawnAfterRestartExit waits for agentID to exit, then re-spawns it from
// configPath, mirroring _respawn_after_restart_exit.
func (s *Server) respawnAfterRestartExit(agentID, configPath string, exitTimeoutS, pollIntervalS float64) {
	deadline := time.Now().Add(time.Duration(exitTimeoutS * float64(time.Second)))
	exited := false
	for time.Now().Before(deadline) {
		time.Sleep(time.Duration(pollIntervalS * float64(time.Second)))
		s.M.mu.Lock()
		agent, ok := s.M.Agents[agentID]
		if !ok {
			s.M.mu.Unlock()
			return
		}
		st := agent.State
		s.M.mu.Unlock()
		if st == "completed" || st == "error" || st == "stopped" {
			exited = true
			break
		}
	}
	if !exited {
		s.M.logger.Warn("respawn_after_restart_exit_timeout", "agent_id", agentID, "timeout_s", exitTimeoutS)
		return
	}
	s.M.mu.Lock()
	if agent, ok := s.M.Agents[agentID]; ok {
		agent.PendingCommandSeq = 0
		agent.PendingCommandType = nil
		agent.PendingCommandPayload = map[string]any{}
	}
	s.M.mu.Unlock()
	if _, err := s.M.SpawnAgent(context.Background(), configPath, agentID); err != nil {
		s.M.logger.Warn("respawn_after_restart_failed", "agent_id", agentID, "config_path", configPath, "error", err.Error())
		return
	}
	s.M.logger.Info("respawn_after_restart_complete", "agent_id", agentID)
}
