#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Timing constants for the swarm manager."""

from __future__ import annotations

# How long to wait after an agent's last heartbeat before declaring it dead.
HEARTBEAT_TIMEOUT_S: float = 60.0

# How often the manager persists swarm state to disk.
SAVE_INTERVAL_S: float = 60.0

# Default timeseries sample interval.
TIMESERIES_INTERVAL_S: int = 20

# Epoch detection: a turn-count drop exceeding this ratio of the previous
# total signals a swarm restart (new epoch).
EPOCH_TURN_DROP_RATIO: float = 0.20

# Minimum absolute turn drop to trigger an epoch boundary.
EPOCH_TURN_DROP_MIN: int = 50

# Default health-check polling interval (seconds).
HEALTH_CHECK_INTERVAL_S: int = 10

# Worker log rotation: rotate on spawn when file exceeds this size.
WORKER_LOG_MAX_BYTES: int = 50 * 1024 * 1024  # 50 MB

# Worker log retention: delete stale .prev files after this age.
WORKER_LOG_RETENTION_S: float = 3.0 * 86400  # 3 days

# Timeseries rotation: rotate when file exceeds this size.
TIMESERIES_MAX_BYTES: int = 50 * 1024 * 1024  # 50 MB

# Timeseries retention: delete old timeseries files after this age.
TIMESERIES_RETENTION_S: float = 7.0 * 86400  # 7 days
