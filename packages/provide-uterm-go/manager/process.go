//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// agentIDRe matches the canonical agent_NNN id, mirroring _AGENT_ID_RE.
var agentIDRe = regexp.MustCompile(`^agent_(\d+)$`)

// workerEnvPassthrough is the allowlist of env vars forwarded to worker
// subprocesses, mirroring _WORKER_ENV_PASSTHROUGH.
var workerEnvPassthrough = map[string]struct{}{
	"PATH": {}, "HOME": {}, "USER": {}, "LOGNAME": {}, "SHELL": {},
	"LANG": {}, "LC_ALL": {}, "LC_CTYPE": {}, "LC_MESSAGES": {},
	"TMPDIR": {}, "TMP": {}, "TEMP": {}, "PYTHONPATH": {},
	"PYTHONHOME": {}, "VIRTUAL_ENV": {},
}

// WorkerRegistryEntry maps a worker type to its worker subprocess module and
// injects game-specific env, mirroring WorkerRegistryPlugin in protocols.py.
type WorkerRegistryEntry interface {
	WorkerType() string
	WorkerModule() string
	ConfigureWorkerEnv(env map[string]string, agent *AgentStatus, mgr *AgentManager, rawConfig map[string]any)
}

// SpawnCommandFunc builds the argv used to launch a worker. The default form
// mirrors the Python command [python3, -m, module, --config, path, --agent-id,
// id]; tests override it to launch a short-lived child (e.g. sleep/echo).
type SpawnCommandFunc func(workerModule, configPath, agentID string) []string

func defaultSpawnCommand(workerModule, configPath, agentID string) []string {
	return []string{"python3", "-m", workerModule, "--config", configPath, "--agent-id", agentID}
}

// AgentProcessManager spawns, monitors, and terminates agent processes,
// mirroring AgentProcessManager in process_impl.py + process_impl_spawn.py.
type AgentProcessManager struct {
	manager        *AgentManager
	workerRegistry map[string]WorkerRegistryEntry
	logDir         string

	SpawnCommand SpawnCommandFunc
	getenv       func(string) string
	environ      func() []string

	mu                sync.Mutex
	spawnCancel       context.CancelFunc
	spawnWG           sync.WaitGroup
	queuedSince       map[string]float64
	queuedLaunchDelay float64
	nextAgentIndex    int
	spawnNameStyle    string
	spawnNameBase     string
	lastSpawnConfig   string
	policyGate        AgentSpawnPolicyGate

	logger *slog.Logger
}

// NewAgentProcessManager constructs a process manager bound to mgr.
func NewAgentProcessManager(mgr *AgentManager, workerRegistry map[string]WorkerRegistryEntry, logDir string) *AgentProcessManager {
	if workerRegistry == nil {
		workerRegistry = map[string]WorkerRegistryEntry{}
	}
	return &AgentProcessManager{
		manager:           mgr,
		workerRegistry:    workerRegistry,
		logDir:            logDir,
		SpawnCommand:      defaultSpawnCommand,
		getenv:            os.Getenv,
		environ:           os.Environ,
		queuedSince:       map[string]float64{},
		queuedLaunchDelay: 30.0,
		spawnNameStyle:    "random",
		policyGate:        NoOpAgentSpawnPolicyGate{},
		logger:            getLogger("provide.uterm.manager.process"),
	}
}

// SetPolicyGate sets the external policy gate for agent spawning.
func (pm *AgentProcessManager) SetPolicyGate(gate AgentSpawnPolicyGate) { pm.policyGate = gate }

// parseAgentIndex extracts the numeric index from an agent_NNN id.
func parseAgentIndex(agentID string) (int, bool) {
	m := agentIDRe.FindStringSubmatch(strings.TrimSpace(agentID))
	if m == nil {
		return 0, false
	}
	n, err := strconv.Atoi(m[1])
	if err != nil {
		return 0, false
	}
	return n, true
}

