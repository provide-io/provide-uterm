//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"net/http"
	"sort"
	"strings"
)

// handleListAgents serves GET /agents, mirroring list_agents.
func (s *Server) handleListAgents(w http.ResponseWriter, r *http.Request) {
	stateFilter := r.URL.Query().Get("state")
	hasStateFilter := r.URL.Query().Has("state")
	interactiveOnly := r.URL.Query().Get("interactive_only") == "true"

	s.M.mu.Lock()
	rows := []map[string]any{}
	for _, agent := range s.M.Agents {
		if hasStateFilter && agent.State != stateFilter {
			continue
		}
		configValue := ""
		if agent.Config != nil {
			configValue = *agent.Config
		}
		interactive := agent.SessionID != nil && *agent.SessionID != "" && strings.HasPrefix(configValue, "mcp://")
		if interactiveOnly && !interactive {
			continue
		}
		row := agent.toMap()
		row["interactive"] = interactive
		rows = append(rows, row)
	}
	s.M.mu.Unlock()

	sort.SliceStable(rows, func(i, j int) bool {
		li := floatOf(rows[i]["last_update_time"])
		lj := floatOf(rows[j]["last_update_time"])
		if li != lj {
			return li > lj
		}
		return strOf(rows[i]["agent_id"]) > strOf(rows[j]["agent_id"])
	})
	writeJSON(w, http.StatusOK, map[string]any{"total": len(rows), "agents": rows})
}

// handleAgentStatus serves GET /agent/{agent_id}/status, mirroring agent_status.
func (s *Server) handleAgentStatus(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	s.M.mu.Lock()
	agent, ok := s.M.Agents[agentID]
	var row map[string]any
	if ok {
		row = agent.toMap()
	}
	s.M.mu.Unlock()
	if !ok {
		jsonError(w, http.StatusNotFound, "Agent "+agentID+" not found")
		return
	}
	writeJSON(w, http.StatusOK, row)
}

// handleAgentDetails serves GET /agent/{agent_id}/details, mirroring
// agent_details (no plugin path).
func (s *Server) handleAgentDetails(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	s.M.mu.Lock()
	agent, ok := s.M.Agents[agentID]
	var row map[string]any
	if ok {
		row = agent.toMap()
	}
	s.M.mu.Unlock()
	if !ok {
		jsonError(w, http.StatusNotFound, "Agent "+agentID+" not found")
		return
	}
	writeJSON(w, http.StatusOK, row)
}

// handleAgentSessionData serves GET /agent/{agent_id}/session-data. The bare
// manager has no identity store, so it always 503s (mirroring the None branch).
func (s *Server) handleAgentSessionData(w http.ResponseWriter, _ *http.Request) {
	jsonError(w, http.StatusServiceUnavailable, "Identity store not configured")
}

// handleRegisterAgent serves POST /agent/{agent_id}/register, mirroring
// register_agent.
func (s *Server) handleRegisterAgent(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	if !agentIDPathRe.MatchString(agentID) {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": "invalid agent_id"})
		return
	}
	data, ok := decodeJSONMap(r)
	if !ok {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": "invalid request body"})
		return
	}
	var rejected []string
	for _, f := range operatorFields {
		if _, present := data[f]; present {
			rejected = append(rejected, f)
		}
	}
	if len(rejected) > 0 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
			"error": "register may not set operator-authority fields: [" + strings.Join(quoteAll(sortedStrings(rejected)), ", ") + "]",
		})
		return
	}
	now := nowUnix()
	s.M.mu.Lock()
	existing, exists := s.M.Agents[agentID]
	created := !exists
	if created && len(s.M.Agents) >= s.M.MaxAgents {
		s.M.mu.Unlock()
		writeJSON(w, http.StatusTooManyRequests, map[string]any{"error": "Max agents (" + itoaInt(s.M.MaxAgents) + ") reached"})
		return
	}
	var base map[string]any
	if created {
		base = map[string]any{"agent_id": agentID}
	} else {
		base = existing.toMap()
	}
	merged := map[string]any{}
	for k, v := range base {
		merged[k] = v
	}
	for k, v := range data {
		merged[k] = v
	}
	merged["agent_id"] = agentID
	sessionID := firstNonEmptyStr(data["session_id"], base["session_id"])
	if sessionID == "" {
		sessionID = agentID
	}
	merged["session_id"] = sessionID
	merged["last_update_time"] = now
	s.M.Agents[agentID] = agentStatusFromMap(merged)
	s.M.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "created": created})
}

// handleSetGoal serves POST /agent/{agent_id}/set-goal, mirroring set_goal.
func (s *Server) handleSetGoal(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	goal := r.URL.Query().Get("goal")
	s.M.mu.Lock()
	agent, ok := s.M.Agents[agentID]
	if !ok {
		s.M.mu.Unlock()
		jsonError(w, http.StatusNotFound, "Agent "+agentID+" not found")
		return
	}
	queued := queueManagerCommand(agent, "set_goal", map[string]any{"goal": goal})
	state := agent.State
	s.M.mu.Unlock()
	writeJSON(w, http.StatusOK, buildActionResponse(agentID, "set_goal", "worker_queue", false, true,
		map[string]any{"goal": goal, "queued_command": queued}, state))
}

