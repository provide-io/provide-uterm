//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"math"
	"strings"
	"testing"
)

// A non-finite uptime must not collapse the whole status to {}: it is
// emitted as null (Pydantic's ser_json_inf_nan="null" default) and every
// other field survives.
func TestSwarmStatusToMapNullsNonFiniteUptime(t *testing.T) {
	for name, uptime := range map[string]float64{
		"nan":  math.NaN(),
		"+inf": math.Inf(1),
		"-inf": math.Inf(-1),
	} {
		t.Run(name, func(t *testing.T) {
			s := &SwarmStatus{TotalAgents: 1, Running: 1, UptimeSeconds: uptime, Agents: []*AgentStatus{newAgentStatus("agent_001")}}
			m := s.toMap()
			v, ok := m["uptime_seconds"]
			if !ok || v != nil {
				t.Fatalf("uptime_seconds = %v (present=%v), want null", v, ok)
			}
			if m["total_agents"] != float64(1) || m["running"] != float64(1) {
				t.Fatalf("status fields lost: %v", m)
			}
			if agents, _ := m["agents"].([]any); len(agents) != 1 {
				t.Fatalf("agents = %v, want 1 row", m["agents"])
			}
			if s.UptimeSeconds == 0 {
				t.Fatal("toMap mutated the receiver")
			}
			b, err := json.Marshal(s)
			if err != nil || !strings.Contains(string(b), `"uptime_seconds":null`) {
				t.Fatalf("MarshalJSON = %s, %v", b, err)
			}
		})
	}
}

func TestSwarmStatusToMapKeepsFiniteUptime(t *testing.T) {
	m := (&SwarmStatus{UptimeSeconds: 12.5}).toMap()
	if m["uptime_seconds"] != 12.5 {
		t.Fatalf("uptime_seconds = %v", m["uptime_seconds"])
	}
}

// When encoding fails for a reason toMap cannot sanitise (a NaN inside an
// agent's Extra), the failure is logged and the status is still well-formed,
// minus the agent rows.
func TestSwarmStatusToMapLogsAndDropsAgentsOnEncodeFailure(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })

	bad := newAgentStatus("agent_001")
	bad.Extra = map[string]any{"score": math.NaN()}
	m := (&SwarmStatus{TotalAgents: 1, UptimeSeconds: 3, Agents: []*AgentStatus{bad}}).toMap()

	if m["total_agents"] != float64(1) || m["uptime_seconds"] != float64(3) {
		t.Fatalf("status fields lost: %v", m)
	}
	if agents, ok := m["agents"].([]any); !ok || len(agents) != 0 {
		t.Fatalf("agents = %#v, want empty list", m["agents"])
	}
	if !strings.Contains(buf.String(), "swarm_status_encode_failed") {
		t.Fatalf("expected warning log, got %q", buf.String())
	}
}
