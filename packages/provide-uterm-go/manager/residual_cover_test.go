//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"bytes"
	"context"
	"errors"
	"io"
	"io/fs"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

var errInjected = errors.New("injected failure")

// fakeProc is a scripted processHandle.
type fakeProc struct {
	mu       sync.Mutex
	pid      int
	code     int
	exited   bool
	waitErrs []error // consumed in order; nil once exhausted
	signals  []syscall.Signal
}

func (p *fakeProc) PID() int { return p.pid }

func (p *fakeProc) Poll() (int, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.code, p.exited
}

func (p *fakeProc) WaitExit(time.Duration) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.waitErrs) == 0 {
		return nil
	}
	err := p.waitErrs[0]
	p.waitErrs = p.waitErrs[1:]
	return err
}

func (p *fakeProc) SignalGroup(sig syscall.Signal) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.signals = append(p.signals, sig)
	return nil
}

func (p *fakeProc) sent() []syscall.Signal {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]syscall.Signal(nil), p.signals...)
}

func clearManagerEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{"UTERM_CORS_ORIGINS", "UTERM_MANAGER_TOKEN", "UTERM_MANAGER_ALLOW_UNAUTHENTICATED", ConfigDirEnvVar} {
		t.Setenv(k, "")
	}
}

// With no Getenv injected the app reads the real environment.
func TestCreateManagerAppDefaultsToProcessEnvironment(t *testing.T) {
	clearManagerEnv(t)
	t.Setenv("UTERM_CORS_ORIGINS", "*")
	cfg := DefaultManagerConfig()
	cfg.Host = "127.0.0.1"
	cfg.CORSOrigins = []string{"https://ok.example"}
	cfg.LogDir = t.TempDir()
	cfg.TimeseriesDir = t.TempDir()
	cfg.StateFile = ""
	if _, _, err := CreateManagerApp(cfg, AppOptions{}); err == nil || !strings.Contains(err.Error(), "wildcard") {
		t.Fatalf("CreateManagerApp = %v; want the env's wildcard origin refused", err)
	}
}

// A loopback bind with no token and no injected getenv skips auth.
func TestSetupAuthNilGetenvOnLoopback(t *testing.T) {
	mw, err := SetupAuth(&ManagerConfig{Host: "127.0.0.1"}, "UTERM_MANAGER_TOKEN", nil)
	if mw != nil || err != nil {
		t.Fatalf("SetupAuth = %v, %v; want auth skipped", mw, err)
	}
}

func TestWriteStateFailuresLeaveNoTempFile(t *testing.T) {
	t.Run("write", func(t *testing.T) {
		m := testManager(t, nil)
		m.StateFile = filepath.Join(t.TempDir(), "missing-dir", "state.json")
		m.writeState(map[string]any{"a": 1})
		if _, err := os.Stat(m.StateFile + ".tmp"); !errors.Is(err, fs.ErrNotExist) {
			t.Fatalf("temp file left behind: %v", err)
		}
	})
	t.Run("rename", func(t *testing.T) {
		m := testManager(t, nil)
		m.StateFile = filepath.Join(t.TempDir(), "state.json")
		// A non-empty directory where the state file goes: the rename fails.
		if err := os.MkdirAll(filepath.Join(m.StateFile, "occupied"), 0o755); err != nil {
			t.Fatal(err)
		}
		m.writeState(map[string]any{"a": 1})
		if _, err := os.Stat(m.StateFile + ".tmp"); !errors.Is(err, fs.ErrNotExist) {
			t.Fatalf("temp file left behind: %v", err)
		}
		if info, err := os.Stat(m.StateFile); err != nil || !info.IsDir() {
			t.Fatal("the existing path must be left untouched")
		}
	})
}

func TestLoadStateUnreadableStateFile(t *testing.T) {
	m := testManager(t, nil)
	m.StateFile = t.TempDir() // a directory: stats fine, cannot be read
	m.DesiredAgents = 3
	m.LoadState()
	if m.DesiredAgents != 3 || len(m.Agents) != 0 {
		t.Fatal("an unreadable state file must change nothing")
	}
}

