//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

// ManagerConfig configures a generic swarm manager instance. It mirrors the
// Pydantic ManagerConfig in manager/config.py, including its default values.
type ManagerConfig struct {
	Title     string `json:"title"`
	Host      string `json:"host"`
	Port      int    `json:"port"`
	MaxAgents int    `json:"max_agents"`
	LogLevel  string `json:"log_level"`

	// File paths (relative or absolute).
	StateFile     string `json:"state_file"`
	TimeseriesDir string `json:"timeseries_dir"`
	LogDir        string `json:"log_dir"`

	// Timing.
	HealthCheckIntervalS int     `json:"health_check_interval_s"`
	HeartbeatTimeoutS    float64 `json:"heartbeat_timeout_s"`
	SaveIntervalS        float64 `json:"save_interval_s"`
	TimeseriesIntervalS  int     `json:"timeseries_interval_s"`

	// Auth.
	AuthTokenEnvVar       string `json:"auth_token_env_var"`
	AuthWorkerTokenEnvVar string `json:"auth_worker_token_env_var"`
	// EnforcePerAgentWorkerToken rejects the raw fleet-shared worker token on
	// the self-report routes and accepts only the per-agent derived token.
	EnforcePerAgentWorkerToken bool     `json:"enforce_per_agent_worker_token"`
	CORSOrigins                []string `json:"cors_origins"`

	// Dashboard.
	DashboardHTML string `json:"dashboard_html"`
	StaticDir     string `json:"static_dir"`

	// Worker env-var prefix forwarded to subprocesses.
	WorkerEnvPrefix string `json:"worker_env_prefix"`
	// Optional worker process resource limits (0 = disabled).
	WorkerRlimitNofileSoft int `json:"worker_rlimit_nofile_soft"`
	WorkerRlimitNofileHard int `json:"worker_rlimit_nofile_hard"`
	WorkerRlimitASMB       int `json:"worker_rlimit_as_mb"`
	WorkerRlimitCPUS       int `json:"worker_rlimit_cpu_s"`

	// SpawnConfigDir is the directory that swarm config files must live under.
	SpawnConfigDir string `json:"spawn_config_dir"`

	// Governance & Policy.
	SpawnPolicyWebhookURL      string  `json:"spawn_policy_webhook_url"`
	SpawnPolicyWebhookSecret   string  `json:"spawn_policy_webhook_secret"`
	SpawnPolicyWebhookTimeoutS float64 `json:"spawn_policy_webhook_timeout_s"`

	// Auto-shutdown when all MCP clients disconnect and no agents are active.
	AutoShutdownEnabled bool    `json:"auto_shutdown_enabled"`
	AutoShutdownGraceS  float64 `json:"auto_shutdown_grace_s"`

	// Paths that never require auth.
	AuthPublicPaths    []string `json:"auth_public_paths"`
	AuthPublicPrefixes []string `json:"auth_public_prefixes"`
}

// DefaultManagerConfig returns a ManagerConfig populated with the same defaults
// as the Python ManagerConfig() constructor.
func DefaultManagerConfig() ManagerConfig {
	return ManagerConfig{
		Title:     "Swarm Manager",
		Host:      "127.0.0.1",
		Port:      2272,
		MaxAgents: 200,
		LogLevel:  "info",

		HealthCheckIntervalS: HealthCheckIntervalS,
		HeartbeatTimeoutS:    HeartbeatTimeoutS,
		SaveIntervalS:        SaveIntervalS,
		TimeseriesIntervalS:  TimeseriesIntervalS,

		AuthTokenEnvVar:       "UTERM_MANAGER_API_TOKEN",
		AuthWorkerTokenEnvVar: "UTERM_MANAGER_WORKER_TOKEN",
		CORSOrigins:           []string{"http://localhost:2272"},

		WorkerEnvPrefix: "UTERM_",

		SpawnPolicyWebhookTimeoutS: 2.0,
		AutoShutdownGraceS:         30.0,

		AuthPublicPaths:    []string{"/", "/dashboard", "/hijack", "/hijack/", "/hijack/hijack.html"},
		AuthPublicPrefixes: []string{"/static/", "/hijack/assets/"},
	}
}
