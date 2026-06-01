#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests wiring the WORM audit chain into the running server.

Covers:
  * ``AuditConfig`` defaults + the chain_enabled-requires-chain_file validator.
  * ``audit_event`` best-effort chain hook (append / failure-swallow / disabled).
  * ``_lifespan`` startup resume/verify/checkpoint flow + the periodic checkpoint
    task + the clean-shutdown head flush, including the genesis-no-alarm vs the
    real truncation-alarm predicate.
  * ``compute_security_posture`` audit_chain_enabled field + compliance warning.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.app.posture import compute_security_posture
from provide.uterm.server.audit import audit_event, configure_audit_chain
from provide.uterm.server.audit_chain import GENESIS_HASH, AuditChain, verify_audit_log
from provide.uterm.server.models import AuditConfig, AuthConfig, ServerConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_chain_global() -> Any:
    """Ensure the module-global audit chain never leaks between tests."""
    configure_audit_chain(None)
    yield
    configure_audit_chain(None)


def _make_audit_app(chain_file: Path) -> tuple[Any, Any]:
    """Create a test app with the audit chain enabled on ``chain_file``."""
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.sessions = []
    config.audit = AuditConfig(chain_enabled=True, chain_file=str(chain_file))
    return create_server_app(config), config


async def _run_lifespan_one_tick(app: Any) -> None:
    """Enter the lifespan, let background tasks run one iteration, then exit.

    Mirrors the harness in test_app_coverage.py: the first ``asyncio.sleep``
    from each task returns immediately (run the loop body once); the second
    blocks forever until lifespan teardown cancels it.
    """
    ran_once: set[int] = set()
    _real_sleep = asyncio.sleep

    async def _patched_sleep(delay: float) -> None:
        task = id(asyncio.current_task())
        if task in ran_once:
            await _real_sleep(3600)
            return
        ran_once.add(task)
        await _real_sleep(0)

    with patch("asyncio.sleep", _patched_sleep):
        async with app.router.lifespan_context(app):
            for _ in range(20):
                await _real_sleep(0)


def _make_audit_app_api_only(chain_file: Path) -> Any:
    """Build an api_only audit-enabled app (the harness used by the reaper tests).

    ``api_only=True`` keeps the lifespan minimal so the periodic checkpoint task
    is the only thing ticking, mirroring test_factory_coverage.py's sweep tests.
    """
    config = default_server_config()
    config.sessions = []
    config.control_plane.reap_interval_s = 1
    config.audit = AuditConfig(chain_enabled=True, chain_file=str(chain_file))
    return create_server_app(config, api_only=True)