// The save loop falls back to the default interval and every loop stops on
// cancellation.
func TestStartBackgroundDefaultIntervalStopsOnCancel(t *testing.T) {
	m := testManager(t, nil)
	m.Config.SaveIntervalS = 0
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	var wg sync.WaitGroup
	m.StartBackground(ctx, &wg)
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("background loops did not stop")
	}
}

func TestSpawnPolicyGateRejectsUnreadableAndInvalidBodies(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	// A 200 that promises more body than it sends: the read fails.
	go func() {
		for {
			c, aErr := ln.Accept()
			if aErr != nil {
				return
			}
			buf := make([]byte, 4096)
			_, _ = c.Read(buf)
			_, _ = c.Write([]byte("HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"))
			_ = c.Close()
		}
	}()
	defer func() { _ = ln.Close() }()
	short := NewWebhookAgentSpawnPolicyGate("http://"+ln.Addr().String(), "", 5)
	if short.InterceptSpawn(context.Background(), "a", "c.yaml", nil) {
		t.Fatal("a truncated response must not allow the spawn")
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("not json"))
	}))
	defer srv.Close()
	invalid := NewWebhookAgentSpawnPolicyGate(srv.URL, "", 5)
	if invalid.InterceptSpawn(context.Background(), "a", "c.yaml", nil) {
		t.Fatal("a non-JSON response must not allow the spawn")
	}
}

func TestSwarmSpawnBatchEstimatesGroupDelay(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	defer m.KillAll()
	defer m.PM.CancelSpawn()
	cfgs := []string{writeConfig(t, m, "a.yaml"), writeConfig(t, m, "b.yaml"), writeConfig(t, m, "c.yaml")}
	got := NewManagerTools(m).SwarmSpawnBatch(cfgs, 1, 3600, "random", "")
	if got["total_groups"] != 3 {
		t.Fatalf("batch = %v", got)
	}
	if est, _ := got["estimated_time_seconds"].(float64); est != 7200 {
		t.Fatalf("estimate = %v, want 2 gaps of 3600s", got)
	}
}

