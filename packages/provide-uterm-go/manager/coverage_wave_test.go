//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestSwarmStatusStripsTelemetryFields exercises the real strip path now that
// SwarmStatus walks the []any shape produced by toMap().
func TestSwarmStatusStripsTelemetryFields(t *testing.T) {
	m := testManager(t, nil)
	tools := NewManagerTools(m)
	tools.AgentTelemetryFields = map[string]struct{}{"error_message": {}, "error_type": {}}
	a := newAgentStatus("agent_001")
	a.ErrorMessage = strPtr("boom")
	a.ErrorType = strPtr("RuntimeError")
	a.State = "running"
	m.Agents["agent_001"] = a

	data := tools.SwarmStatus(false)
	agents, ok := data["agents"].([]any)
	if !ok || len(agents) != 1 {
		t.Fatalf("agents = %#v", data["agents"])
	}
	row, ok := agents[0].(map[string]any)
	if !ok {
		t.Fatalf("row type %T", agents[0])
	}
	if _, has := row["error_message"]; has {
		t.Fatalf("error_message should be stripped: %#v", row)
	}
	if _, has := row["error_type"]; has {
		t.Fatalf("error_type should be stripped: %#v", row)
	}
	if row["agent_id"] != "agent_001" {
		t.Fatalf("agent_id lost: %#v", row)
	}

	// includeTelemetry keeps fields.
	kept := tools.SwarmStatus(true)
	kAgents := kept["agents"].([]any)
	kRow := kAgents[0].(map[string]any)
	if kRow["error_message"] != "boom" {
		t.Fatalf("includeTelemetry should keep error_message: %#v", kRow)
	}
}

// TestSwarmStatusTypedAgentsBranch covers the []*AgentStatus arm + non-map []any
// items via the extracted stripAgentsTelemetry helper.
func TestSwarmStatusTypedAgentsBranch(t *testing.T) {
	fields := map[string]struct{}{"error_message": {}}
	a := newAgentStatus("agent_t")
	a.ErrorMessage = strPtr("x")
	out := map[string]any{"agents": []*AgentStatus{a}}
	stripAgentsTelemetry(out, fields)
	rows, ok := out["agents"].([]map[string]any)
	if !ok || len(rows) != 1 {
		t.Fatalf("typed strip = %#v", out["agents"])
	}
	if _, has := rows[0]["error_message"]; has {
		t.Fatal("typed branch should strip error_message")
	}
	// Non-map items in []any are preserved.
	out2 := map[string]any{
		"agents": []any{"skip", map[string]any{"error_message": "z", "agent_id": "a"}},
	}
	stripAgentsTelemetry(out2, fields)
	arr := out2["agents"].([]any)
	if arr[0] != "skip" {
		t.Fatalf("non-map preserved: %#v", arr[0])
	}
	if _, has := arr[1].(map[string]any)["error_message"]; has {
		t.Fatal("map item should strip")
	}
}

// TestTimeseriesCleanupPreservesLiveAndFresh removes only aged archives.
func TestTimeseriesCleanupPreservesLiveAndFresh(t *testing.T) {
	dir := t.TempDir()
	now := 1_000_000.0
	tm := NewTimeseriesManager(fixedStatus, dir, 1, nil, func() float64 { return now })
	// Materialize the live file so the skip-current-path arm is meaningful.
	tm.WriteSample(fixedStatus(), "seed")

	old := filepath.Join(dir, "swarm_timeseries_ancient.jsonl")
	if err := os.WriteFile(old, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	oldTime := time.Unix(int64(now-TimeseriesRetentionS-60), 0)
	if err := os.Chtimes(old, oldTime, oldTime); err != nil {
		t.Fatal(err)
	}
	fresh := filepath.Join(dir, "swarm_timeseries_fresh.jsonl")
	if err := os.WriteFile(fresh, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	freshTime := time.Unix(int64(now-10), 0)
	_ = os.Chtimes(fresh, freshTime, freshTime)

	tm.cleanupOld(TimeseriesRetentionS)

	if _, err := os.Stat(old); !os.IsNotExist(err) {
		t.Fatalf("old archive should be removed, err=%v", err)
	}
	if _, err := os.Stat(fresh); err != nil {
		t.Fatalf("fresh archive should remain: %v", err)
	}
	if _, err := os.Stat(tm.Path); err != nil {
		t.Fatalf("live path should remain: %v", err)
	}
}

// TestTimeseriesWriteSampleErrors covers ensureFh, marshal, and write failures.
func TestTimeseriesWriteSampleErrors(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	tm.Path = filepath.Join(t.TempDir(), "missing", "nested", "x.jsonl")
	tm.fh = nil
	tm.WriteSample(fixedStatus(), "fail") // ensureFh error

	// Marshal failure via plugin that injects a non-JSON value.
	tm2 := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, badTSPlugin{}, func() float64 { return 5 })
	tm2.WriteSample(fixedStatus(), "bad")

}

type badTSPlugin struct{}

func (badTSPlugin) BuildRow(*SwarmStatus, string) map[string]any {
	return map[string]any{"ch": make(chan int)}
}
func (badTSPlugin) GetSummary(*TimeseriesManager, int) map[string]any {
	return map[string]any{}
}

// TestTimeseriesDefaults covers interval floor + nil-now + empty-dir defaults.
func TestTimeseriesDefaults(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 0, nil, nil)
	if tm.IntervalS < 1 {
		t.Fatalf("interval floor = %d", tm.IntervalS)
	}
	if tm.now == nil {
		t.Fatal("now default nil")
	}
	_ = tm.now()

	// Empty timeseriesDir defaults to logs/metrics under cwd; clean up after.
	cwd, _ := os.Getwd()
	defaultDir := filepath.Join(cwd, "logs", "metrics")
	tm2 := NewTimeseriesManager(fixedStatus, "", 1, nil, func() float64 { return 1 })
	if tm2.Dir != "logs/metrics" && tm2.Dir != defaultDir && tm2.Dir != filepath.Clean("logs/metrics") {
		// Dir is stored as the default string "logs/metrics".
		if tm2.Dir != "logs/metrics" {
			t.Fatalf("default dir = %q", tm2.Dir)
		}
	}
	_ = os.RemoveAll(filepath.Join(cwd, "logs"))
}