// SyncNextAgentIndex advances nextAgentIndex past every known id.
func (pm *AgentProcessManager) SyncNextAgentIndex() int {
	maxSeen := -1
	seen := map[string]struct{}{}
	pm.manager.mu.Lock()
	for id := range pm.manager.Agents {
		seen[id] = struct{}{}
	}
	for id := range pm.manager.Processes {
		seen[id] = struct{}{}
	}
	pm.manager.mu.Unlock()
	for id := range seen {
		if idx, ok := parseAgentIndex(id); ok && idx > maxSeen {
			maxSeen = idx
		}
	}
	pm.mu.Lock()
	defer pm.mu.Unlock()
	if maxSeen+1 > pm.nextAgentIndex {
		pm.nextAgentIndex = maxSeen + 1
	}
	return pm.nextAgentIndex
}

// NoteAgentID advances nextAgentIndex to accommodate an externally supplied id.
func (pm *AgentProcessManager) NoteAgentID(agentID string) {
	idx, ok := parseAgentIndex(agentID)
	if !ok {
		return
	}
	pm.mu.Lock()
	defer pm.mu.Unlock()
	if idx+1 > pm.nextAgentIndex {
		pm.nextAgentIndex = idx + 1
	}
}

// AllocateAgentID returns a fresh unused agent_NNN id.
func (pm *AgentProcessManager) AllocateAgentID() (string, error) {
	idx := pm.SyncNextAgentIndex()
	pm.manager.mu.Lock()
	bound := len(pm.manager.Agents) + len(pm.manager.Processes) + 1
	pm.manager.mu.Unlock()
	for i := 0; i < bound; i++ {
		candidate := fmt.Sprintf("agent_%03d", idx)
		pm.manager.mu.Lock()
		_, inAgents := pm.manager.Agents[candidate]
		_, inProcs := pm.manager.Processes[candidate]
		pm.manager.mu.Unlock()
		if !inAgents && !inProcs {
			pm.mu.Lock()
			pm.nextAgentIndex = idx + 1
			pm.mu.Unlock()
			return candidate, nil
		}
		idx++
	}
	return "", fmt.Errorf("agent id allocation exhausted")
}

// loadWorkerType reads worker_type and the raw config from a config file.
// YAML is not available without an external dep; a JSON-content config (a valid
// YAML subset) is parsed fully, otherwise a minimal top-level `worker_type:`
// scan is used. See port notes.
func (pm *AgentProcessManager) loadWorkerType(configPath string) (string, map[string]any) {
	raw := map[string]any{}
	data, err := os.ReadFile(configPath) //nolint:gosec // path is sandbox-validated by the route
	if err != nil {
		pm.logger.Warn("worker_type_read_failed", "config_path", configPath, "error", err.Error())
		return "default", raw
	}
	if json.Unmarshal(data, &raw) == nil {
		wt, _ := raw["worker_type"].(string)
		if wt == "" {
			wt = "default"
		}
		return wt, raw
	}
	// Minimal flat scan for a top-level `worker_type: value`.
	wt := "default"
	for _, line := range strings.Split(string(data), "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "worker_type:") {
			v := strings.TrimSpace(strings.TrimPrefix(trimmed, "worker_type:"))
			v = strings.Trim(v, `"'`)
			if v != "" {
				wt = v
				raw["worker_type"] = v
			}
			break
		}
	}
	return wt, raw
}

