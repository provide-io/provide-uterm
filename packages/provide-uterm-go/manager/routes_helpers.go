//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"errors"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// agentIDPathRe mirrors the Path(pattern=r"^[\w\-]+$") constraint on the
// register/status routes.
var agentIDPathRe = regexp.MustCompile(`^[\w\-]+$`)

// operatorFields are the operator-authority fields a worker self-report
// (register) must never set, mirroring _OPERATOR_FIELDS in agent_ops.py.
var operatorFields = []string{
	"pending_command_seq", "pending_command_type", "pending_command_payload",
	"manager_command_history", "is_hijacked", "hijacked_by", "hijacked_at",
	"paused", "config",
}

// buildActionResponse mirrors _build_action_response (no plugin path).
func buildActionResponse(agentID, action, source string, applied, queued bool, result map[string]any, state string) map[string]any {
	return map[string]any{
		"agent_id": agentID,
		"action":   action,
		"source":   source,
		"applied":  applied,
		"queued":   queued,
		"result":   result,
		"state":    state,
	}
}

// commandHistoryRows returns the agent's history slice (ensuring non-nil).
func commandHistoryRows(a *AgentStatus) []map[string]any {
	if a.ManagerCommandHistory == nil {
		a.ManagerCommandHistory = []map[string]any{}
	}
	return a.ManagerCommandHistory
}

// appendCommandHistory appends a copy of entry, capping to the last 25.
func appendCommandHistory(a *AgentStatus, entry map[string]any) {
	rows := commandHistoryRows(a)
	rows = append(rows, copyMap(entry))
	if len(rows) > 25 {
		rows = rows[len(rows)-25:]
	}
	a.ManagerCommandHistory = rows
}

// updateCommandHistory updates the newest history row matching seq.
func updateCommandHistory(a *AgentStatus, seq int, updates map[string]any) {
	if seq <= 0 {
		return
	}
	rows := commandHistoryRows(a)
	for i := len(rows) - 1; i >= 0; i-- {
		if asInt(rows[i]["seq"]) != seq {
			continue
		}
		for k, v := range updates {
			rows[i][k] = v
		}
		return
	}
}

// queueManagerCommand queues a manager command onto the agent, mirroring
// _queue_manager_command.
func queueManagerCommand(a *AgentStatus, commandType string, payload map[string]any) map[string]any {
	replacedSeq := a.PendingCommandSeq
	if replacedSeq > 0 && a.PendingCommandType != nil && *a.PendingCommandType != "" {
		updateCommandHistory(a, replacedSeq, map[string]any{
			"status":      "replaced",
			"replaced_by": replacedSeq + 1,
			"updated_at":  nowUnix(),
		})
	}
	a.PendingCommandSeq = replacedSeq + 1
	a.PendingCommandType = strPtr(commandType)
	a.PendingCommandPayload = copyMap(payload)
	var replaces any
	if replacedSeq > 0 {
		replaces = replacedSeq
	}
	queued := map[string]any{
		"seq":      a.PendingCommandSeq,
		"type":     commandType,
		"payload":  copyMap(payload),
		"replaces": replaces,
	}
	now := nowUnix()
	appendCommandHistory(a, map[string]any{
		"seq":              queued["seq"],
		"type":             commandType,
		"payload":          copyMap(payload),
		"status":           "queued",
		"queued_at":        now,
		"updated_at":       now,
		"replaces":         replaces,
		"replaced_by":      nil,
		"cancelled_reason": nil,
	})
	return queued
}

// cancelPendingManagerCommand mirrors _cancel_pending_manager_command.
func cancelPendingManagerCommand(a *AgentStatus, reason string) map[string]any {
	pendingSeq := a.PendingCommandSeq
	pendingType := ""
	if a.PendingCommandType != nil {
		pendingType = *a.PendingCommandType
	}
	if pendingSeq <= 0 || pendingType == "" {
		return nil
	}
	cancelled := map[string]any{
		"seq":              pendingSeq,
		"type":             pendingType,
		"payload":          copyMap(a.PendingCommandPayload),
		"cancelled_reason": reason,
	}
	updateCommandHistory(a, pendingSeq, map[string]any{
		"status":           "cancelled",
		"cancelled_reason": reason,
		"updated_at":       nowUnix(),
	})
	a.PendingCommandSeq = 0
	a.PendingCommandType = nil
	a.PendingCommandPayload = map[string]any{}
	return cancelled
}

// acknowledgeCommand clears a pending command the agent acknowledged, mirroring
// _acknowledge_command.
func acknowledgeCommand(a *AgentStatus, ackSeq int) {
	if ackSeq > 0 && ackSeq == a.PendingCommandSeq {
		updateCommandHistory(a, ackSeq, map[string]any{"status": "acknowledged", "updated_at": nowUnix()})
		a.PendingCommandSeq = 0
		a.PendingCommandType = nil
		a.PendingCommandPayload = map[string]any{}
	}
}

