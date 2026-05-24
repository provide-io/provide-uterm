#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the _run exception classifier."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from provide.uterm.server.runtime import _classify_run_error


class TestClassifyRunError:
    def test_cancelled_returns_cancelled(self) -> None:
        assert _classify_run_error(asyncio.CancelledError()) == "cancelled"

    def test_value_error_is_permanent(self) -> None:
        assert _classify_run_error(ValueError("bad config")) == "permanent"

    def test_http_401_is_permanent(self) -> None:
        exc = RuntimeError("auth failed")
        exc.status_code = 401  # type: ignore[attr-defined]
        assert _classify_run_error(exc) == "permanent"

    def test_http_403_is_permanent(self) -> None:
        exc = RuntimeError("forbidden")
        exc.status_code = 403  # type: ignore[attr-defined]
        assert _classify_run_error(exc) == "permanent"

    def test_http_404_is_permanent(self) -> None:
        exc = RuntimeError("not found")
        exc.status_code = 404  # type: ignore[attr-defined]
        assert _classify_run_error(exc) == "permanent"

    def test_http_500_is_retry(self) -> None:
        exc = RuntimeError("upstream broke")
        exc.status_code = 500  # type: ignore[attr-defined]
        assert _classify_run_error(exc) == "retry"

    def test_response_status_code_attribute_path(self) -> None:
        """httpx-style exceptions hang the status on response.status_code."""
        exc = RuntimeError("auth failed")
        exc.response = SimpleNamespace(status_code=403)  # type: ignore[attr-defined]
        assert _classify_run_error(exc) == "permanent"

    def test_generic_runtime_error_is_retry(self) -> None:
        assert _classify_run_error(RuntimeError("flaky")) == "retry"

    def test_connection_refused_is_retry(self) -> None:
        assert _classify_run_error(ConnectionRefusedError()) == "retry"

    def test_os_error_is_retry(self) -> None:
        assert _classify_run_error(OSError("transient")) == "retry"
