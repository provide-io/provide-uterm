//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// --- mcp tools gaps ---

func TestManagerToolsSwarmStatusTelemetryGuard(t *testing.T) {
	m := testManager(t, nil)
	tools := NewManagerTools(m)
	// Exercises the !includeTelemetry && len(fields)>0 guard. The inner strip
	// loop is unreachable because GetSwarmStatus().toMap() JSON-round-trips the
	// agents into []any (never []*AgentStatus), so nothing is actually stripped.
	tools.AgentTelemetryFields = map[string]struct{}{"error_message": {}}
	a := newAgentStatus("agent_001")
	a.ErrorMessage = strPtr("boom")
	m.Agents["agent_001"] = a

	data := tools.SwarmStatus(false)
	if data["total_agents"] != float64(1) {
		t.Fatalf("total_agents = %v", data["total_agents"])
	}
	// includeTelemetry=true short-circuits the guard entirely.
	if got := tools.SwarmStatus(true); got["total_agents"] != float64(1) {
		t.Fatalf("include telemetry total = %v", got["total_agents"])
	}
}

func TestManagerToolsSpawnBatch(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	tools := NewManagerTools(m)
	cfg := writeConfig(t, m, "agent.yaml")

	// Invalid path -> error envelope.
	if got := tools.SwarmSpawnBatch([]string{"/outside/x.yaml"}, 1, 0, "random", ""); got["error"] == nil {
		t.Fatalf("expected error for outside path, got %v", got)
	}
	// Valid path -> spawning; group_size<1 is clamped to 1.
	got := tools.SwarmSpawnBatch([]string{cfg}, 0, 0, "random", "")
	if got["status"] != "spawning" || got["group_size"].(int) != 1 {
		t.Fatalf("spawn batch = %v", got)
	}
	m.PM.CancelSpawn()
	m.KillAll()
}

func TestManagerToolsAgentListFilter(t *testing.T) {
	m := testManager(t, nil)
	tools := NewManagerTools(m)
	r := newAgentStatus("agent_001")
	r.State = "running"
	e := newAgentStatus("agent_002")
	e.State = "error"
	m.Agents["agent_001"] = r
	m.Agents["agent_002"] = e
	got := tools.AgentList("running")
	if int(got["total"].(int)) != 1 {
		t.Fatalf("filtered total = %v, want 1", got["total"])
	}
}

func TestManagerToolsAgentKillWithProcess(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	tools := NewManagerTools(m)
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	m.mu.Lock()
	m.DesiredAgents = 3
	m.mu.Unlock()
	got := tools.AgentKill("agent_001")
	if got["action"] != "kill" || got["state"] != "stopped" {
		t.Fatalf("kill = %v", got)
	}
	m.mu.Lock()
	desired := m.DesiredAgents
	m.mu.Unlock()
	if desired != 2 {
		t.Fatalf("desired after kill = %d, want 2", desired)
	}
}

func TestManagerToolsMissingAgentErrors(t *testing.T) {
	m := testManager(t, nil)
	tools := NewManagerTools(m)
	if tools.AgentKill("nope")["error"] == nil {
		t.Fatal("kill missing should error")
	}
	if tools.AgentPause("nope")["error"] == nil {
		t.Fatal("pause missing should error")
	}
	if tools.AgentRestart("nope")["error"] == nil {
		t.Fatal("restart missing should error")
	}
}

func TestFloatPtrAnyNonNil(t *testing.T) {
	if got := floatPtrAny(floatPtr(2.5)); got != 2.5 {
		t.Fatalf("floatPtrAny = %v, want 2.5", got)
	}
	if floatPtrAny(nil) != nil {
		t.Fatal("floatPtrAny(nil) should be nil")
	}
}

// --- routes_agent_ops gaps ---

