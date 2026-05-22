#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the startup durability warning escalation.

Process-local control-plane state is fine on a single replica and broken
on multi-replica orchestrators. The startup banner must escalate from a
WARNING (single-replica) to an ERROR (multi-replica detected) so
operators don't miss the mismatch until users hit it in production.
"""

from __future__ import annotations

import logging

import pytest

from provide.uterm.server.app.factory import (
    _detect_multi_replica_environment,
    create_server_app,
)
from provide.uterm.server.models import AuthConfig, ControlPlaneConfig, ServerConfig


@pytest.fixture(autouse=True)
def _clear_multi_replica_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a single-replica environment for every test."""
    for var, _label in (
        ("KUBERNETES_SERVICE_HOST", "Kubernetes"),
        ("K_SERVICE", "Cloud Run"),
        ("WEBSITE_INSTANCE_ID", "Azure App Service"),
        ("ECS_CONTAINER_METADATA_URI", "AWS ECS"),
        ("ECS_CONTAINER_METADATA_URI_V4", "AWS ECS"),
        ("FLY_APP_NAME", "Fly.io"),
    ):
        monkeypatch.delenv(var, raising=False)


class TestMultiReplicaDetection:
    def test_no_env_returns_empty(self) -> None:
        assert _detect_multi_replica_environment() == set()

    def test_kubernetes_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        assert _detect_multi_replica_environment() == {"Kubernetes"}

    def test_cloud_run_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("K_SERVICE", "my-service")
        assert _detect_multi_replica_environment() == {"Cloud Run"}

    def test_multiple_hints_aggregate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        monkeypatch.setenv("FLY_APP_NAME", "uterm")
        assert _detect_multi_replica_environment() == {"Kubernetes", "Fly.io"}

    def test_empty_string_env_value_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An empty string set in env should not be treated as "present".
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "")
        assert _detect_multi_replica_environment() == set()


class TestDurabilityStartupWarning:
    def test_memory_backend_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="provide.uterm.server.app.factory")
        config = ServerConfig(
            auth=AuthConfig(mode="none"),
            control_plane=ControlPlaneConfig(backend="memory"),
        )
        create_server_app(config, api_only=True)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("standalone_server_durability=process-local" in m for m in msgs)

    def test_memory_backend_on_kubernetes_emits_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        caplog.set_level(logging.ERROR, logger="provide.uterm.server.app.factory")
        config = ServerConfig(
            auth=AuthConfig(mode="none"),
            control_plane=ControlPlaneConfig(backend="memory"),
        )
        create_server_app(config, api_only=True)
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "expected at least one ERROR-level record"
        assert any("multi-replica environment" in r.getMessage() for r in errors)
        assert any("Kubernetes" in r.getMessage() for r in errors)

    def test_memory_backend_single_replica_no_error(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.ERROR, logger="provide.uterm.server.app.factory")
        config = ServerConfig(
            auth=AuthConfig(mode="none"),
            control_plane=ControlPlaneConfig(backend="memory"),
        )
        create_server_app(config, api_only=True)
        # No ERROR should fire when no multi-replica env vars are set.
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not any("multi-replica environment" in r.getMessage() for r in errors)