func TestSpawnSwarmClampsGroupAndStopsOnCancel(t *testing.T) {
	m := testManager(t, sleepCmd("30"))
	defer m.KillAll()
	bad := filepath.Join(m.Config.SpawnConfigDir, "bad.yaml")
	if err := os.WriteFile(bad, []byte(`{"worker_type": "nope"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	good := writeConfig(t, m, "good.yaml")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	// group size 0 clamps to 1: the bad config fails alone, then the
	// inter-group wait sees the cancellation before the good one spawns.
	ids, err := m.PM.SpawnSwarm(ctx, []string{bad, good}, 0, 3600, "", "")
	if !errors.Is(err, context.Canceled) || len(ids) != 0 {
		t.Fatalf("SpawnSwarm = %v, %v; want nothing started and the cancellation", ids, err)
	}
}

func TestSetAgentExitStateCleanExitAfterReportedError(t *testing.T) {
	a := newAgentStatus("agent_001")
	a.ErrorMessage = strPtr("boom")
	setAgentExitState(a, 0)
	if a.State != "error" || a.ExitReason == nil || *a.ExitReason != "reported_error_then_exit_0" {
		t.Fatalf("state %q reason %v", a.State, a.ExitReason)
	}
	b := newAgentStatus("agent_002")
	b.State = "error"
	b.ExitReason = strPtr("kept")
	setAgentExitState(b, 0)
	if *b.ExitReason != "kept" {
		t.Fatalf("an existing exit reason must be kept, got %q", *b.ExitReason)
	}
}

func TestHandleExitedProcessesForUnknownAgent(t *testing.T) {
	m := testManager(t, nil)
	m.Processes["agent_009"] = &fakeProc{pid: 1, exited: true}
	m.PM.handleExitedProcesses()
	if m.hasProcess("agent_009") {
		t.Fatal("an exited process with no agent must be dropped")
	}
}

func TestBustRespawnOnlyStopsRunningAgents(t *testing.T) {
	m := testManager(t, nil)
	m.BustRespawn = true
	stopped := newAgentStatus("agent_001")
	stopped.State = "stopped"
	stopped.Extra = map[string]any{"activity_context": "BUST"}
	m.Agents["agent_001"] = stopped
	m.PM.handleBustRespawn()
	if stopped.ExitReason != nil {
		t.Fatalf("a non-running agent was bust-killed: %v", *stopped.ExitReason)
	}
}

// The default log directory is relative to the working directory.
func TestDefaultWorkerLogDir(t *testing.T) {
	t.Chdir(t.TempDir())
	m := testManager(t, nil)
	m.PM.logDir = ""
	if got := m.PM.cleanupOldWorkerLogs(); got != 0 {
		t.Fatalf("cleanup with no log dir = %d", got)
	}
	proc, err := m.PM.spawnProcess("agent_001", []string{"true"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	_ = proc.WaitExit(5 * time.Second)
	if _, err := os.Stat(filepath.Join("logs", "workers", "agent_001.log")); err != nil {
		t.Fatalf("log not created under the default dir: %v", err)
	}
}

type failInfoEntry struct{ fs.DirEntry }

func (failInfoEntry) Info() (fs.FileInfo, error) { return nil, errInjected }

func TestCleanupOldWorkerLogsListingFailures(t *testing.T) {
	m := testManager(t, nil)
	if err := os.MkdirAll(m.PM.logDir, 0o755); err != nil {
		t.Fatal(err)
	}
	orig := readDir
	t.Cleanup(func() { readDir = orig })

	readDir = func(string) ([]os.DirEntry, error) { return nil, errInjected }
	if got := m.PM.cleanupOldWorkerLogs(); got != 0 {
		t.Fatalf("cleanup with an unreadable dir = %d", got)
	}

	stale := filepath.Join(m.PM.logDir, "agent_404.log")
	if err := os.WriteFile(stale, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-48 * time.Hour)
	_ = os.Chtimes(stale, old, old)
	readDir = func(dir string) ([]os.DirEntry, error) {
		entries, err := os.ReadDir(dir)
		out := make([]os.DirEntry, 0, len(entries))
		for _, e := range entries {
			out = append(out, failInfoEntry{e})
		}
		return out, err
	}
	if got := m.PM.cleanupOldWorkerLogs(); got != 0 {
		t.Fatalf("an entry that cannot be stat'ed must be skipped, deleted %d", got)
	}
	if _, err := os.Stat(stale); err != nil {
		t.Fatal("the unstat'able entry must not be removed")
	}
}

func TestDesiredStatePrunesDeadAgentsWithProcesses(t *testing.T) {
	m := testManager(t, nil)
	m.DesiredAgents = 1
	dead := newAgentStatus("agent_001")
	dead.State = "error"
	m.Agents["agent_001"] = dead
	proc := &fakeProc{pid: 4242}
	m.Processes["agent_001"] = proc
	running := newAgentStatus("agent_002")
	running.State = "running"
	m.Agents["agent_002"] = running
	m.PM.handleDesiredState()
	if _, ok := m.Agents["agent_001"]; ok || m.hasProcess("agent_001") {
		t.Fatal("the dead agent and its process must be pruned")
	}
	if got := proc.sent(); len(got) == 0 || got[0] != syscall.SIGTERM {
		t.Fatalf("pruned process signals = %v, want a SIGTERM", got)
	}
}

func TestParseAgentIndexRejectsOverflow(t *testing.T) {
	if _, ok := parseAgentIndex("agent_99999999999999999999999"); ok {
		t.Fatal("an index that overflows int must be rejected")
	}
}

func TestSyncNextAgentIndexAdvancesPastKnownIDs(t *testing.T) {
	m := testManager(t, nil)
	m.Agents["agent_007"] = newAgentStatus("agent_007")
	if got := m.PM.SyncNextAgentIndex(); got != 8 {
		t.Fatalf("next index = %d, want 8", got)
	}
	if got := m.PM.AllocateAgentID(); got != "agent_008" {
		t.Fatalf("allocated %q", got)
	}
}

func TestBuildWorkerEnvCarriesNameBase(t *testing.T) {
	m := testManager(t, nil)
	m.PM.spawnNameBase = "crew"
	env := m.PM.buildWorkerEnv("UTERM_", nil, nil, nil, "agent_001")
	if env["UTERM_NAME_BASE"] != "crew" {
		t.Fatalf("env = %v", env)
	}
}

func TestBuildWorkerEnvSkipsMalformedEntriesAndDefaultsWorkerVar(t *testing.T) {
	m := testManager(t, nil)
	m.Config.AuthWorkerTokenEnvVar = ""
	m.PM.environ = func() []string { return []string{"MALFORMED", "UTERM_MANAGER_WORKER_TOKEN=leak", "UTERM_X=1"} }
	m.PM.getenv = func(k string) string {
		if k == "UTERM_MANAGER_WORKER_TOKEN" {
			return "worker-secret"
		}
		return ""
	}
	env := m.PM.buildWorkerEnv("UTERM_", nil, nil, nil, "agent_001")
	if _, leaked := env["UTERM_MANAGER_WORKER_TOKEN"]; leaked {
		t.Fatal("the worker token variable must not reach the worker")
	}
	if env["UTERM_X"] != "1" || env[m.Config.AuthTokenEnvVar] == "" {
		t.Fatalf("env = %v", env)
	}
	if _, ok := env["MALFORMED"]; ok {
		t.Fatal("an entry without '=' must be skipped")
	}
}

func TestGetRegistryEntrySingleEntryServesDefault(t *testing.T) {
	m := testManager(t, nil)
	only := fakeEntry{module: "w", wtypeName: "special"}
	m.PM.workerRegistry = map[string]WorkerRegistryEntry{"special": only}
	got, err := m.PM.getRegistryEntry("default", "c.yaml")
	if err != nil || got == nil || got.WorkerType() != "special" {
		t.Fatalf("getRegistryEntry = %v, %v; want the single entry", got, err)
	}
}

func TestLoadWorkerTypeDefaultsEmptyJSONType(t *testing.T) {
	m := testManager(t, nil)
	p := filepath.Join(t.TempDir(), "c.yaml")
	if err := os.WriteFile(p, []byte(`{"worker_type": ""}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if wt, _ := m.PM.loadWorkerType(p); wt != "default" {
		t.Fatalf("worker type = %q", wt)
	}
}

func TestSpawnProcessLogFailures(t *testing.T) {
	t.Run("log dir cannot be created", func(t *testing.T) {
		m := testManager(t, nil)
		blocker := filepath.Join(t.TempDir(), "file")
		if err := os.WriteFile(blocker, nil, 0o644); err != nil {
			t.Fatal(err)
		}
		m.PM.logDir = filepath.Join(blocker, "logs")
		if _, err := m.PM.spawnProcess("agent_001", []string{"true"}, nil); err == nil {
			t.Fatal("expected a log dir failure")
		}
	})
	t.Run("log file cannot be opened", func(t *testing.T) {
		m := testManager(t, nil)
		if err := os.MkdirAll(filepath.Join(m.PM.logDir, "agent_001.log"), 0o755); err != nil {
			t.Fatal(err)
		}
		if _, err := m.PM.spawnProcess("agent_001", []string{"true"}, nil); err == nil {
			t.Fatal("expected a log open failure")
		}
	})
	t.Run("oversized log is rotated", func(t *testing.T) {
		m := testManager(t, nil)
		if err := os.MkdirAll(m.PM.logDir, 0o755); err != nil {
			t.Fatal(err)
		}
		logPath := filepath.Join(m.PM.logDir, "agent_001.log")
		f, err := os.Create(logPath)
		if err != nil {
			t.Fatal(err)
		}
		_ = f.Truncate(WorkerLogMaxBytes + 1)
		_ = f.Close()
		proc, err := m.PM.spawnProcess("agent_001", []string{"true"}, nil)
		if err != nil {
			t.Fatal(err)
		}
		_ = proc.WaitExit(5 * time.Second)
		info, err := os.Stat(logPath + ".prev")
		if err != nil || info.Size() != WorkerLogMaxBytes+1 {
			t.Fatalf("rotated log = %v, %v", info, err)
		}
	})
}

func TestStopProcessTreeEscalatesToKill(t *testing.T) {
	m := testManager(t, nil)
	proc := &fakeProc{pid: 4242, waitErrs: []error{errWaitTimeout}}
	m.PM.stopProcessTree("agent_001", proc, 0, time.Millisecond)
	if got := proc.sent(); len(got) != 2 || got[0] != syscall.SIGTERM || got[1] != syscall.SIGKILL {
		t.Fatalf("signals = %v, want TERM then KILL", got)
	}
}

// KillAgent with no process handle falls back to the recorded pid and kills
// that process group outright.
func TestKillAgentFallsBackToRecordedPID(t *testing.T) {
	m := testManager(t, nil)
	cmd := exec.Command("sleep", "30")
	cmd.SysProcAttr = newSysProcAttr()
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	a := newAgentStatus("agent_001")
	a.State = "running"
	a.PID = intPtr(cmd.Process.Pid)
	m.Agents["agent_001"] = a
	m.PM.KillAgent("agent_001")
	select {
	case <-exited:
	case <-time.After(5 * time.Second):
		_ = cmd.Process.Kill()
		t.Fatal("the recorded process group was not killed")
	}
	if m.agentState("agent_001") != "stopped" {
		t.Fatalf("state = %q", m.agentState("agent_001"))
	}
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, errInjected }

// A child whose output cannot be copied ends with Wait reporting the copy
// error rather than an exit status; the reaper records that as -1.
func TestReapRecordsNonExitErrorAsFailure(t *testing.T) {
	cmd := exec.Command("sh", "-c", "echo out")
	cmd.Stdout = failingWriter{}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	p := newManagedProcess(cmd)
	if err := p.WaitExit(5 * time.Second); err != nil {
		t.Fatal(err)
	}
	if code, done := p.Poll(); !done || code != -1 {
		t.Fatalf("Poll = %d, %v; want -1, exited", code, done)
	}
}

func TestListAgentsBreaksTimeTiesByID(t *testing.T) {
	m := testManager(t, nil)
	for _, id := range []string{"agent_001", "agent_002"} {
		a := newAgentStatus(id)
		a.LastUpdateTime = 50
		m.Agents[id] = a
	}
	s := &Server{M: m, getenv: func(string) string { return "" }}
	code, body := doJSON(t, s.Routes(), http.MethodGet, "/agents", "")
	rows, _ := body["agents"].([]any)
	if code != http.StatusOK || len(rows) != 2 || rows[0].(map[string]any)["agent_id"] != "agent_002" {
		t.Fatalf("agents = %d %v", code, body)
	}
}

func TestUpdateCommandHistorySkipsOtherSeqs(t *testing.T) {
	a := newAgentStatus("agent_001")
	a.ManagerCommandHistory = []map[string]any{{"seq": 1, "status": "queued"}, {"seq": 2, "status": "queued"}}
	updateCommandHistory(a, 1, map[string]any{"status": "done"})
	if a.ManagerCommandHistory[0]["status"] != "done" || a.ManagerCommandHistory[1]["status"] != "queued" {
		t.Fatalf("history = %v", a.ManagerCommandHistory)
	}
}

// With the working directory gone, a relative config path cannot be made
// absolute; it must be refused, never resolved against a guess.
func TestValidateConfigPathWithoutWorkingDirectory(t *testing.T) {
	base := t.TempDir()
	gone := filepath.Join(t.TempDir(), "gone")
	if err := os.Mkdir(gone, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(gone)
	if err := os.Remove(gone); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Getwd(); err == nil {
		t.Skip("this platform still reports a working directory after it is removed")
	}
	if _, rejected := validateConfigPath("agent.yaml", base, func(string) string { return "" }); rejected == nil {
		t.Fatal("a path that cannot be resolved must be refused")
	}
}

func TestUpdateStatusRefusesNewAgentPastTheCap(t *testing.T) {
	m := testManager(t, nil)
	m.MaxAgents = 1
	m.Agents["agent_001"] = newAgentStatus("agent_001")
	s := &Server{M: m, getenv: func(string) string { return "" }}
	code, body := doJSON(t, s.Routes(), http.MethodPost, "/agent/agent_002/status", `{"state":"running"}`)
	if code != http.StatusTooManyRequests || !strings.Contains(strOf(body["error"]), "Max agents") {
		t.Fatalf("status update = %d %v", code, body)
	}
	if _, ok := m.Agents["agent_002"]; ok {
		t.Fatal("no agent may be created past the cap")
	}
}

// tailFile is a scripted ReadSeekCloser for the tail reader.
type tailFile struct {
	*bytes.Reader
	seekErrAt int // fail the Nth Seek (1-based); 0 never
	seeks     int
	readErr   bool
}

func (f *tailFile) Seek(off int64, whence int) (int64, error) {
	f.seeks++
	if f.seeks == f.seekErrAt {
		return 0, errInjected
	}
	return f.Reader.Seek(off, whence)
}

func (f *tailFile) Read(p []byte) (int, error) {
	if f.readErr {
		return 0, errInjected
	}
	return f.Reader.Read(p)
}

func (f *tailFile) Close() error { return nil }

func TestReadTailSurvivesFileFailures(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	tm.WriteSample(fixedStatus(), "init")
	orig := openTail
	t.Cleanup(func() { openTail = orig })
	content := []byte("{\"a\":1}\n{\"a\":2}\n")

	openTail = func(string) (io.ReadSeekCloser, error) { return nil, errInjected }
	if rows := tm.ReadTail(5); len(rows) != 0 {
		t.Fatalf("open failure rows = %v", rows)
	}
	openTail = func(string) (io.ReadSeekCloser, error) {
		return &tailFile{Reader: bytes.NewReader(content), seekErrAt: 2}, nil
	}
	if rows := tm.ReadTail(5); len(rows) != 0 {
		t.Fatalf("seek failure rows = %v", rows)
	}
	openTail = func(string) (io.ReadSeekCloser, error) {
		return &tailFile{Reader: bytes.NewReader(content), readErr: true}, nil
	}
	if rows := tm.ReadTail(5); len(rows) != 0 {
		t.Fatalf("read failure rows = %v", rows)
	}
}

func TestTimeseriesCleanupOldSkipsBadPatternAndDanglingLinks(t *testing.T) {
	bad := filepath.Join(t.TempDir(), "bad[")
	tm := NewTimeseriesManager(fixedStatus, bad, 1, nil, func() float64 { return 1e12 })
	tm.cleanupOld(1) // a malformed glob pattern: nothing to do, no panic

	dir := t.TempDir()
	tm = NewTimeseriesManager(fixedStatus, dir, 1, nil, func() float64 { return 1e12 })
	link := filepath.Join(dir, "swarm_timeseries_dangling.jsonl")
	if err := os.Symlink(filepath.Join(dir, "missing"), link); err != nil {
		t.Fatal(err)
	}
	tm.cleanupOld(1)
	if _, err := os.Lstat(link); err != nil {
		t.Fatal("an entry that cannot be stat'ed must be left alone")
	}
}

// A sample too large to buffer is written straight through; a handle that
// refuses the write is closed and dropped.
func TestWriteSampleWriteFailureDropsHandle(t *testing.T) {
	tm := NewTimeseriesManager(fixedStatus, t.TempDir(), 1, nil, func() float64 { return 5 })
	if err := os.WriteFile(tm.Path, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	ro, err := os.Open(tm.Path)
	if err != nil {
		t.Fatal(err)
	}
	tm.fh = ro
	tm.WriteSample(fixedStatus(), strings.Repeat("r", 8192))
	if tm.fh != nil {
		t.Fatal("a handle that failed a write must be dropped")
	}
	if info, _ := os.Stat(tm.Path); info.Size() != 0 {
		t.Fatal("nothing may be written through a read-only handle")
	}
}
