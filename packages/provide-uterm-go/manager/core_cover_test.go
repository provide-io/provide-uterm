//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"os"
	"path/filepath"
	"testing"
)

// TestNewAgentManagerDefaults covers the empty-TimeseriesDir, zero-interval, and
// nil-now default branches of NewAgentManager.
func TestNewAgentManagerDefaults(t *testing.T) {
	m := NewAgentManager(ManagerConfig{}, nil, nil)
	if m.Timeseries == nil {
		t.Fatal("Timeseries must be initialized with default dir/interval")
	}
	if m.now == nil {
		t.Fatal("now must default to a non-nil clock")
	}
	_ = m.now()
}

// TestLoadStateMissingFile covers the os.ReadFile error branch of LoadState.
func TestLoadStateMissingFile(t *testing.T) {
	m := NewAgentManager(ManagerConfig{
		StateFile: filepath.Join(t.TempDir(), "does-not-exist.json"),
	}, nil, nil)
	// Must not panic; the read error is logged and swallowed.
	m.LoadState()
}

// TestLoadStateDefaultsSavedState covers restoreAgent's savedState=="" default
// ("stopped") via a persisted agent record lacking a "state" field.
func TestLoadStateDefaultsSavedState(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	// One agent with no "state" key -> restoreAgent defaults it to "stopped".
	blob := `{"agents":{"a1":{"agent_id":"a1"}}}`
	if err := os.WriteFile(path, []byte(blob), 0o644); err != nil {
		t.Fatal(err)
	}
	m := NewAgentManager(ManagerConfig{StateFile: path}, nil, nil)
	m.LoadState()
}
