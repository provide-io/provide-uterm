//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeEntry is a WorkerRegistryEntry for tests.
type fakeEntry struct {
	module    string
	envHook   func(env map[string]string, a *AgentStatus, m *AgentManager, raw map[string]any)
	wtypeName string
}

func (f fakeEntry) WorkerType() string   { return f.wtypeName }
func (f fakeEntry) WorkerModule() string { return f.module }
func (f fakeEntry) ConfigureWorkerEnv(env map[string]string, a *AgentStatus, m *AgentManager, raw map[string]any) {
	if f.envHook != nil {
		f.envHook(env, a, m, raw)
	}
}

// testManager builds a manager wired with a temp state/log/timeseries dir and a
// default worker registry. SpawnCommand launches a short-lived child.
func testManager(t *testing.T, spawnCmd SpawnCommandFunc) *AgentManager {
	t.Helper()
	dir := t.TempDir()
	cfg := DefaultManagerConfig()
	cfg.LogDir = filepath.Join(dir, "workers")
	cfg.TimeseriesDir = filepath.Join(dir, "metrics")
	cfg.StateFile = filepath.Join(dir, "state.json")
	cfg.SpawnConfigDir = dir
	m := NewAgentManager(cfg, nil, nil)
	registry := map[string]WorkerRegistryEntry{"default": fakeEntry{module: "worker", wtypeName: "default"}}
	pm := NewAgentProcessManager(m, registry, cfg.LogDir)
	if spawnCmd != nil {
		pm.SpawnCommand = spawnCmd
	}
	pm.getenv = func(string) string { return "" }
	m.PM = pm
	return m
}

// writeConfig writes a minimal .yaml (JSON content) config file in the sandbox.
func writeConfig(t *testing.T, m *AgentManager, name string) string {
	t.Helper()
	p := filepath.Join(m.Config.SpawnConfigDir, name)
	if err := os.WriteFile(p, []byte(`{"worker_type": "default"}`), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	return p
}

// sleepCmd returns a spawn command that runs `sleep <secs>`.
func sleepCmd(secs string) SpawnCommandFunc {
	return func(_, _, _ string) []string { return []string{"sleep", secs} }
}

// scriptCmd returns a spawn command that runs `sh -c <script>`.
func scriptCmd(script string) SpawnCommandFunc {
	return func(_, _, _ string) []string { return []string{"sh", "-c", script} }
}

// waitFor polls cond until true or the deadline, failing otherwise.
func waitFor(t *testing.T, msg string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", msg)
}

// agentState returns the current state of agentID (or "" if absent).
func (m *AgentManager) agentState(agentID string) string {
	m.mu.Lock()
	defer m.mu.Unlock()
	if a, ok := m.Agents[agentID]; ok {
		return a.State
	}
	return ""
}

// hasProcess reports whether agentID has a live process handle.
func (m *AgentManager) hasProcess(agentID string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	_, ok := m.Processes[agentID]
	return ok
}

// doJSON drives a request through the mux and decodes the JSON response.
func doJSON(t *testing.T, h http.Handler, method, target string, body string) (int, map[string]any) {
	t.Helper()
	var req *http.Request
	if body != "" {
		req = httptest.NewRequest(method, target, strings.NewReader(body))
	} else {
		req = httptest.NewRequest(method, target, nil)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var m map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &m)
	}
	return rec.Code, m
}
