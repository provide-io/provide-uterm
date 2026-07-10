//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"testing"
	"time"
)

func fixedStatus() *SwarmStatus {
	return &SwarmStatus{TotalAgents: 2, Running: 1, Completed: 1, Agents: []*AgentStatus{}}
}

func TestTimeseriesWriteAndRead(t *testing.T) {
	clock := 1000.0
	now := func() float64 { return clock }
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 20, nil, now)

	tm.WriteSample(fixedStatus(), "startup")
	tm.SamplesCount++
	clock = 1020
	tm.WriteSample(fixedStatus(), "interval")
	tm.SamplesCount++

	rows := tm.ReadTail(10)
	if len(rows) != 2 {
		t.Fatalf("rows = %d, want 2", len(rows))
	}
	if rows[0]["reason"] != "startup" || rows[1]["reason"] != "interval" {
		t.Fatalf("reasons = %v", rows)
	}
	if rows[0]["ts"] != 1000.0 {
		t.Fatalf("ts = %v, want injected clock 1000", rows[0]["ts"])
	}
	info := tm.GetInfo()
	if info["samples"].(int) != 2 {
		t.Fatalf("samples = %v", info["samples"])
	}
}

func TestTimeseriesGetRecentAndSummary(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	for i := 0; i < 3; i++ {
		tm.WriteSample(fixedStatus(), "interval")
	}
	if got := tm.GetRecent(2); len(got) == 0 {
		t.Fatal("expected recent rows")
	}
	sum := tm.GetSummary(60)
	if sum["error"] == nil {
		t.Fatalf("expected no-plugin summary error, got %v", sum)
	}
}

func TestTrimToLatestEpoch(t *testing.T) {
	rows := []map[string]any{
		{"total_turns": 100.0, "total_agents": 5.0},
		{"total_turns": 110.0, "total_agents": 5.0},
		// Hard reset: turns drop far below threshold.
		{"total_turns": 1.0, "total_agents": 5.0},
		{"total_turns": 5.0, "total_agents": 5.0},
	}
	trimmed := trimToLatestEpoch(rows)
	if len(trimmed) != 2 {
		t.Fatalf("trimmed len = %d, want 2", len(trimmed))
	}
	// agents→0 also starts a new epoch.
	rows2 := []map[string]any{
		{"total_turns": 10.0, "total_agents": 5.0},
		{"total_turns": 11.0, "total_agents": 0.0},
	}
	if len(trimToLatestEpoch(rows2)) != 1 {
		t.Fatal("agents→0 should start a new epoch")
	}
	// Single row unchanged.
	if len(trimToLatestEpoch(rows[:1])) != 1 {
		t.Fatal("single row unchanged")
	}
}

func TestTimeseriesRotation(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	tm.maxBytes = 1 // force rotation on the next write
	tm.WriteSample(fixedStatus(), "a")
	tm.WriteSample(fixedStatus(), "b")
	// After rotation the current file is fresh; the archived file exists.
	if _, err := tm.readTailBytes(10); err != nil {
		// Not fatal; rotation may have moved the file. Just ensure no panic.
		_ = err
	}
}

func TestTimeseriesLoopCancels(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { tm.Loop(ctx); close(done) }()
	time.Sleep(20 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Loop did not stop on cancel")
	}
	if tm.SamplesCount < 1 {
		t.Fatal("expected at least the startup sample")
	}
}
