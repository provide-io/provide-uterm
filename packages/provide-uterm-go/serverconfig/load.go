//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	toml "github.com/pelletier/go-toml/v2"
)

// tableSections mirrors config._TABLE_SECTIONS (the sections whose value MUST
// be a TOML table).
var tableSections = []string{
	"server", "auth", "ui", "recording", "profiles",
	"security", "tunnel", "webhooks", "pam", "control_plane",
}

// knownTopLevel lists every accepted top-level key (extra="forbid" parity).
var knownTopLevel = map[string]struct{}{
	"environment": {}, "server": {}, "auth": {}, "control_plane": {}, "ui": {},
	"recording": {}, "profiles": {}, "security": {}, "tunnel": {}, "webhooks": {},
	"pam": {}, "governance": {}, "audit": {}, "sessions": {}, "graphical_targets": {},
	"session_idle_timeout_s": {}, "session_retention_s": {}, "browser_rate_limit_per_sec": {},
	"worker_frame_on_invalid": {}, "max_connections_per_principal": {}, "max_workers": {},
}

// errExtraInputs returns the exact Pydantic extra="forbid" message; the leading
// capital is deliberate for byte-for-byte parity with the Python validator.
//
//nolint:staticcheck // ST1005: message must match Python's "Extra inputs are not permitted"
func errExtraInputs() error { return fmt.Errorf("Extra inputs are not permitted") }

// pyTypeName maps a decoded Go value to the Python type name used in the
// "[section] must be a table (got X)" error.
func pyTypeName(v any) string {
	switch v.(type) {
	case []any:
		return "list"
	case string:
		return "str"
	case bool:
		return "bool"
	case int64, int:
		return "int"
	case float64:
		return "float"
	case map[string]any:
		return "dict"
	case nil:
		return "NoneType"
	default:
		return fmt.Sprintf("%T", v)
	}
}

func deepMerge(base, override map[string]any) map[string]any {
	merged := make(map[string]any, len(base))
	for k, v := range base {
		merged[k] = v
	}
	for k, v := range override {
		if vm, ok := v.(map[string]any); ok {
			if bm, ok := merged[k].(map[string]any); ok {
				merged[k] = deepMerge(bm, vm)
				continue
			}
		}
		merged[k] = v
	}
	return merged
}

func structToMap(v any) (map[string]any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return m, nil
}

// decodeSection merges userVal (a decoded table) over dst's current defaults,
// rejecting unknown keys (extra="forbid"), and decodes the merged result back
// into dst.
func decodeSection(dst any, userVal any) error {
	userMap, ok := userVal.(map[string]any)
	if !ok {
		return nil
	}
	dm, err := structToMap(dst)
	if err != nil {
		return err
	}
	for k := range userMap {
		if _, known := dm[k]; !known {
			return errExtraInputs()
		}
	}
	merged := deepMerge(dm, userMap)
	b, err := json.Marshal(merged)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, dst)
}

// ConfigFromMapping builds a validated config from a plain mapping (the Go
// equivalent of config.config_from_mapping: user values deep-merge over the
// defaults, then per-model validators run). Keys and messages mirror Python.
func ConfigFromMapping(data map[string]any) (*UtermServerConfig, error) {
	for _, section := range tableSections {
		if v, present := data[section]; present {
			if _, ok := v.(map[string]any); !ok {
				return nil, fmt.Errorf("[%s] must be a table (got %s)", section, pyTypeName(v))
			}
		}
	}
	for k := range data {
		if _, known := knownTopLevel[k]; !known {
			return nil, errExtraInputs()
		}
	}

	cfg := DefaultServerConfig()

	sectionTargets := []struct {
		key string
		dst any
	}{
		{"server", &cfg.Server}, {"auth", &cfg.Auth}, {"control_plane", &cfg.ControlPlane},
		{"ui", &cfg.UI}, {"recording", &cfg.Recording}, {"profiles", &cfg.Profiles},
		{"security", &cfg.Security}, {"tunnel", &cfg.Tunnel}, {"webhooks", &cfg.Webhooks},
		{"pam", &cfg.Pam}, {"governance", &cfg.Governance}, {"audit", &cfg.Audit},
	}
	for _, st := range sectionTargets {
		if err := decodeSection(st.dst, data[st.key]); err != nil {
			return nil, err
		}
	}

	if err := applyTopScalars(cfg, data); err != nil {
		return nil, err
	}
	if err := applySessions(cfg, data); err != nil {
		return nil, err
	}
	if err := applyGraphicalTargets(cfg, data); err != nil {
		return nil, err
	}

	// Post-decode normalizers (Pydantic model/field validators).
	normalizeUI(&cfg.UI)
	deriveServerURL(&cfg.Server)
	if err := runValidators(cfg); err != nil {
		return nil, err
	}
	return cfg, nil
}

