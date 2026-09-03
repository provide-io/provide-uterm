#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the reference server's lifespan.

Startup and shutdown are almost entirely side effects, so an app that leaks
every background task and closes none of its HTTP clients still starts, still
serves, and still passes any test that only asks whether requests work.

Four things here are load-bearing:

*The readiness flag.* ``/readyz`` and ``/api/health`` gate on it, so a
half-initialized pod must not pass a Kubernetes probe. It is set only after
``migrate()`` and task creation, and cleared again on the way out so a draining
pod stops receiving traffic.

*The audit chain.* Opt-in, and enabled only when a chain file is configured as
well. On clean shutdown the latest head is flushed so the persisted
anti-rollback anchor reflects every record written this run, and the module
global is reset so a re-created app in the same process does not inherit a
stale chain.

*The auto-start failure log.* The boot task is fire-and-forget; its done
callback is the only place a failure to start configured sessions is ever
reported. A cancelled task is not a failure and must not be reported as one.

*The teardown.* Every pooled HTTP client and every background task has exactly
one place it is released. ``_aclose_webhook_gates`` lives at module level
specifically so its two branches are directly testable -- coverage.py on 3.11
mis-tracks the async-generator resume it would otherwise sit in.
"""

from __future__ import annotations

import sys
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.app import factory_impl


def _app(config: Any = None) -> Any:
    return create_server_app(config if config is not None else default_server_config(), api_only=True)


def _patch_method(monkeypatch: pytest.MonkeyPatch, obj: Any, name: str, replacement: Any) -> Any:
    """Patch a method on *obj*, falling back to its class.

    Several collaborators here use ``__slots__``, so their bound methods are
    read-only on the instance.
    """
    try:
        monkeypatch.setattr(obj, name, replacement)
    except AttributeError:
        monkeypatch.setattr(type(obj), name, lambda _self, *a, **k: replacement(*a, **k))
    return replacement


# ---------------------------------------------------------------------------
# _aclose_webhook_gates — the branchy close loop, extracted to be testable
# ---------------------------------------------------------------------------


async def test_every_configured_gate_has_its_client_released() -> None:
    """Each gate holds a pooled HTTP client; skipping one leaks it per app."""
    first, second = AsyncMock(), AsyncMock()

    await factory_impl._aclose_webhook_gates(first, second)

    first.aclose.assert_awaited_once()
    second.aclose.assert_awaited_once()


async def test_an_unconfigured_gate_is_skipped_rather_than_dereferenced() -> None:
    """Most deployments configure none of these, so ``None`` is the common case."""
    configured = AsyncMock()

    await factory_impl._aclose_webhook_gates(None, configured, None)

    configured.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_the_app_is_ready_only_while_it_is_serving() -> None:
    """False before startup, True while up, False again once draining."""
    app = _app()

    assert app.state.uterm_ready is False
    with TestClient(app):
        assert app.state.uterm_ready is True
    assert app.state.uterm_ready is False


def test_readiness_follows_the_control_plane_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    """``migrate()`` runs first; a failure there must leave the pod unready.

    Not "ready then migrate" -- that order passes a readiness probe against a
    database that has not been brought up to schema.
    """
    app = _app()
    plane = app.state.uterm_control_plane
    seen: list[bool] = []
    original = type(plane).migrate

    async def _record(self: Any) -> Any:
        seen.append(app.state.uterm_ready)
        return await original(self)

    monkeypatch.setattr(type(plane), "migrate", _record)

    with TestClient(app):
        pass

    assert seen == [False], "the app was already advertising readiness during migrate()"


# ---------------------------------------------------------------------------
# The audit chain
# ---------------------------------------------------------------------------


def _audit_config(*, enabled: bool, chain_file: str | None) -> Any:
    config = default_server_config()
    config.audit.chain_enabled = enabled
    config.audit.chain_file = chain_file
    return config


@pytest.mark.parametrize(
    ("enabled", "chain_file"),
    [(False, "/tmp/chain.jsonl"), (True, None), (False, None)],
)
def test_the_audit_chain_stays_off_unless_both_settings_say_so(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, chain_file: str | None
) -> None:
    """Both operands: enabling without a file has nowhere to write."""
    resume = AsyncMock()
    monkeypatch.setattr(factory_impl, "resume_audit_chain", resume)

    with TestClient(_app(_audit_config(enabled=enabled, chain_file=chain_file))):
        pass

    resume.assert_not_awaited()


def test_an_enabled_audit_chain_is_resumed_and_checkpointed(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Resumed with this server's own audit hook, so a test-time patch is observed."""
    chain = MagicMock()
    chain.seq, chain.last_hash = 42, "deadbeef"
    resume = AsyncMock(return_value=chain)
    monkeypatch.setattr(factory_impl, "resume_audit_chain", resume)
    monkeypatch.setattr(factory_impl, "checkpoint_audit_head", AsyncMock())
    config = _audit_config(enabled=True, chain_file=str(tmp_path / "chain.jsonl"))

    with TestClient(_app(config)):
        pass

    resume.assert_awaited_once()
    assert resume.await_args.kwargs["audit_event"] is factory_impl.audit_event