// getRegistryEntry resolves the worker registry entry for workerType.
func (pm *AgentProcessManager) getRegistryEntry(workerType, configPath string) (WorkerRegistryEntry, error) {
	if entry, ok := pm.workerRegistry[workerType]; ok {
		return entry, nil
	}
	if len(pm.workerRegistry) == 1 && workerType == "default" {
		for _, entry := range pm.workerRegistry {
			return entry, nil
		}
	}
	keys := make([]string, 0, len(pm.workerRegistry))
	for k := range pm.workerRegistry {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return nil, fmt.Errorf("Unknown worker_type %q in %s. Registered: %v", workerType, configPath, keys) //nolint:staticcheck // matches Python error text
}

// buildWorkerEnv builds the environment for a worker subprocess.
func (pm *AgentProcessManager) buildWorkerEnv(envPrefix string, agentEntry *AgentStatus, registryEntry WorkerRegistryEntry, rawConfig map[string]any, agentID string) map[string]string {
	env := map[string]string{}
	for _, kv := range pm.environ() {
		i := strings.IndexByte(kv, '=')
		if i < 0 {
			continue
		}
		k, v := kv[:i], kv[i+1:]
		if _, ok := workerEnvPassthrough[k]; ok || strings.HasPrefix(k, envPrefix) {
			env[k] = v
		}
	}
	pm.scopeWorkerTokens(env, agentID)
	if pm.spawnNameStyle != "" {
		env[envPrefix+"NAME_STYLE"] = pm.spawnNameStyle
	}
	if pm.spawnNameBase != "" {
		env[envPrefix+"NAME_BASE"] = pm.spawnNameBase
	}
	if agentEntry != nil && registryEntry != nil {
		registryEntry.ConfigureWorkerEnv(env, agentEntry, pm.manager, rawConfig)
	}
	return env
}

// scopeWorkerTokens down-scopes the manager tokens in a worker environment,
// mirroring _scope_worker_tokens.
func (pm *AgentProcessManager) scopeWorkerTokens(env map[string]string, agentID string) {
	cfg := pm.manager.Config
	operatorVar := cfg.AuthTokenEnvVar
	workerVar := cfg.AuthWorkerTokenEnvVar
	if workerVar == "" {
		workerVar = "UTERM_MANAGER_WORKER_TOKEN"
	}
	workerToken := strings.TrimSpace(pm.getenv(workerVar))
	if workerToken != "" {
		env[operatorVar] = deriveAgentToken(workerToken, agentID)
	}
	delete(env, workerVar)
}

// envSlice converts an env map to the KEY=VALUE form exec.Cmd expects.
func envSlice(env map[string]string) []string {
	out := make([]string, 0, len(env))
	for k, v := range env {
		out = append(out, k+"="+v)
	}
	return out
}

// SpawnAgent spawns a worker for agentID from configPath, mirroring
// process_impl_spawn.spawn_agent.
func (pm *AgentProcessManager) SpawnAgent(ctx context.Context, configPath, agentID string) (string, error) {
	pm.NoteAgentID(agentID)
	pm.manager.mu.Lock()
	nAgents := len(pm.manager.Agents)
	pm.manager.mu.Unlock()
	if nAgents >= pm.manager.MaxAgents {
		return "", fmt.Errorf("Max agents (%d) reached", pm.manager.MaxAgents) //nolint:staticcheck // matches Python error text
	}
	if _, err := os.Stat(configPath); err != nil {
		return "", fmt.Errorf("Config not found: %s", configPath) //nolint:staticcheck // matches Python error text
	}

	pm.logger.Info("spawning_agent", "agent_id", agentID, "config_path", configPath)

	workerType, raw := pm.loadWorkerType(configPath)

	if !pm.policyGate.InterceptSpawn(ctx, agentID, configPath, raw) {
		pm.logger.Warn("agent_spawn_rejected_by_policy", "agent_id", agentID)
		return "", fmt.Errorf("Spawn rejected by policy for agent %s", agentID) //nolint:staticcheck // matches Python error text
	}

	registryEntry, err := pm.getRegistryEntry(workerType, configPath)
	if err != nil {
		pm.logger.Error("agent_spawn_failed", "agent_id", agentID, "error", err.Error())
		return "", fmt.Errorf("Failed to spawn agent: %w", err) //nolint:staticcheck // matches Python error text
	}
	cmd := pm.SpawnCommand(registryEntry.WorkerModule(), configPath, agentID)

	envPrefix := pm.manager.Config.WorkerEnvPrefix
	pm.manager.mu.Lock()
	agentEntry := pm.manager.Agents[agentID]
	pm.manager.mu.Unlock()
	env := pm.buildWorkerEnv(envPrefix, agentEntry, registryEntry, raw, agentID)

	proc, err := pm.spawnProcess(agentID, cmd, env)
	if err != nil {
		pm.logger.Error("agent_spawn_failed", "agent_id", agentID, "error", err.Error())
		return "", fmt.Errorf("Failed to spawn agent: %w", err) //nolint:staticcheck // matches Python error text
	}

	now := nowUnix()
	pm.manager.mu.Lock()
	if agent, ok := pm.manager.Agents[agentID]; ok {
		agent.PID = intPtr(proc.PID())
		agent.State = "running"
		agent.LastUpdateTime = now
		agent.StartedAt = floatPtr(now)
		agent.StoppedAt = nil
	} else {
		a := newAgentStatus(agentID)
		a.PID = intPtr(proc.PID())
		a.Config = strPtr(configPath)
		a.State = "running"
		a.StartedAt = floatPtr(now)
		a.LastUpdateTime = now
		pm.manager.Agents[agentID] = a
	}
	pm.manager.Processes[agentID] = proc
	pm.manager.mu.Unlock()

	pm.mu.Lock()
	pm.lastSpawnConfig = configPath
	pm.mu.Unlock()

	pm.logger.Info(EventAgentSpawned, "agent_id", agentID, "pid", proc.PID(), "worker_type", workerType)
	pm.manager.BroadcastStatus()
	return agentID, nil
}

// spawnProcess opens the worker log (rotating an oversized one), starts the
// child in its own session, and returns a managed handle.
func (pm *AgentProcessManager) spawnProcess(agentID string, argv []string, env map[string]string) (processHandle, error) {
	logDir := pm.logDir
	if logDir == "" {
		logDir = "logs/workers"
	}
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		return nil, err
	}
	logPath := filepath.Join(logDir, agentID+".log")
	if info, err := os.Stat(logPath); err == nil && info.Mode().IsRegular() && info.Size() > WorkerLogMaxBytes {
		prev := filepath.Join(logDir, agentID+".log.prev")
		_ = os.Remove(prev)
		_ = os.Rename(logPath, prev)
	}
	logHandle, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return nil, err
	}
	if len(argv) == 0 {
		_ = logHandle.Close()
		return nil, fmt.Errorf("empty spawn command")
	}
	cmd := exec.Command(argv[0], argv[1:]...) //nolint:gosec // argv is manager-built
	cmd.Env = envSlice(env)
	cmd.Stdout = logHandle
	cmd.Stderr = logHandle
	cmd.SysProcAttr = newSysProcAttr()
	if err := cmd.Start(); err != nil {
		_ = logHandle.Close()
		return nil, err
	}
	// The child inherited a dup of the log fd; the parent's copy can close.
	_ = logHandle.Close()
	return newManagedProcess(cmd), nil
}