def _patch_fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the factory module's asyncio.sleep with a fast yield so the
    checkpoint loop ticks quickly under test (copied from test_factory_coverage)."""
    import provide.uterm.server.app.factory_impl as factory_impl

    real_sleep = asyncio.sleep

    async def _fast_sleep(_delay: float) -> None:
        await real_sleep(0.001)

    monkeypatch.setattr(factory_impl.asyncio, "sleep", _fast_sleep)


# ---------------------------------------------------------------------------
# 1. AuditConfig
# ---------------------------------------------------------------------------


class TestAuditConfig:
    def test_defaults_disabled(self) -> None:
        cfg = AuditConfig()
        assert cfg.chain_enabled is False
        assert cfg.chain_file is None

    def test_server_config_audit_default_disabled(self) -> None:
        assert ServerConfig().audit.chain_enabled is False

    def test_enabled_without_file_raises(self) -> None:
        with pytest.raises(ValidationError, match="audit.chain_enabled requires audit.chain_file"):
            AuditConfig(chain_enabled=True)

    def test_enabled_with_file_ok(self, tmp_path: Path) -> None:
        cfg = AuditConfig(chain_enabled=True, chain_file=str(tmp_path / "audit.log"))
        assert cfg.chain_enabled is True
        assert cfg.chain_file == str(tmp_path / "audit.log")

    def test_disabled_with_file_ok(self, tmp_path: Path) -> None:
        # A file may be configured but the chain left disabled — not an error.
        cfg = AuditConfig(chain_enabled=False, chain_file=str(tmp_path / "audit.log"))
        assert cfg.chain_enabled is False


# ---------------------------------------------------------------------------
# 2. audit_event chain hook (best-effort)
# ---------------------------------------------------------------------------


class TestAuditEventChainHook:
    def test_append_and_structured_log(self, tmp_path: Path) -> None:
        chain_file = tmp_path / "audit.log"
        chain = AuditChain(chain_file)
        configure_audit_chain(chain)

        with patch("provide.uterm.server.audit._audit_log") as mock_log:
            audit_event("x.y", principal="p", detail={"a": 1})

        # The structured log emit still fires unchanged.
        mock_log.info.assert_called_once()
        # The record is appended and verifies as a valid chain.
        result = verify_audit_log(chain_file)
        assert result.ok is True
        assert result.count == 1
        record = json.loads(chain_file.read_text(encoding="utf-8").splitlines()[0])
        assert record["action"] == "x.y"
        assert record["principal"] == "p"
        assert record["detail"] == {"a": 1}

    def test_append_failure_does_not_propagate(self, tmp_path: Path) -> None:
        chain = AuditChain(tmp_path / "audit.log")
        configure_audit_chain(chain)

        with (
            patch.object(chain, "append", side_effect=OSError("disk full")),
            patch("provide.uterm.server.audit._audit_log") as mock_log,
        ):
            # Must NOT raise even though the append blew up.
            audit_event("x.fail", principal="p")

        mock_log.info.assert_called_once()
        mock_log.warning.assert_called_once()
        assert "audit_chain_append_failed" in mock_log.warning.call_args[0][0]

    def test_no_append_when_chain_unset(self, tmp_path: Path) -> None:
        chain_file = tmp_path / "audit.log"
        # configure_audit_chain(None) is applied by the autouse fixture.
        with patch("provide.uterm.server.audit._audit_log") as mock_log:
            audit_event("x.none")
        mock_log.info.assert_called_once()
        assert not chain_file.exists()


# ---------------------------------------------------------------------------
# 3. Lifespan wiring
# ---------------------------------------------------------------------------


class TestAuditLifespan:
    async def test_genesis_boot_no_alarm(self, tmp_path: Path) -> None:
        """First-ever boot: no file + no cp_head → chain configured, NO alarm."""
        chain_file = tmp_path / "audit.log"
        app, _config = _make_audit_app(chain_file)

        with patch("provide.uterm.server.app.factory_impl.audit_event") as mock_audit:
            await _run_lifespan_one_tick(app)
            # No integrity alarm emitted on genesis.
            alarms = [c for c in mock_audit.call_args_list if c[0][0] == "audit.chain_integrity_alarm"]
            assert alarms == []

    async def test_appends_and_checkpoint_reflected(self, tmp_path: Path) -> None:
        """audit_event during the lifespan writes records and the head checkpoints."""
        chain_file = tmp_path / "audit.log"
        app, _config = _make_audit_app(chain_file)
        control_plane = app.state.uterm_control_plane

        ran_once: set[int] = set()
        _real_sleep = asyncio.sleep

        async def _patched_sleep(delay: float) -> None:
            task = id(asyncio.current_task())
            if task in ran_once:
                await _real_sleep(3600)
                return
            ran_once.add(task)
            await _real_sleep(0)

        with patch("asyncio.sleep", _patched_sleep):
            async with app.router.lifespan_context(app):
                # Emit a couple of audit events through the configured chain.
                audit_event("session.create", principal="admin")
                audit_event("session.delete", principal="admin")
                for _ in range(20):
                    await _real_sleep(0)
                head = await control_plane.get_audit_head()

        # File has both records and verifies.
        result = verify_audit_log(chain_file)
        assert result.ok is True
        assert result.count == 2
        # The periodic checkpoint reflected the head into the control plane.
        assert head is not None
        assert head[0] == 2

    async def test_truncation_fires_alarm(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A cp_head AHEAD of the on-disk file (rollback) fires the alarm."""
        chain_file = tmp_path / "audit.log"
        # Pre-write a one-record chain.
        chain = AuditChain(chain_file)
        chain.append("seed.event", principal="p")
        seeded_seq, seeded_hash = chain.seq, chain.last_hash

        app, _config = _make_audit_app(chain_file)
        control_plane = app.state.uterm_control_plane
        # Persist a cp_head AHEAD of the file (simulating an end-truncation).
        await control_plane.set_audit_head(seeded_seq + 5, "f" * 64)

        with (
            patch("provide.uterm.server.app.factory_impl.audit_event") as mock_audit,
            caplog.at_level(logging.CRITICAL),
        ):
            await _run_lifespan_one_tick(app)

        alarms = [c for c in mock_audit.call_args_list if c[0][0] == "audit.chain_integrity_alarm"]
        assert len(alarms) == 1
        assert "head mismatch" in (alarms[0][1]["detail"]["reason"] or "")
        # Chain resumes from the file's actual head, not the bogus cp_head.
        assert seeded_seq == 1
        assert seeded_hash != GENESIS_HASH

    async def test_existing_corrupt_file_fires_alarm(self, tmp_path: Path) -> None:
        """An internally-broken file (no cp_head) still fires the alarm."""
        chain_file = tmp_path / "audit.log"
        # Write a structurally-broken line (valid JSON, bad chain).
        chain_file.write_text(
            json.dumps(
                {
                    "seq": 1,
                    "ts": 1.0,
                    "mono_ns": 1,
                    "action": "a",
                    "principal": "",
                    "session_id": "",
                    "source_ip": "",
                    "detail": {},
                    "prev_hash": GENESIS_HASH,
                    "record_hash": "deadbeef",  # wrong hash → record hash mismatch
                }
            )
            + "\n",
            encoding="utf-8",
        )
        app, _config = _make_audit_app(chain_file)

        with patch("provide.uterm.server.app.factory_impl.audit_event") as mock_audit:
            await _run_lifespan_one_tick(app)

        alarms = [c for c in mock_audit.call_args_list if c[0][0] == "audit.chain_integrity_alarm"]
        assert len(alarms) == 1

    async def test_clean_shutdown_flushes_head_and_clears_global(self, tmp_path: Path) -> None:
        """Clean shutdown flushes the final head and resets the module global."""
        chain_file = tmp_path / "audit.log"
        app, _config = _make_audit_app(chain_file)
        control_plane = app.state.uterm_control_plane

        ran_once: set[int] = set()
        _real_sleep = asyncio.sleep

        async def _patched_sleep(delay: float) -> None:
            task = id(asyncio.current_task())
            if task in ran_once:
                await _real_sleep(3600)
                return
            ran_once.add(task)
            await _real_sleep(0)

        with patch("asyncio.sleep", _patched_sleep):
            async with app.router.lifespan_context(app):
                audit_event("session.create", principal="admin")
                for _ in range(20):
                    await _real_sleep(0)

        # After teardown the global is reset to None (no append now).
        with patch("provide.uterm.server.audit._audit_log"):
            audit_event("after.shutdown")
        result = verify_audit_log(chain_file)
        # Only the one in-lifespan record; the post-shutdown event did NOT append.
        assert result.count == 1
        # Final head flushed on shutdown.
        head = await control_plane.get_audit_head()
        assert head is not None and head[0] == 1

    @pytest.mark.asyncio
    async def test_checkpoint_error_is_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A set_audit_head that raises in the checkpoint loop is caught by the
        task's ``except Exception`` branch and does not crash the lifespan."""
        chain_file = tmp_path / "audit.log"
        app = _make_audit_app_api_only(chain_file)
        _patch_fast_sleep(monkeypatch)

        control_plane = app.state.uterm_control_plane
        cp_type = type(control_plane)
        real_set = cp_type.set_audit_head
        called = asyncio.Event()
        calls = {"n": 0}

        async def _boom_set(self: Any, seq: int, record_hash: str) -> None:
            # Let the startup re-checkpoint (first call) succeed, then blow up on
            # every periodic checkpoint call so only the loop body's except fires.
            calls["n"] += 1
            if calls["n"] == 1:
                await real_set(self, seq, record_hash)
                return
            called.set()
            raise RuntimeError("boom")

        monkeypatch.setattr(cp_type, "set_audit_head", _boom_set)

        with TestClient(app):
            await asyncio.wait_for(called.wait(), timeout=5.0)
        # No exception escaping means the except path absorbed the error.
        assert called.is_set()

    @pytest.mark.asyncio
    async def test_checkpoint_cancellation_is_reraised(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the lifespan cancels the checkpoint task while set_audit_head is
        awaiting, the CancelledError must propagate (not be swallowed) so the task
        actually stops — drives the ``except asyncio.CancelledError: raise`` branch."""
        chain_file = tmp_path / "audit.log"
        app = _make_audit_app_api_only(chain_file)
        _patch_fast_sleep(monkeypatch)

        control_plane = app.state.uterm_control_plane
        cp_type = type(control_plane)
        real_set = cp_type.set_audit_head
        inside = asyncio.Event()
        calls = {"n": 0}

        async def _block_set(self: Any, seq: int, record_hash: str) -> None:
            # Let the startup re-checkpoint (first call) through and the shutdown
            # flush (any call after we've parked) through; park only the first
            # periodic loop call so teardown's cancel lands on an awaiting
            # set_audit_head inside the task's try block.
            calls["n"] += 1
            if calls["n"] == 1 or inside.is_set():
                await real_set(self, seq, record_hash)
                return
            inside.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(cp_type, "set_audit_head", _block_set)

        with TestClient(app):
            # Ensure the task is parked inside set_audit_head before teardown.
            await asyncio.wait_for(inside.wait(), timeout=5.0)
        # Reaching here means teardown completed cleanly: the CancelledError was
        # re-raised and awaited without escaping.
        assert inside.is_set()


# ---------------------------------------------------------------------------
# 4. Posture
# ---------------------------------------------------------------------------


class TestAuditPosture:
    def test_audit_chain_enabled_field_present(self, tmp_path: Path) -> None:
        config = ServerConfig(audit=AuditConfig(chain_enabled=True, chain_file=str(tmp_path / "a.log")))
        posture = compute_security_posture(config)
        assert posture["audit_chain_enabled"] is True

    def test_disabled_emits_compliance_warning(self) -> None:
        config = ServerConfig()
        posture = compute_security_posture(config)
        assert posture["audit_chain_enabled"] is False
        assert any("tamper-evident" in w for w in posture["warnings"])

    def test_enabled_no_compliance_warning(self, tmp_path: Path) -> None:
        config = ServerConfig(audit=AuditConfig(chain_enabled=True, chain_file=str(tmp_path / "a.log")))
        posture = compute_security_posture(config)
        assert not any("tamper-evident" in w for w in posture["warnings"])

    def test_posture_json_serializable(self, tmp_path: Path) -> None:
        config = ServerConfig(audit=AuditConfig(chain_enabled=True, chain_file=str(tmp_path / "a.log")))
        posture = compute_security_posture(config)
        assert json.loads(json.dumps(posture)) == posture

    def test_posture_defensive_when_audit_attr_missing(self) -> None:
        # Defensive getattr path: a config object lacking ``audit`` reports False.
        class _NoAudit:
            auth = AuthConfig(mode="dev_token")
            security = ServerConfig().security
            server = ServerConfig().server
            environment = "production"

        posture = compute_security_posture(_NoAudit())  # type: ignore[arg-type]
        assert posture["audit_chain_enabled"] is False
