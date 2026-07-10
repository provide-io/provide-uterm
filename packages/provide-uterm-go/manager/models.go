//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import "encoding/json"

// strPtr / floatPtr / intPtr are small helpers for the many nullable fields.
func strPtr(s string) *string     { return &s }
func floatPtr(f float64) *float64 { return &f }
func intPtr(i int) *int           { return &i }

// AgentStatus holds the game-agnostic status fields shared by all agent types,
// mirroring AgentStatusBase in manager/models.py. Unknown fields carried by a
// worker self-report (Pydantic extra="allow") are preserved in Extra so the
// JSON round-trips faithfully.
type AgentStatus struct {
	AgentID               string           `json:"agent_id"`
	SessionID             *string          `json:"session_id"`
	State                 string           `json:"state"`
	PID                   *int             `json:"pid"`
	Config                *string          `json:"config"`
	StartedAt             *float64         `json:"started_at"`
	StoppedAt             *float64         `json:"stopped_at"`
	CompletedAt           *float64         `json:"completed_at"`
	LastUpdateTime        float64          `json:"last_update_time"`
	ErrorMessage          *string          `json:"error_message"`
	ErrorType             *string          `json:"error_type"`
	ErrorTimestamp        *float64         `json:"error_timestamp"`
	ExitReason            *string          `json:"exit_reason"`
	LastAction            *string          `json:"last_action"`
	LastActionTime        *float64         `json:"last_action_time"`
	StatusReportedAt      *float64         `json:"status_reported_at"`
	RecentActions         []map[string]any `json:"recent_actions"`
	IsHijacked            bool             `json:"is_hijacked"`
	HijackedBy            *string          `json:"hijacked_by"`
	HijackedAt            *float64         `json:"hijacked_at"`
	Paused                bool             `json:"paused"`
	RespawnedFrom         *string          `json:"respawned_from"`
	PendingCommandSeq     int              `json:"pending_command_seq"`
	PendingCommandType    *string          `json:"pending_command_type"`
	PendingCommandPayload map[string]any   `json:"pending_command_payload"`
	ManagerCommandHistory []map[string]any `json:"manager_command_history"`

	// Extra holds game-specific fields injected by a worker self-report
	// (extra="allow" parity). Keys that collide with a known field are
	// ignored on output (the known field wins).
	Extra map[string]any `json:"-"`
}

// newAgentStatus builds an AgentStatus with the model defaults, matching the
// AgentStatusBase Pydantic defaults (state="unknown", collections empty).
func newAgentStatus(agentID string) *AgentStatus {
	return &AgentStatus{
		AgentID:               agentID,
		State:                 "unknown",
		RecentActions:         []map[string]any{},
		PendingCommandPayload: map[string]any{},
		ManagerCommandHistory: []map[string]any{},
		Extra:                 map[string]any{},
	}
}

// toMap serializes the agent to the same key/value shape as Pydantic
// model_dump(), including all known fields plus any Extra fields.
func (a *AgentStatus) toMap() map[string]any {
	// Marshal known fields via the struct tags, then decode back to a map so
	// null/[]/{} defaults match Pydantic exactly.
	type alias AgentStatus
	b, _ := json.Marshal((*alias)(a))
	m := map[string]any{}
	_ = json.Unmarshal(b, &m)
	for k, v := range a.Extra {
		if _, known := m[k]; known {
			continue
		}
		m[k] = v
	}
	return m
}

// MarshalJSON emits the model_dump() shape.
func (a *AgentStatus) MarshalJSON() ([]byte, error) {
	return json.Marshal(a.toMap())
}

// SwarmStatus is the overall swarm status snapshot, mirroring SwarmStatus in
// manager/models.py. Extra carries aggregate fields a plugin may inject.
type SwarmStatus struct {
	TotalAgents               int            `json:"total_agents"`
	Running                   int            `json:"running"`
	Completed                 int            `json:"completed"`
	Errors                    int            `json:"errors"`
	Stopped                   int            `json:"stopped"`
	UptimeSeconds             float64        `json:"uptime_seconds"`
	TimeseriesFile            *string        `json:"timeseries_file"`
	TimeseriesIntervalSeconds int            `json:"timeseries_interval_seconds"`
	TimeseriesSamples         int            `json:"timeseries_samples"`
	SwarmPaused               bool           `json:"swarm_paused"`
	BustRespawn               bool           `json:"bust_respawn"`
	DesiredAgents             int            `json:"desired_agents"`
	Agents                    []*AgentStatus `json:"agents"`

	Extra map[string]any `json:"-"`
}

// toMap serializes SwarmStatus to the model_dump() shape.
func (s *SwarmStatus) toMap() map[string]any {
	type alias SwarmStatus
	b, _ := json.Marshal((*alias)(s))
	m := map[string]any{}
	_ = json.Unmarshal(b, &m)
	for k, v := range s.Extra {
		if _, known := m[k]; known {
			continue
		}
		m[k] = v
	}
	return m
}

// MarshalJSON emits the model_dump() shape.
func (s *SwarmStatus) MarshalJSON() ([]byte, error) {
	return json.Marshal(s.toMap())
}

// knownAgentFields is the set of AgentStatus JSON keys (everything Pydantic
// declares on AgentStatusBase). Any other key in a self-report body is an
// extra="allow" field preserved in Extra.
var knownAgentFields = map[string]struct{}{
	"agent_id": {}, "session_id": {}, "state": {}, "pid": {}, "config": {},
	"started_at": {}, "stopped_at": {}, "completed_at": {}, "last_update_time": {},
	"error_message": {}, "error_type": {}, "error_timestamp": {}, "exit_reason": {},
	"last_action": {}, "last_action_time": {}, "status_reported_at": {},
	"recent_actions": {}, "is_hijacked": {}, "hijacked_by": {}, "hijacked_at": {},
	"paused": {}, "respawned_from": {}, "pending_command_seq": {},
	"pending_command_type": {}, "pending_command_payload": {}, "manager_command_history": {},
}

// agentStatusFromMap builds an AgentStatus from a decoded JSON object, applying
// the model defaults for missing fields (matching Pydantic model_validate) and
// preserving unknown keys in Extra (extra="allow").
func agentStatusFromMap(data map[string]any) *AgentStatus {
	id, _ := data["agent_id"].(string)
	a := newAgentStatus(id)
	b, _ := json.Marshal(data)
	_ = json.Unmarshal(b, a)
	if a.RecentActions == nil {
		a.RecentActions = []map[string]any{}
	}
	if a.PendingCommandPayload == nil {
		a.PendingCommandPayload = map[string]any{}
	}
	if a.ManagerCommandHistory == nil {
		a.ManagerCommandHistory = []map[string]any{}
	}
	extra := map[string]any{}
	for k, v := range data {
		if _, known := knownAgentFields[k]; !known {
			extra[k] = v
		}
	}
	a.Extra = extra
	return a
}

// SpawnBatchRequest is the request body for POST /swarm/spawn-batch, mirroring
// SpawnBatchRequest in manager/models.py.
type SpawnBatchRequest struct {
	ConfigPaths []string `json:"config_paths"`
	GroupSize   int      `json:"group_size"`
	GroupDelay  float64  `json:"group_delay"`
	NameStyle   string   `json:"name_style"`
	NameBase    string   `json:"name_base"`
}