func applyTopScalars(cfg *UtermServerConfig, data map[string]any) error {
	if v, ok := data["environment"]; ok {
		s := asString(v)
		if !inSet(s, "dev", "production") {
			return literalError("environment", "dev", "production")
		}
		cfg.Environment = s
	}
	if v, ok := asInt(data["session_idle_timeout_s"]); ok {
		cfg.SessionIdleTimeoutS = v
	}
	if v, ok := asInt(data["session_retention_s"]); ok {
		cfg.SessionRetentionS = v
	}
	if v, ok := data["browser_rate_limit_per_sec"]; ok {
		cfg.BrowserRateLimitPerSec = asFloat(v)
	}
	if v, ok := data["worker_frame_on_invalid"]; ok {
		s := asString(v)
		if !inSet(s, "drop", "reject") {
			return literalError("worker_frame_on_invalid", "drop", "reject")
		}
		cfg.WorkerFrameOnInvalid = s
	}
	if v, ok := asInt(data["max_connections_per_principal"]); ok {
		cfg.MaxConnectionsPerPrincipal = v
	}
	if v, ok := asInt(data["max_workers"]); ok {
		cfg.MaxWorkers = v
	}
	return nil
}

func applySessions(cfg *UtermServerConfig, data map[string]any) error {
	raw, present := data["sessions"]
	if !present {
		return nil // keep the default session
	}
	list, ok := raw.([]any)
	if !ok {
		return fmt.Errorf("sessions must be a list of tables")
	}
	out := []SessionDefinition{}
	for _, entry := range list {
		m, ok := entry.(map[string]any)
		if !ok {
			continue // skip non-dict entries (parity with config_from_mapping)
		}
		sd, err := sessionFromMapping(m)
		if err != nil {
			return err
		}
		out = append(out, sd)
	}
	cfg.Sessions = out
	return nil
}

// applyGraphicalTargets parses the [[graphical_targets]] array. It mirrors the
// C# ConfigLoader graphical_targets branch: unknown/missing keys take their
// defaults, enabled defaults to true, dimensions default to 640x480, and a
// blank target_id is left blank (SeedGraphicalTargets assigns one). Non-table
// entries are skipped.
func applyGraphicalTargets(cfg *UtermServerConfig, data map[string]any) error {
	raw, present := data["graphical_targets"]
	if !present {
		return nil
	}
	list, ok := raw.([]any)
	if !ok {
		return fmt.Errorf("graphical_targets must be a list of tables")
	}
	out := []GraphicalTargetConfig{}
	for _, entry := range list {
		m, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		gt := GraphicalTargetConfig{
			TargetID:      asString(m["target_id"]),
			TenantID:      asString(m["tenant_id"]),
			Protocol:      strOr(m["protocol"], "rfb"),
			TargetAddress: asString(m["target_address"]),
			VMName:        optString(m["vm_name"]),
			Name:          asString(m["name"]),
			Description:   optString(m["description"]),
			Enabled:       boolOr(m, "enabled", true),
			Width:         intOr(m, "width", 640),
			Height:        intOr(m, "height", 480),
			IsStatic:      boolOr(m, "is_static", false),
		}
		if cfgTable, ok := m["config"].(map[string]any); ok {
			gt.Config = cfgTable
		}
		out = append(out, gt)
	}
	cfg.GraphicalTargets = out
	return nil
}

func boolOr(m map[string]any, key string, def bool) bool {
	if v, ok := m[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return def
}

func intOr(m map[string]any, key string, def int) int {
	if v, ok := asInt(m[key]); ok {
		return v
	}
	return def
}

func runValidators(cfg *UtermServerConfig) error {
	if err := validateAuth(&cfg.Auth); err != nil {
		return err
	}
	if err := validateControlPlane(&cfg.ControlPlane); err != nil {
		return err
	}
	if err := validateRecording(&cfg.Recording); err != nil {
		return err
	}
	if err := validateSecurity(&cfg.Security); err != nil {
		return err
	}
	if err := validateTunnel(&cfg.Tunnel); err != nil {
		return err
	}
	if err := validatePam(&cfg.Pam); err != nil {
		return err
	}
	if err := validateGovernance(&cfg.Governance); err != nil {
		return err
	}
	if err := validateAudit(&cfg.Audit); err != nil {
		return err
	}
	if cfg.MaxWorkers < 1 {
		return fmt.Errorf("max_workers must be >= 1, got: %d", cfg.MaxWorkers)
	}
	return nil
}

func asFloat(v any) float64 {
	switch t := v.(type) {
	case float64:
		return t
	case int64:
		return float64(t)
	case int:
		return float64(t)
	default:
		return 0
	}
}

// LoadServerConfig loads server config from a TOML file, or returns the
// default config when path is empty. It mirrors config.load_server_config,
// including resolving a relative recording.directory against the config file's
// parent directory.
func LoadServerConfig(path string) (*UtermServerConfig, error) {
	if path == "" {
		return DefaultServerConfig(), nil
	}
	raw, err := os.ReadFile(path) //nolint:gosec // path is operator-supplied config
	if err != nil {
		return nil, err
	}
	var data map[string]any
	if err := toml.Unmarshal(raw, &data); err != nil {
		return nil, err
	}
	cfg, err := ConfigFromMapping(data)
	if err != nil {
		return nil, err
	}
	if !filepath.IsAbs(cfg.Recording.Directory) {
		abs, err := filepath.Abs(filepath.Join(filepath.Dir(path), cfg.Recording.Directory))
		if err != nil {
			return nil, err
		}
		cfg.Recording.Directory = abs
	}
	return cfg, nil
}