// resolveStopPid mirrors _resolve_stop_pid.
func resolveStopPid(process processHandle, pid int) int {
	if pid > 0 {
		return pid
	}
	if process != nil {
		return process.PID()
	}
	return 0
}

// stopProcessTree terminates a process group gracefully then forcefully,
// mirroring _stop_process_tree. When process is nil, only the fallback SIGKILL
// signal is sent (no wait).
func (pm *AgentProcessManager) stopProcessTree(agentID string, process processHandle, pid int, timeout time.Duration) {
	resolvedPid := resolveStopPid(process, pid)
	if resolvedPid <= 0 {
		return
	}
	if process == nil {
		_ = signalGroupByPID(resolvedPid, syscall.SIGKILL)
		pm.logger.Warn("agent_force_killed", "agent_id", agentID)
		return
	}
	_ = process.SignalGroup(syscall.SIGTERM)
	if err := process.WaitExit(timeout); err == nil {
		pm.logger.Info("agent_terminated", "agent_id", agentID)
		return
	}
	_ = process.SignalGroup(syscall.SIGKILL)
	_ = process.WaitExit(1 * time.Second)
	pm.logger.Warn("agent_force_killed", "agent_id", agentID)
}

// KillAgent terminates agentID's process tree and marks it stopped, mirroring
// kill_agent.
func (pm *AgentProcessManager) KillAgent(agentID string) {
	pm.logger.Info(EventAgentKilled, "agent_id", agentID)
	pm.manager.mu.Lock()
	process := pm.manager.Processes[agentID]
	agent := pm.manager.Agents[agentID]
	fallbackPid := 0
	if process == nil && agent != nil && agent.PID != nil {
		fallbackPid = *agent.PID
	}
	pm.manager.mu.Unlock()

	pm.stopProcessTree(agentID, process, fallbackPid, time.Duration(stopTimeoutS*float64(time.Second)))

	now := nowUnix()
	pm.manager.mu.Lock()
	if a, ok := pm.manager.Agents[agentID]; ok {
		a.State = "stopped"
		a.StoppedAt = floatPtr(now)
	}
	delete(pm.manager.Processes, agentID)
	pm.manager.mu.Unlock()
	pm.releaseAgentAccount(agentID)
	pm.manager.BroadcastStatus()
}

// releaseAgentAccount is a no-op in the bare manager (no account pool plugin).
func (pm *AgentProcessManager) releaseAgentAccount(_ string) {}
