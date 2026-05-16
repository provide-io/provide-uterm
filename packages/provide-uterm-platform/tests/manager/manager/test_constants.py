#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.manager.constants."""

from provide.uterm.manager.constants import (
    EPOCH_TURN_DROP_MIN,
    EPOCH_TURN_DROP_RATIO,
    HEALTH_CHECK_INTERVAL_S,
    HEARTBEAT_TIMEOUT_S,
    SAVE_INTERVAL_S,
    TIMESERIES_INTERVAL_S,
)


def test_constants_are_positive():
    assert HEARTBEAT_TIMEOUT_S > 0
    assert SAVE_INTERVAL_S > 0
    assert TIMESERIES_INTERVAL_S > 0
    assert EPOCH_TURN_DROP_RATIO > 0
    assert EPOCH_TURN_DROP_MIN > 0
    assert HEALTH_CHECK_INTERVAL_S > 0