func TestListAgentsFilters(t *testing.T) {
	s, h := newTestServer(t, nil)
	a1 := seedAgent(s, "agent_001") // interactive (mcp:// + session)
	a1.LastUpdateTime = 100
	a2 := seedAgent(s, "agent_002")
	a2.LastUpdateTime = 200
	a2.State = "error"
	a3 := newAgentStatus("agent_003") // non-interactive: no session
	a3.State = "running"
	a3.LastUpdateTime = 50
	s.M.Agents["agent_003"] = a3

	// state filter.
	if _, body := doJSON(t, h, "GET", "/agents?state=running", ""); int(body["total"].(float64)) != 2 {
		t.Fatalf("state filter total = %v, want 2", body["total"])
	}
	// interactive_only filter.
	if _, body := doJSON(t, h, "GET", "/agents?interactive_only=true", ""); int(body["total"].(float64)) != 2 {
		t.Fatalf("interactive filter total = %v", body["total"])
	}
	// no filter sorts newest first.
	_, body := doJSON(t, h, "GET", "/agents", "")
	agents := body["agents"].([]any)
	if len(agents) != 3 {
		t.Fatalf("all agents = %d", len(agents))
	}
}

func TestSetGoalDirectiveCancelMissing(t *testing.T) {
	s, h := newTestServer(t, nil)
	seedAgent(s, "agent_001")

	if code, _ := doJSON(t, h, "POST", "/agent/nope/set-goal?goal=x", ""); code != 404 {
		t.Fatalf("set-goal missing: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/agent/nope/set-directive", `{"directive":"d"}`); code != 404 {
		t.Fatalf("set-directive missing: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/agent/nope/cancel-command", `{}`); code != 404 {
		t.Fatalf("cancel missing: %d", code)
	}
	// set-directive with a JSON body (directive + turns).
	code, resp := doJSON(t, h, "POST", "/agent/agent_001/set-directive", `{"directive":"go","turns":3}`)
	if code != 200 || resp["action"] != "set_directive" {
		t.Fatalf("set-directive body: %d %v", code, resp)
	}
}

func TestCancelPendingCommand(t *testing.T) {
	s, h := newTestServer(t, nil)
	seedAgent(s, "agent_001")
	// Queue a command, then cancel it (cancelled != nil branch).
	if code, _ := doJSON(t, h, "POST", "/agent/agent_001/set-goal?goal=win", ""); code != 200 {
		t.Fatalf("set-goal: %d", code)
	}
	code, resp := doJSON(t, h, "POST", "/agent/agent_001/cancel-command", `{}`)
	if code != 200 {
		t.Fatalf("cancel: %d", code)
	}
	result := resp["result"].(map[string]any)
	if result["cancelled"] != true {
		t.Fatalf("expected cancelled=true, got %v", result)
	}
}

func TestAgentEventsRichAndTruncated(t *testing.T) {
	s, h := newTestServer(t, nil)
	a := seedAgent(s, "agent_001")
	a.ErrorTimestamp = floatPtr(500)
	a.ErrorType = strPtr("Boom")
	a.ErrorMessage = strPtr("kaboom")
	actions := make([]map[string]any, 0, 60)
	for i := 0; i < 60; i++ {
		actions = append(actions, map[string]any{"time": float64(i), "action": "MOVE"})
	}
	a.RecentActions = actions

	_, body := doJSON(t, h, "GET", "/agent/agent_001/events", "")
	events := body["events"].([]any)
	if len(events) != 50 {
		t.Fatalf("events = %d, want 50 (truncated)", len(events))
	}

	// A second agent with no actions but a heartbeat -> status_update event.
	b := seedAgent(s, "agent_002")
	b.RecentActions = []map[string]any{}
	b.LastUpdateTime = 123
	_, body2 := doJSON(t, h, "GET", "/agent/agent_002/events", "")
	evs2 := body2["events"].([]any)
	if len(evs2) != 1 || evs2[0].(map[string]any)["type"] != "status_update" {
		t.Fatalf("status_update event = %v", evs2)
	}
}

// --- routes_update gaps ---

func TestUpdateStatusRichAndStale(t *testing.T) {
	s, h := newTestServer(t, nil)
	seedAgent(s, "agent_001")

	body := `{"pid":42,"started_at":100,"stopped_at":null,"state":"running",` +
		`"last_action":"MOVE","last_action_time":5,"error_message":"e","error_type":"T",` +
		`"error_timestamp":9,"exit_reason":"done","recent_actions":[{"action":"X"}],"reported_at":100}`
	if code, _ := doJSON(t, h, "POST", "/agent/agent_001/status", body); code != 200 {
		t.Fatalf("rich update: %d", code)
	}
	m := s.M
	m.mu.Lock()
	a := m.Agents["agent_001"]
	pid, startedAt := *a.PID, *a.StartedAt
	m.mu.Unlock()
	if pid != 42 || startedAt != 100 {
		t.Fatalf("applied fields wrong: pid=%d started=%v", pid, startedAt)
	}
	// An older reported_at is ignored as stale.
	_, resp := doJSON(t, h, "POST", "/agent/agent_001/status", `{"state":"error","reported_at":50}`)
	if resp["ignored"] != "stale_report" {
		t.Fatalf("expected stale_report, got %v", resp)
	}
	if m.agentState("agent_001") != "running" {
		t.Fatal("stale report should not have applied state=error")
	}
}

// --- routes_spawn gaps ---

func TestRespawnAfterRestartExit(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	s := &Server{M: m, getenv: func(string) string { return "" }}
	cfg := writeConfig(t, m, "agent.yaml")

	// Terminal agent -> respawns from config.
	a := newAgentStatus("agent_001")
	a.State = "stopped"
	a.Config = strPtr(cfg)
	m.Agents["agent_001"] = a
	s.respawnAfterRestartExit("agent_001", cfg, 5.0, 0.01)
	if m.agentState("agent_001") != "running" {
		t.Fatalf("state = %q, want running after respawn", m.agentState("agent_001"))
	}
	m.KillAll()

	// Never-exiting agent -> timeout branch (no respawn).
	r := newAgentStatus("agent_002")
	r.State = "running"
	m.Agents["agent_002"] = r
	s.respawnAfterRestartExit("agent_002", cfg, 0.02, 0.01)
	if m.hasProcess("agent_002") {
		t.Fatal("timeout branch must not respawn")
	}

	// Missing agent -> immediate return.
	s.respawnAfterRestartExit("ghost", cfg, 5.0, 0.01)

	// Terminal agent but a broken config path -> respawn fails, stays stopped.
	d := newAgentStatus("agent_003")
	d.State = "stopped"
	m.Agents["agent_003"] = d
	s.respawnAfterRestartExit("agent_003", "/no/such.yaml", 5.0, 0.01)
	if m.agentState("agent_003") != "stopped" {
		t.Fatalf("failed respawn state = %q, want stopped", m.agentState("agent_003"))
	}
}

func TestRestartAgentSpawnsRespawnGoroutine(t *testing.T) {
	s, h := newTestServer(t, nil)
	a := seedAgent(s, "agent_001")
	a.Config = strPtr("/no/such.yaml") // non-empty -> respawn goroutine launched
	code, _ := doJSON(t, h, "POST", "/agent/agent_001/restart", `{}`)
	if code != 200 {
		t.Fatalf("restart: %d", code)
	}
	// Remove the agent so the background respawn poller exits promptly.
	s.M.mu.Lock()
	delete(s.M.Agents, "agent_001")
	s.M.mu.Unlock()
	// Missing agent -> 404.
	if code, _ := doJSON(t, h, "POST", "/agent/nope/restart", `{}`); code != 404 {
		t.Fatalf("restart missing: %d", code)
	}
}

func TestSpawnBatchValidation(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	s := &Server{M: m, getenv: func(string) string { return "" }}
	h := s.Routes()
	cfg := writeConfig(t, m, "agent.yaml")

	if code, _ := doJSON(t, h, "POST", "/swarm/spawn-batch", `{not json`); code != 422 {
		t.Fatalf("bad body: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/swarm/spawn-batch", `{"config_paths":["`+cfg+`"],"group_size":0}`); code != 422 {
		t.Fatalf("group_size 0: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/swarm/spawn-batch", `{"config_paths":["`+cfg+`"],"group_size":1,"group_delay":-1}`); code != 422 {
		t.Fatalf("neg delay: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/swarm/spawn-batch", `{"config_paths":["/outside.yaml"],"group_size":1}`); code != 400 {
		t.Fatalf("outside path: %d", code)
	}
}

func TestSetDesiredValidation(t *testing.T) {
	_, h := newTestServer(t, nil)
	if code, _ := doJSON(t, h, "POST", "/swarm/desired", `{bad`); code != 400 {
		t.Fatalf("bad body: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/swarm/desired", `{"count":"x"}`); code != 400 {
		t.Fatalf("non-number count: %d", code)
	}
	if code, _ := doJSON(t, h, "POST", "/swarm/desired", `{"count":-1}`); code != 400 {
		t.Fatalf("negative count: %d", code)
	}
}

func TestSpawnAllocatesID(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	s := &Server{M: m, getenv: func(string) string { return "" }}
	h := s.Routes()
	cfg := writeConfig(t, m, "agent.yaml")
	// No agent_id -> the server allocates one.
	code, body := doJSON(t, h, "POST", "/swarm/spawn?config_path="+cfg, "")
	if code != 200 || body["agent_id"] == nil {
		t.Fatalf("spawn alloc: %d %v", code, body)
	}
	m.KillAll()
}

// --- routes_status gap ---

func TestTimeseriesSummaryWindowParam(t *testing.T) {
	_, h := newTestServer(t, nil)
	if code, _ := doJSON(t, h, "GET", "/swarm/timeseries/summary?window_minutes=60", ""); code != 200 {
		t.Fatalf("summary with window: %d", code)
	}
	if code, _ := doJSON(t, h, "GET", "/swarm/timeseries/summary?window_minutes=notnum", ""); code != 200 {
		t.Fatalf("summary bad window: %d", code)
	}
}

// --- timeseries gaps ---

type fakeTSPlugin struct{}

func (fakeTSPlugin) BuildRow(status *SwarmStatus, reason string) map[string]any {
	return map[string]any{"reason": reason, "n": status.TotalAgents}
}
func (fakeTSPlugin) GetSummary(_ *TimeseriesManager, windowMinutes int) map[string]any {
	return map[string]any{"window_minutes": windowMinutes, "plugin": true}
}

func TestTimeseriesPluginPaths(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 0, fakeTSPlugin{}, func() float64 { return 5 })
	if tm.IntervalS != 1 {
		t.Fatalf("interval clamp = %d, want 1", tm.IntervalS)
	}
	tm.WriteSample(fixedStatus(), "startup") // buildRow plugin branch
	if got := tm.GetSummary(30); got["plugin"] != true {
		t.Fatalf("plugin summary = %v", got)
	}
	rows := tm.ReadTail(10)
	if len(rows) != 1 || rows[0]["reason"] != "startup" {
		t.Fatalf("plugin rows = %v", rows)
	}
}

func TestGetRecentBounds(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	tm.WriteSample(fixedStatus(), "a")
	tm.WriteSample(fixedStatus(), "b")
	// Exercises the limit<1 clamp (to 1) branch; a 1-line tail lands on the
	// file's trailing newline, so no rows come back (matches the reader).
	_ = tm.GetRecent(0)
	// Exercises the limit>5000 clamp (to 5000) branch and returns data.
	if got := tm.GetRecent(6000); len(got) == 0 {
		t.Fatal("GetRecent(6000) should return rows")
	}
}

func TestAsIntInt64(t *testing.T) {
	if asInt(int64(7)) != 7 {
		t.Fatal("asInt(int64) should coerce")
	}
	if asInt("nope") != 0 {
		t.Fatal("asInt(non-number) should be 0")
	}
}

func TestTimeseriesCleanupOld(t *testing.T) {
	dir := t.TempDir()
	tm := NewTimeseriesManager(fixedStatus, dir, 1, nil, func() float64 { return 1e9 })
	// An old sibling timeseries file gets swept.
	oldFile := filepath.Join(dir, "swarm_timeseries_00010101_000000.jsonl")
	if err := os.WriteFile(oldFile, []byte("{}\n"), 0o644); err != nil {
		t.Fatalf("write old: %v", err)
	}
	oldTime := time.Unix(0, 0)
	if err := os.Chtimes(oldFile, oldTime, oldTime); err != nil {
		t.Fatalf("chtimes: %v", err)
	}
	tm.cleanupOld(1000)
	if _, err := os.Stat(oldFile); !os.IsNotExist(err) {
		t.Fatal("old timeseries file should be swept")
	}
}

// --- models gaps ---

func TestAgentStatusToMapExtra(t *testing.T) {
	a := newAgentStatus("agent_001")
	a.Extra["custom"] = "v"
	a.Extra["state"] = "SHOULD_LOSE" // collides with a known field -> ignored
	m := a.toMap()
	if m["custom"] != "v" {
		t.Fatalf("extra custom = %v", m["custom"])
	}
	if m["state"] != "unknown" {
		t.Fatalf("known field must win over extra: state=%v", m["state"])
	}
}

func TestSwarmStatusToMapExtra(t *testing.T) {
	s := &SwarmStatus{TotalAgents: 1, Agents: []*AgentStatus{}, Extra: map[string]any{
		"aggregate":    42,
		"total_agents": 999, // collides -> ignored
	}}
	m := s.toMap()
	// Extra values are merged verbatim (not JSON round-tripped), so an int
	// stays an int.
	if m["aggregate"].(int) != 42 {
		t.Fatalf("aggregate = %v (%T)", m["aggregate"], m["aggregate"])
	}
	if int(m["total_agents"].(float64)) != 1 {
		t.Fatalf("known field must win: total_agents=%v", m["total_agents"])
	}
}

func TestAgentStatusFromMapNullsAndExtra(t *testing.T) {
	data := map[string]any{
		"agent_id":                "agent_001",
		"recent_actions":          nil,
		"pending_command_payload": nil,
		"manager_command_history": nil,
		"game_field":              "kept",
	}
	a := agentStatusFromMap(data)
	if a.RecentActions == nil || a.PendingCommandPayload == nil || a.ManagerCommandHistory == nil {
		t.Fatal("nil collections must default to empty, not nil")
	}
	if a.Extra["game_field"] != "kept" {
		t.Fatalf("extra field = %v", a.Extra["game_field"])
	}
}

// --- process gaps ---

func TestLoadWorkerTypeFlatScanAndMissing(t *testing.T) {
	m := testManager(t, nil)
	dir := m.Config.SpawnConfigDir
	yamlPath := filepath.Join(dir, "flat.yaml")
	if err := os.WriteFile(yamlPath, []byte("worker_type: custom\nfoo: bar\n"), 0o644); err != nil {
		t.Fatalf("write yaml: %v", err)
	}
	wt, raw := m.PM.loadWorkerType(yamlPath)
	if wt != "custom" || raw["worker_type"] != "custom" {
		t.Fatalf("flat scan wt=%q raw=%v", wt, raw)
	}
	// Missing file -> default.
	if wt, _ := m.PM.loadWorkerType("/no/such/file.yaml"); wt != "default" {
		t.Fatalf("missing wt = %q, want default", wt)
	}
}

func TestGetRegistryEntryUnknownAndDefault(t *testing.T) {
	m := testManager(t, nil)
	// Single default registry -> "default" resolves via the fallback.
	if _, err := m.PM.getRegistryEntry("default", "/cfg"); err != nil {
		t.Fatalf("default resolve: %v", err)
	}
	// Multiple entries + unknown type -> error.
	m.PM.workerRegistry = map[string]WorkerRegistryEntry{
		"a": fakeEntry{module: "a", wtypeName: "a"},
		"b": fakeEntry{module: "b", wtypeName: "b"},
	}
	if _, err := m.PM.getRegistryEntry("zzz", "/cfg"); err == nil {
		t.Fatal("unknown worker type should error")
	}
}

func TestSpawnProcessEmptyAndBadBinary(t *testing.T) {
	// Empty argv from the spawn command -> error.
	m := testManager(t, func(_, _, _ string) []string { return []string{} })
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err == nil ||
		!strings.Contains(err.Error(), "empty spawn command") {
		t.Fatalf("empty argv err = %v", err)
	}

	// A nonexistent binary -> exec start error.
	m2 := testManager(t, func(_, _, _ string) []string { return []string{"/no/such/binary_xyz"} })
	cfg2 := writeConfig(t, m2, "agent.yaml")
	if _, err := m2.SpawnAgent(context.Background(), cfg2, "agent_001"); err == nil {
		t.Fatal("bad binary should fail to spawn")
	}
}

func TestSignalGroupByPIDBadPID(t *testing.T) {
	// A pid that does not exist -> Getpgid error is surfaced.
	if err := signalGroupByPID(1<<30, syscall.SIGTERM); err == nil {
		t.Fatal("expected error for nonexistent pid")
	}
}

// --- httpjson gaps ---

func TestDecodeJSONMapEdgeCases(t *testing.T) {
	// nil body.
	req := httptest.NewRequest("POST", "/x", nil)
	req.Body = nil
	if m, ok := decodeJSONMap(req); !ok || m == nil {
		t.Fatal("nil body should yield empty map, ok")
	}
	// JSON null body -> empty map.
	req2 := httptest.NewRequest("POST", "/x", strings.NewReader("null"))
	if m, ok := decodeJSONMap(req2); !ok || m == nil || len(m) != 0 {
		t.Fatalf("null body -> empty map: %v %v", m, ok)
	}
}

// --- app gap: webhook gate + CORS env override ---

func TestCreateManagerAppWebhookAndCORSEnv(t *testing.T) {
	cfg := DefaultManagerConfig()
	cfg.TimeseriesDir = t.TempDir()
	cfg.SpawnPolicyWebhookURL = "http://localhost:9/decision"
	getenv := func(k string) string {
		if k == "UTERM_CORS_ORIGINS" {
			return "http://a.example, http://b.example"
		}
		return ""
	}
	s, handler, err := CreateManagerApp(cfg, AppOptions{Getenv: getenv})
	if err != nil {
		t.Fatalf("create app: %v", err)
	}
	if _, ok := s.M.PM.policyGate.(*WebhookAgentSpawnPolicyGate); !ok {
		t.Fatalf("expected webhook policy gate, got %T", s.M.PM.policyGate)
	}
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest("GET", "/health", nil))
	if rec.Code != 200 {
		t.Fatalf("health: %d", rec.Code)
	}
}

// --- auth gaps ---

func TestExtractRequestTokenWebsocket(t *testing.T) {
	req := httptest.NewRequest("GET", "/ws?token=wstok", nil)
	req.Header.Set("Upgrade", "websocket")
	tok, passThrough := extractRequestToken(req)
	if tok != "wstok" || passThrough {
		t.Fatalf("ws token = %q passThrough=%v", tok, passThrough)
	}
}

// --- routes_helpers pure-function gaps ---

func TestCommandHistoryHelpers(t *testing.T) {
	a := newAgentStatus("agent_001")
	// Queue then acknowledge with a matching seq -> ack branch clears it.
	q := queueManagerCommand(a, "restart", map[string]any{})
	seq := asInt(q["seq"])
	acknowledgeCommand(a, seq)
	if a.PendingCommandSeq != 0 || a.PendingCommandType != nil {
		t.Fatal("acknowledge should clear the pending command")
	}
	// A non-matching ack is a no-op.
	_ = queueManagerCommand(a, "pause", map[string]any{})
	acknowledgeCommand(a, 999)
	if a.PendingCommandSeq == 0 {
		t.Fatal("non-matching ack should not clear")
	}

	// updateCommandHistory: seq<=0 is a no-op; matching seq updates the row.
	updateCommandHistory(a, 0, map[string]any{"x": 1})
	updateCommandHistory(a, a.PendingCommandSeq, map[string]any{"status": "poked"})

	// appendCommandHistory caps to the last 25 rows.
	for i := 0; i < 30; i++ {
		appendCommandHistory(a, map[string]any{"seq": i})
	}
	if len(a.ManagerCommandHistory) != 25 {
		t.Fatalf("history len = %d, want 25", len(a.ManagerCommandHistory))
	}
}

func TestSmallHelperCoercions(t *testing.T) {
	if floatOf(3) != 3 {
		t.Fatal("floatOf(int)")
	}
	if floatOf("x") != 0 {
		t.Fatal("floatOf(string) should be 0")
	}
	if orDefault(nil, "d") != "d" {
		t.Fatal("orDefault(nil)")
	}
	if orDefault("v", "d") != "v" {
		t.Fatal("orDefault(non-nil)")
	}
	if len(toRowSlice("not-array")) != 0 {
		t.Fatal("toRowSlice(non-array) should be empty")
	}
	// A non-object element is dropped.
	if got := toRowSlice([]any{map[string]any{"a": 1}, "skip"}); len(got) != 1 {
		t.Fatalf("toRowSlice mixed = %d, want 1", len(got))
	}
	// isRelativeTo: a sibling path is not contained.
	if isRelativeTo("/a/b", "/c") {
		t.Fatal("/a/b is not relative to /c")
	}
	// realpath resolves a nonexistent leaf to an absolute path.
	if p := realpath("relative/leaf"); !filepath.IsAbs(p) {
		t.Fatalf("realpath not absolute: %q", p)
	}
}

// --- core.go gaps ---

func TestWriteStateEdgeCases(t *testing.T) {
	m := testManager(t, nil)
	// Empty state file -> early return, no panic.
	m.StateFile = ""
	m.writeState(map[string]any{"a": 1})
	// A value json can't marshal -> the error path logs and returns.
	m.StateFile = filepath.Join(t.TempDir(), "state.json")
	m.writeState(map[string]any{"bad": make(chan int)})
	if _, err := os.Stat(m.StateFile); !os.IsNotExist(err) {
		t.Fatal("marshal error must not create a state file")
	}
}

func TestLoadStateRestoresAndNormalizes(t *testing.T) {
	m := testManager(t, nil)
	// Pre-seed an agent so restoreAgent hits the already-present early return.
	m.Agents["agent_keep"] = newAgentStatus("agent_keep")
	state := `{"desired_agents":3,"swarm_paused":true,"bust_respawn":true,"agents":{` +
		`"agent_keep":{"state":"running"},` +
		`"agent_run":{"state":"running"},` +
		`"agent_noid":{"state":"completed"}}}`
	if err := os.WriteFile(m.StateFile, []byte(state), 0o644); err != nil {
		t.Fatalf("write state: %v", err)
	}
	m.LoadState()
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.DesiredAgents != 3 || !m.SwarmPaused || !m.BustRespawn {
		t.Fatalf("scalars: desired=%d paused=%v bust=%v", m.DesiredAgents, m.SwarmPaused, m.BustRespawn)
	}
	// A restored "running" agent is normalized to stopped.
	if m.Agents["agent_run"].State != "stopped" {
		t.Fatalf("agent_run state = %q, want stopped", m.Agents["agent_run"].State)
	}
	// The pre-existing agent is untouched (still its default unknown state).
	if m.Agents["agent_keep"].State != "unknown" {
		t.Fatalf("agent_keep should be preserved, got %q", m.Agents["agent_keep"].State)
	}
}

func TestLoadStateMalformed(t *testing.T) {
	m := testManager(t, nil)
	if err := os.WriteFile(m.StateFile, []byte("{not json"), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	m.LoadState() // logs an error, does not panic
	// Missing file is also a clean no-op.
	m2 := testManager(t, nil)
	m2.StateFile = filepath.Join(t.TempDir(), "absent.json")
	m2.LoadState()
}

func TestPruneDeadKillsProcess(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	cfg := writeConfig(t, m, "agent.yaml")
	if _, err := m.SpawnAgent(context.Background(), cfg, "agent_001"); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	// A terminal agent that still owns a live process is killed then removed.
	m.mu.Lock()
	m.Agents["agent_001"].State = "stopped"
	m.mu.Unlock()
	res := m.PruneDead()
	if res["pruned"].(int) != 1 {
		t.Fatalf("pruned = %v", res["pruned"])
	}
	if m.hasProcess("agent_001") {
		t.Fatal("process should be killed on prune")
	}
}

func TestSetupAuthScopesWorkerToken(t *testing.T) {
	cfg := DefaultManagerConfig()
	cfg.EnforcePerAgentWorkerToken = true
	getenv := func(k string) string {
		switch k {
		case cfg.AuthTokenEnvVar:
			return "operator-tok"
		case "UTERM_MANAGER_WORKER_TOKEN":
			return "worker-tok"
		}
		return ""
	}
	mw, err := SetupAuth(&cfg, cfg.AuthTokenEnvVar, getenv)
	if err != nil || mw == nil {
		t.Fatalf("setup auth: mw=%v err=%v", mw, err)
	}
	if mw.workerToken == nil || *mw.workerToken != "worker-tok" {
		t.Fatalf("worker token not scoped: %v", mw.workerToken)
	}
	if !mw.enforcePerAgent {
		t.Fatal("enforcePerAgent should be set")
	}
}