def test_a_clean_shutdown_flushes_the_audit_head_and_resets_the_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The persisted anchor must reflect every record written this run.

    Resetting the module global matters because a second app in the same
    process would otherwise inherit this run's chain.
    """
    chain = MagicMock()
    chain.seq, chain.last_hash = 42, "deadbeef"
    monkeypatch.setattr(factory_impl, "resume_audit_chain", AsyncMock(return_value=chain))
    monkeypatch.setattr(factory_impl, "checkpoint_audit_head", AsyncMock())
    configure = MagicMock()
    monkeypatch.setattr(factory_impl, "configure_audit_chain", configure)
    app = _app(_audit_config(enabled=True, chain_file=str(tmp_path / "chain.jsonl")))
    set_head = AsyncMock()
    _patch_method(monkeypatch, app.state.uterm_control_plane, "set_audit_head", set_head)

    with TestClient(app):
        pass

    set_head.assert_awaited_once_with(42, "deadbeef")
    configure.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# Auto-start
# ---------------------------------------------------------------------------


def test_configured_sessions_are_started_after_startup_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred so FastAPI finishes route/middleware init before sessions connect."""
    app = _app()
    ran = threading.Event()

    async def _start() -> None:
        ran.set()

    monkeypatch.setattr(app.state.uterm_registry, "start_auto_start_sessions", _start)
    monkeypatch.setattr(factory_impl, "_AUTO_START_DELAY_S", 0.0)

    with TestClient(app):
        # Waited on rather than assumed: the boot task runs on the server's own
        # loop, so a fixed number of requests is a race, not a synchronisation.
        assert ran.wait(timeout=5.0), "configured sessions were never started"


def test_a_failure_to_start_configured_sessions_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fire-and-forget: this callback is the only report while the server is up.

    The failure surfaces a second time at shutdown, because ``cancel_and_drain``
    awaits the same task and an already-failed task re-raises. That is existing
    behaviour and is asserted here rather than worked around -- but it happens
    only once the process is already going down, which is why this callback is
    what a *running* server has.
    """
    app = _app()
    failure = RuntimeError("no such connector")
    monkeypatch.setattr(app.state.uterm_registry, "start_auto_start_sessions", AsyncMock(side_effect=failure))
    monkeypatch.setattr(factory_impl, "_AUTO_START_DELAY_S", 0.0)
    recorder = MagicMock()
    monkeypatch.setattr(factory_impl, "logger", recorder)

    with pytest.raises(RuntimeError, match="no such connector"), TestClient(app) as client:
        client.get("/api/durability/capabilities")

    recorder.error.assert_called_once_with("auto_start_sessions_failed error=%s", failure)


def test_a_shutdown_before_the_sessions_start_is_not_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled boot task is an ordinary shutdown, not an error to log."""
    app = _app()
    monkeypatch.setattr(factory_impl, "_AUTO_START_DELAY_S", 30.0)
    recorder = MagicMock()
    monkeypatch.setattr(factory_impl, "logger", recorder)

    with TestClient(app):
        pass

    recorder.error.assert_not_called()


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def test_every_collaborator_is_shut_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each of these holds tasks, sockets or a pooled HTTP client.

    Asserted together because they are released in one block: dropping any
    single call leaks that resource for the life of the process, and no request
    ever notices.
    """
    app = _app()
    hub_shutdown = AsyncMock()
    webhooks_shutdown = AsyncMock()
    authz_aclose = AsyncMock()
    registry_shutdown = AsyncMock()
    plane_close = AsyncMock()
    _patch_method(monkeypatch, app.state.uterm_hub, "shutdown", hub_shutdown)
    _patch_method(monkeypatch, app.state.uterm_webhooks, "shutdown", webhooks_shutdown)
    _patch_method(monkeypatch, app.state.uterm_authz, "aclose", authz_aclose)
    _patch_method(monkeypatch, app.state.uterm_registry, "shutdown", registry_shutdown)
    _patch_method(monkeypatch, app.state.uterm_control_plane, "close", plane_close)

    with TestClient(app):
        pass

    hub_shutdown.assert_awaited_once()
    webhooks_shutdown.assert_awaited_once()
    authz_aclose.assert_awaited_once()
    registry_shutdown.assert_awaited_once()
    plane_close.assert_awaited_once()


def test_the_governance_gates_are_released_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The four gates are passed positionally; a dropped one leaks its client."""
    seen: list[tuple[Any, ...]] = []

    async def _record(*gates: Any) -> None:
        seen.append(gates)

    monkeypatch.setattr(factory_impl, "_aclose_webhook_gates", _record)

    with TestClient(_app()):
        pass

    assert len(seen) == 1
    assert len(seen[0]) == 4, "policy, fanout policy, behavioral audit and telemetry gates"


def test_a_missing_pam_integration_is_not_a_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PAM is an optional extra; its absence must not stop the server booting."""
    # A ``None`` entry in sys.modules makes importing that one name raise
    # ImportError. Replacing ``builtins.__import__`` would do it too, but it
    # changes every import the app makes, which is its own source of failures.
    monkeypatch.setitem(sys.modules, "provide.uterm.server.pam_integration", None)
    app = _app()

    with TestClient(app):
        assert app.state.uterm_ready is True