// TestTimeseriesLoopIntervalTick waits for at least one interval sample.
func TestTimeseriesLoopIntervalTick(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { tm.Loop(ctx); close(done) }()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		tm.mu.Lock()
		n := tm.SamplesCount
		tm.mu.Unlock()
		if n >= 2 {
			cancel()
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Fatal("loop did not stop")
			}
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	cancel()
	<-done
	t.Fatalf("SamplesCount never reached 2 (got %d)", tm.SamplesCount)
}

// TestWriteStateUnwritable covers WriteFile failure on a read-only directory
// and the json.Marshal failure arm.
func TestWriteStateUnwritable(t *testing.T) {
	m := testManager(t, nil)
	ro := filepath.Join(t.TempDir(), "ro")
	if err := os.Mkdir(ro, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(ro, 0o755) })
	m.StateFile = filepath.Join(ro, "state.json")
	m.writeState(map[string]any{"desired_agents": 1})

	// Marshal failure (unsupported type).
	m.StateFile = filepath.Join(t.TempDir(), "state.json")
	m.writeState(map[string]any{"bad": make(chan int)})

	m.StateFile = ""
	m.writeState(map[string]any{"x": 1})
}

// TestCommandHistoryNilInit covers commandHistoryRows initializing a nil slice.
func TestCommandHistoryNilInit(t *testing.T) {
	a := &AgentStatus{AgentID: "a"}
	rows := commandHistoryRows(a)
	if rows == nil || a.ManagerCommandHistory == nil {
		t.Fatal("expected non-nil history")
	}
	updateCommandHistory(a, 99, map[string]any{"status": "x"})
	updateCommandHistory(a, 0, map[string]any{"status": "x"})
}

// TestWebhookGateTimeoutDefault covers timeoutS <= 0 defaulting to 2s.
func TestWebhookGateTimeoutDefault(t *testing.T) {
	g := NewWebhookAgentSpawnPolicyGate("http://example.invalid", "", 0)
	if g.Timeout != 2*time.Second {
		t.Fatalf("timeout = %v, want 2s", g.Timeout)
	}
	// Do failure path (connection refused / DNS).
	if g.InterceptSpawn(context.Background(), "a1", "c.yaml", nil) {
		t.Fatal("unreachable webhook should deny")
	}
	// NewRequest failure on a malformed URL.
	g2 := &WebhookAgentSpawnPolicyGate{URL: "://not-a-url", Timeout: time.Second}
	if g2.InterceptSpawn(context.Background(), "a1", "c.yaml", map[string]any{}) {
		t.Fatal("malformed URL should deny")
	}
}

// TestTimeseriesReadTailEdges covers missing file, corrupt lines, and limit floor.
func TestTimeseriesReadTailEdges(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	// Missing file → empty.
	if rows := tm.ReadTail(5); len(rows) != 0 {
		t.Fatalf("missing = %v", rows)
	}
	// Corrupt + valid lines (limit large enough that both are scanned).
	if err := os.WriteFile(tm.Path, []byte("not-json\n{\"reason\":\"ok\",\"total_agents\":1}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	rows := tm.ReadTail(10)
	if len(rows) != 1 || rows[0]["reason"] != "ok" {
		t.Fatalf("rows = %#v", rows)
	}
	// limit < 1 floors to 1 (may return empty when the trailing split segment
	// is blank — still exercises the floor assignment).
	_ = tm.ReadTail(0)
	// rotateIfNeeded with missing path is a silent no-op.
	tm.Path = filepath.Join(t.TempDir(), "gone.jsonl")
	tm.rotateIfNeeded()
}