// handleSetDirective serves POST /agent/{agent_id}/set-directive, mirroring
// set_directive.
func (s *Server) handleSetDirective(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	data, _ := decodeJSONMap(r)
	directive := strOf(data["directive"])
	turns := asInt(data["turns"])
	s.M.mu.Lock()
	agent, ok := s.M.Agents[agentID]
	if !ok {
		s.M.mu.Unlock()
		jsonError(w, http.StatusNotFound, "Agent "+agentID+" not found")
		return
	}
	queued := queueManagerCommand(agent, "set_directive", map[string]any{"directive": directive, "turns": turns})
	state := agent.State
	s.M.mu.Unlock()
	payload := queued["payload"].(map[string]any)
	writeJSON(w, http.StatusOK, buildActionResponse(agentID, "set_directive", "worker_queue", false, true,
		map[string]any{"directive": payload["directive"], "turns": payload["turns"], "queued_command": queued}, state))
}

// handleCancelCommand serves POST /agent/{agent_id}/cancel-command, mirroring
// cancel_command.
func (s *Server) handleCancelCommand(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	s.M.mu.Lock()
	agent, ok := s.M.Agents[agentID]
	if !ok {
		s.M.mu.Unlock()
		jsonError(w, http.StatusNotFound, "Agent "+agentID+" not found")
		return
	}
	cancelled := cancelPendingManagerCommand(agent, "operator_cancelled")
	state := agent.State
	s.M.mu.Unlock()
	if cancelled == nil {
		writeJSON(w, http.StatusOK, buildActionResponse(agentID, "cancel_command", "manager", false, false,
			map[string]any{"cancelled": false, "reason": "no_pending_command"}, state))
		return
	}
	s.M.BroadcastStatus()
	writeJSON(w, http.StatusOK, buildActionResponse(agentID, "cancel_command", "manager", true, false,
		map[string]any{"cancelled": true, "cancelled_command": cancelled}, state))
}

// handleDeleteAgent serves DELETE /agent/{agent_id}, mirroring kill.
func (s *Server) handleDeleteAgent(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	terminal := map[string]struct{}{"error": {}, "stopped": {}, "completed": {}}
	s.M.mu.Lock()
	agent, ok := s.M.Agents[agentID]
	if !ok {
		s.M.mu.Unlock()
		jsonError(w, http.StatusNotFound, "Agent "+agentID+" not found")
		return
	}
	_, isTerminal := terminal[agent.State]
	_, hasProc := s.M.Processes[agentID]
	if isTerminal || !hasProc {
		delete(s.M.Agents, agentID)
		desired := s.M.DesiredAgents
		s.M.mu.Unlock()
		s.M.PM.releaseAgentAccount(agentID)
		s.M.BroadcastStatus()
		writeJSON(w, http.StatusOK, buildActionResponse(agentID, "remove", "manager", true, false,
			map[string]any{"removed": agentID, "desired_agents": desired}, "removed"))
		return
	}
	s.M.mu.Unlock()
	s.M.KillAgent(agentID)
	s.M.mu.Lock()
	if s.M.DesiredAgents > 0 {
		s.M.DesiredAgents--
		if s.M.DesiredAgents < 0 {
			s.M.DesiredAgents = 0
		}
	}
	desired := s.M.DesiredAgents
	s.M.mu.Unlock()
	writeJSON(w, http.StatusOK, buildActionResponse(agentID, "remove", "manager", true, false,
		map[string]any{"killed": agentID, "desired_agents": desired}, "removed"))
}

// handleAgentEvents serves GET /agent/{agent_id}/events, mirroring
// get_agent_events.
func (s *Server) handleAgentEvents(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	s.M.mu.Lock()
	agent, ok := s.M.Agents[agentID]
	if !ok {
		s.M.mu.Unlock()
		writeJSON(w, http.StatusNotFound, map[string]any{"error": "Agent " + agentID + " not found", "events": []any{}})
		return
	}
	events := []map[string]any{}
	recentActions := agent.RecentActions
	for _, action := range recentActions {
		events = append(events, map[string]any{
			"timestamp": floatOf(action["time"]),
			"type":      "action",
			"action":    orDefault(action["action"], "UNKNOWN"),
			"sector":    action["sector"],
			"result":    action["result"],
			"details":   action["details"],
		})
	}
	if agent.ErrorTimestamp != nil && *agent.ErrorTimestamp != 0 {
		events = append(events, map[string]any{
			"timestamp":     *agent.ErrorTimestamp,
			"type":          "error",
			"error_type":    ptrOrNil(agent.ErrorType),
			"error_message": ptrOrNil(agent.ErrorMessage),
			"state":         agent.State,
		})
	}
	if agent.LastUpdateTime != 0 && len(recentActions) == 0 {
		events = append(events, map[string]any{
			"timestamp": agent.LastUpdateTime,
			"type":      "status_update",
			"state":     agent.State,
		})
	}
	state := agent.State
	s.M.mu.Unlock()

	sort.SliceStable(events, func(i, j int) bool {
		return floatOf(events[i]["timestamp"]) > floatOf(events[j]["timestamp"])
	})
	if len(events) > 50 {
		events = events[:50]
	}
	writeJSON(w, http.StatusOK, map[string]any{"agent_id": agentID, "state": state, "events": events})
}
