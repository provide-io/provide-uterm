//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"os"
	"testing"
)

// TestReadTailBytesEmptyFile covers the end<=0 early return: an existing but
// zero-length timeseries file.
func TestReadTailBytesEmptyFile(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	if err := os.WriteFile(tm.Path, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	buf, err := tm.readTailBytes(10)
	if err != nil {
		t.Fatalf("empty-file readTailBytes: %v", err)
	}
	if len(buf) != 0 {
		t.Fatalf("expected empty buffer, got %d bytes", len(buf))
	}
	if rows := tm.ReadTail(5); len(rows) != 0 {
		t.Fatalf("empty file must yield no rows, got %d", len(rows))
	}
}

// TestTrimToLatestEpochScaledThreshold covers the ratio-scaled drop-threshold
// branch: prevTurns is large enough that prevTurns*ratio exceeds the min.
func TestTrimToLatestEpochScaledThreshold(t *testing.T) {
	rows := []map[string]any{
		{"total_turns": 1000.0, "total_agents": 5.0}, // scaled threshold = 200
		{"total_turns": 700.0, "total_agents": 5.0},  // drop 300 > 200 -> epoch reset
	}
	trimmed := trimToLatestEpoch(rows)
	if len(trimmed) != 1 {
		t.Fatalf("scaled-threshold reset should trim to 1 row, got %d", len(trimmed))
	}
}
