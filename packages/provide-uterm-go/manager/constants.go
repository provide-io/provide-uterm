//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package manager is a behavior-faithful Go port of the provide-uterm External
// Management Tier (the uterm-manager agent-fleet service). It spawns and
// supervises agent processes, tracks their status and a JSONL timeseries, and
// exposes the same REST API surface as the Python implementation under
// packages/provide-uterm-platform/src/provide/uterm/manager/.
package manager

import "time"

// Timing constants for the swarm manager. These mirror manager/constants.py.
const (
	// HeartbeatTimeoutS is how long to wait after an agent's last heartbeat
	// before declaring it dead.
	HeartbeatTimeoutS float64 = 60.0

	// SaveIntervalS is how often the manager persists swarm state to disk.
	SaveIntervalS float64 = 60.0

	// TimeseriesIntervalS is the default timeseries sample interval.
	TimeseriesIntervalS int = 20

	// EpochTurnDropRatio: a turn-count drop exceeding this ratio of the
	// previous total signals a swarm restart (new epoch).
	EpochTurnDropRatio float64 = 0.20

	// EpochTurnDropMin is the minimum absolute turn drop to trigger an epoch
	// boundary.
	EpochTurnDropMin int = 50

	// HealthCheckIntervalS is the default health-check polling interval.
	HealthCheckIntervalS int = 10

	// WorkerLogMaxBytes: rotate the worker log on spawn when it exceeds this.
	WorkerLogMaxBytes int64 = 50 * 1024 * 1024 // 50 MB

	// WorkerLogRetentionS: delete stale .prev files after this age.
	WorkerLogRetentionS float64 = 3.0 * 86400 // 3 days

	// TimeseriesMaxBytes: rotate when file exceeds this size.
	TimeseriesMaxBytes int64 = 50 * 1024 * 1024 // 50 MB

	// TimeseriesRetentionS: delete old timeseries files after this age.
	TimeseriesRetentionS float64 = 7.0 * 86400 // 7 days

	// ConfigDirEnvVar names the directory that swarm config files must live
	// under. Spawn requests are refused unless a config dir is configured
	// (here or via ManagerConfig.SpawnConfigDir) and the resolved config path
	// is contained within it — this is the spawn sandbox.
	ConfigDirEnvVar string = "UTERM_CONFIG_DIR"

	// stopTimeoutS is the graceful-kill timeout (SIGTERM → wait → SIGKILL).
	stopTimeoutS float64 = 5.0
)

// nowUnix returns the current wall-clock time as a float64 Unix timestamp,
// matching Python's time.time().
func nowUnix() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}