// buildPendingCommandResponse mirrors _build_pending_command_response.
func buildPendingCommandResponse(a *AgentStatus, ackSeq int) map[string]any {
	seq := a.PendingCommandSeq
	if a.PendingCommandType == nil || *a.PendingCommandType == "" || seq <= 0 || seq == ackSeq {
		return nil
	}
	return map[string]any{
		"seq":     seq,
		"type":    *a.PendingCommandType,
		"payload": copyMap(a.PendingCommandPayload),
	}
}

// copyMap returns a shallow copy of m (never nil).
func copyMap(m map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range m {
		out[k] = v
	}
	return out
}

// managerConfigDir returns the ManagerConfig default spawn dir, mirroring
// _manager_config_dir (the bare default is empty).
func managerConfigDir() string { return strings.TrimSpace(DefaultManagerConfig().SpawnConfigDir) }

// realpath resolves p to an absolute, symlink-resolved path (best effort for
// non-existent leaves), matching os.path.realpath semantics closely enough for
// the containment check.
func realpath(p string) string {
	abs, err := filepath.Abs(p)
	if err != nil {
		abs = p
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		return resolved
	}
	return abs
}

// validateConfigPath validates configPath is a safe YAML file inside the spawn
// sandbox, mirroring _validate_config_path. getenv is injectable for testing.
func validateConfigPath(configPath, configDirEnv string, getenv func(string) string) (string, error) {
	baseRaw := configDirEnv
	if baseRaw == "" {
		baseRaw = strings.TrimSpace(getenv(ConfigDirEnvVar))
	}
	if baseRaw == "" {
		baseRaw = managerConfigDir()
	}
	if baseRaw == "" {
		return "", errors.New("config dir is not configured; refusing to spawn from an unrestricted path")
	}
	base := realpath(baseRaw)
	resolved := realpath(configPath)
	suffix := strings.ToLower(filepath.Ext(resolved))
	if suffix != ".yaml" && suffix != ".yml" {
		return "", errors.New("config_path must be a .yaml or .yml file: " + configPath)
	}
	if !isRelativeTo(resolved, base) {
		return "", errors.New("config_path is outside config dir (" + base + "): " + configPath)
	}
	return resolved, nil
}

// isRelativeTo reports whether child is base or a descendant of base.
func isRelativeTo(child, base string) bool {
	rel, err := filepath.Rel(base, child)
	if err != nil {
		return false
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return false
	}
	return true
}

// sortedStrings returns a sorted copy of s (used for deterministic error text).
func sortedStrings(s []string) []string {
	out := append([]string(nil), s...)
	sort.Strings(out)
	return out
}

// floatOf coerces a JSON value to float64 (numbers decode as float64).
func floatOf(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case int:
		return float64(n)
	}
	return 0
}

// strOf coerces a JSON value to string ("" for non-strings/nil).
func strOf(v any) string {
	s, _ := v.(string)
	return s
}

// quoteAll wraps each element in single quotes (matching Python repr of a list
// of strings).
func quoteAll(ss []string) []string {
	out := make([]string, len(ss))
	for i, s := range ss {
		out[i] = "'" + s + "'"
	}
	return out
}

// itoaInt renders an int as decimal.
func itoaInt(n int) string { return strconv.Itoa(n) }

// firstNonEmptyStr returns the first argument that is a non-empty string.
func firstNonEmptyStr(vals ...any) string {
	for _, v := range vals {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return ""
}

// orDefault returns v if non-nil, else def.
func orDefault(v any, def any) any {
	if v == nil {
		return def
	}
	return v
}

// ptrOrNil returns *p as any, or nil when p is nil (for JSON null parity).
func ptrOrNil(p *string) any {
	if p == nil {
		return nil
	}
	return *p
}

// strPtrOrNil returns a *string for a string value, or nil otherwise.
func strPtrOrNil(v any) *string {
	if s, ok := v.(string); ok {
		return &s
	}
	return nil
}

// floatPtrOrNil returns a *float64 for a numeric value, or nil otherwise.
func floatPtrOrNil(v any) *float64 {
	switch n := v.(type) {
	case float64:
		return &n
	case int:
		f := float64(n)
		return &f
	}
	return nil
}

// toRowSlice coerces a decoded JSON array into []map[string]any (dropping
// non-object elements), matching list[dict[str, Any]].
func toRowSlice(v any) []map[string]any {
	arr, ok := v.([]any)
	if !ok {
		return []map[string]any{}
	}
	out := make([]map[string]any, 0, len(arr))
	for _, e := range arr {
		if m, ok := e.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}
