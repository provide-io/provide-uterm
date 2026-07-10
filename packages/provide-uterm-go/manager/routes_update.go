//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import "net/http"

// floatTimestampFields are applied as nullable floats, mirroring
// _FLOAT_TIMESTAMP_FIELDS.
var floatTimestampFields = []string{"started_at", "stopped_at"}

// applyBaseFields applies the standard base status fields from payload onto
// agent, mirroring _apply_base_fields.
func applyBaseFields(a *AgentStatus, payload map[string]any) {
	if pid, ok := payload["pid"]; ok && floatOf(pid) != 0 {
		a.PID = intPtr(int(floatOf(pid)))
	}
	for _, f := range floatTimestampFields {
		if v, ok := payload[f]; ok {
			if v == nil {
				setFloatField(a, f, nil)
			} else {
				fv := floatOf(v)
				setFloatField(a, f, &fv)
			}
		}
	}
	if v, ok := payload["state"]; ok {
		a.State = strOf(v)
	}
	if v, ok := payload["last_action"]; ok {
		a.LastAction = strPtrOrNil(v)
	}
	if v, ok := payload["last_action_time"]; ok {
		a.LastActionTime = floatPtrOrNil(v)
	}
	if v, ok := payload["error_message"]; ok {
		a.ErrorMessage = strPtrOrNil(v)
	}
	if v, ok := payload["error_type"]; ok {
		a.ErrorType = strPtrOrNil(v)
	}
	if v, ok := payload["error_timestamp"]; ok {
		a.ErrorTimestamp = floatPtrOrNil(v)
	}
	if v, ok := payload["exit_reason"]; ok {
		a.ExitReason = strPtrOrNil(v)
	}
	if v, ok := payload["recent_actions"]; ok {
		a.RecentActions = toRowSlice(v)
	}
}

// setFloatField assigns one of the timestamp float pointers by name.
func setFloatField(a *AgentStatus, field string, v *float64) {
	switch field {
	case "started_at":
		a.StartedAt = v
	case "stopped_at":
		a.StoppedAt = v
	}
}

// checkStaleReport returns true if this report is older than the last accepted
// one, mirroring _check_stale_report.
func checkStaleReport(a *AgentStatus, payload map[string]any) bool {
	incoming := floatOf(payload["reported_at"])
	var stored float64
	if a.StatusReportedAt != nil {
		stored = *a.StatusReportedAt
	}
	if incoming > 0 && stored > 0 && incoming < stored {
		return true
	}
	if incoming > 0 {
		a.StatusReportedAt = floatPtr(incoming)
	}
	return false
}

// handleUpdateStatus serves POST /agent/{agent_id}/status, mirroring
// update_status.
func (s *Server) handleUpdateStatus(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("agent_id")
	if !agentIDPathRe.MatchString(agentID) {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": "invalid agent_id"})
		return
	}
	payload, ok := decodeJSONMap(r)
	if !ok {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": "invalid request body"})
		return
	}
	now := nowUnix()
	s.M.mu.Lock()
	agent, exists := s.M.Agents[agentID]
	if !exists {
		if len(s.M.Agents) >= s.M.MaxAgents {
			s.M.mu.Unlock()
			writeJSON(w, http.StatusTooManyRequests, map[string]any{"error": "Max agents (" + itoaInt(s.M.MaxAgents) + ") reached"})
			return
		}
		agent = newAgentStatus(agentID)
		agent.PID = intPtr(0)
		agent.State = "running"
		agent.StartedAt = floatPtr(now)
		s.M.Agents[agentID] = agent
	}

	ackSeq := asInt(payload["last_manager_command_seq"])
	acknowledgeCommand(agent, ackSeq)

	if checkStaleReport(agent, payload) {
		s.M.mu.Unlock()
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "ignored": "stale_report"})
		return
	}

	applyBaseFields(agent, payload)
	agent.LastUpdateTime = now
	paused := agent.Paused || s.M.SwarmPaused
	cmd := buildPendingCommandResponse(agent, ackSeq)
	s.M.mu.Unlock()

	s.M.BroadcastStatus()
	resp := map[string]any{"ok": true, "paused": paused}
	if cmd != nil {
		resp["manager_command"] = cmd
	}
	writeJSON(w, http.StatusOK, resp)
}
