#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for what the factory wires into the hub and its collaborators.

``create_server_app`` is mostly assembly: it reads configuration and hands it to
the hub, the fan-out controller, the webhook manager and the registry. Assembly
is exactly the kind of code a smoke test cannot check -- an app that was built
with every limit set to ``None`` still starts, still serves, and still passes
any test that only asks whether it came up.

Three of these carry real decisions rather than plumbing:

*The loopback webhook permission.* ``allow_loopback_destinations`` is an opt-in
OR'd with "we are bound to loopback". An out-of-the-box server listened only on
127.0.0.1 and simultaneously refused loopback webhook destinations, so the
refusal protected nothing and only cost UX. On a routable bind the calculus is
the opposite -- a loopback destination there is a genuine SSRF pivot -- so the
key is still required. Both operands, and both binds, are pinned here.

*The resume guard.* ``_on_resume`` refuses a token whose session has been
deleted and recreated under the same id. Without the recreate check a stale
token reattaches to somebody else's session.

*The API-only gate.* Two independent ways to say "do not mount the UI" -- the
argument and the environment. Either alone must suppress the mount, or a
headless deployment fails to start because a directory it never wanted is
missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.app import factory_impl

#: Test-only value for the authz webhook's shared signing input.
_WEBHOOK_SHARED_VALUE = "uterm-test-authz-webhook-value-32b"


def _app(config: Any = None, **kwargs: Any) -> Any:
    return create_server_app(config if config is not None else default_server_config(), api_only=True, **kwargs)


def _hub(app: Any) -> Any:
    return app.state.uterm_hub


def _make_routable(config: Any) -> None:
    """Move the server off loopback — which dev_token mode does not permit."""
    config.server.host = "10.1.2.3"
    config.auth.mode = "jwt"
    config.auth.jwt_public_key_pem = "uterm-test-hs256-secret-32-byte-minimum"
    config.auth.jwt_algorithms = ["HS256"]
    config.auth.worker_bearer_token = "uterm-test-worker-bearer-value-32-bytes"


# ---------------------------------------------------------------------------
# The metric accumulator
# ---------------------------------------------------------------------------


def test_a_metric_accumulates_rather_than_overwriting() -> None:
    """Counters are read as totals; overwriting turns every count into "1"."""
    app = _app()
    inc = _hub(app)._on_metric

    inc("widgets")
    inc("widgets")

    assert app.state.uterm_metrics["widgets"] == 2


def test_a_metric_defaults_to_counting_one_and_takes_an_explicit_step() -> None:
    app = _app()
    inc = _hub(app)._on_metric

    inc("widgets")
    inc("widgets", 5)

    assert app.state.uterm_metrics["widgets"] == 6


# ---------------------------------------------------------------------------
# The resume guard
# ---------------------------------------------------------------------------


def _resume_session(*, worker_id: str = "w1", wall_created_at: float) -> Any:
    session = MagicMock()
    session.worker_id = worker_id
    session.wall_created_at = wall_created_at
    return session


def _definition(*, created_at: float) -> Any:
    definition = MagicMock()
    definition.created_at = datetime.fromtimestamp(created_at, tz=UTC)
    return definition


async def test_a_resume_for_a_session_that_is_gone_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing to reattach to; allowing it resumes into a session that does not exist."""
    app = _app()
    monkeypatch.setattr(app.state.uterm_registry, "get_definition", AsyncMock(return_value=None))

    assert await _hub(app)._on_resume("tok", _resume_session(wall_created_at=100.0)) is False


async def test_a_resume_for_the_same_session_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The near side, so "always refuse" cannot pass."""
    app = _app()
    monkeypatch.setattr(
        app.state.uterm_registry, "get_definition", AsyncMock(return_value=_definition(created_at=100.0))
    )

    assert await _hub(app)._on_resume("tok", _resume_session(wall_created_at=100.0)) is True


async def test_a_resume_into_a_recreated_session_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete-and-recreate under the same id is a different session.

    The token predates the current definition, so honouring it would reattach a
    stale client to somebody else's terminal.
    """
    app = _app()
    monkeypatch.setattr(
        app.state.uterm_registry, "get_definition", AsyncMock(return_value=_definition(created_at=200.0))
    )

    assert await _hub(app)._on_resume("tok", _resume_session(wall_created_at=100.0)) is False


async def test_a_token_with_no_creation_stamp_is_not_judged_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wall_created_at > 0`` guards the comparison; an unstamped token predates nothing."""
    app = _app()
    monkeypatch.setattr(
        app.state.uterm_registry, "get_definition", AsyncMock(return_value=_definition(created_at=200.0))
    )

    assert await _hub(app)._on_resume("tok", _resume_session(wall_created_at=0.0)) is True


async def test_the_definition_looked_up_is_the_tokens_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_definition = AsyncMock(return_value=None)
    app = _app()
    monkeypatch.setattr(app.state.uterm_registry, "get_definition", get_definition)

    await _hub(app)._on_resume("tok", _resume_session(worker_id="other", wall_created_at=1.0))

    get_definition.assert_awaited_once_with("other")


# ---------------------------------------------------------------------------
# The loopback webhook permission
# ---------------------------------------------------------------------------


def test_a_loopback_bind_permits_loopback_webhook_destinations() -> None:
    """The bind-derived half: refusing here protected nothing and only cost UX."""
    config = default_server_config()
    config.server.host = "127.0.0.1"
    config.webhooks.allow_loopback_destinations = False

    assert _app(config).state.uterm_webhooks._allow_loopback_destinations is True


def test_a_routable_bind_still_requires_the_explicit_opt_in() -> None:
    """On a shared host a loopback destination is a genuine SSRF pivot."""
    config = default_server_config()
    _make_routable(config)
    config.webhooks.allow_loopback_destinations = False

    assert _app(config).state.uterm_webhooks._allow_loopback_destinations is False


def test_the_explicit_opt_in_permits_them_on_a_routable_bind() -> None:
    """The config half of the ``or``, so the bind alone cannot be the whole answer."""
    config = default_server_config()
    _make_routable(config)
    config.webhooks.allow_loopback_destinations = True

    assert _app(config).state.uterm_webhooks._allow_loopback_destinations is True


def test_the_webhook_manager_shares_the_live_tunnel_token_store() -> None:
    """A COPY would answer "is this session shared right now?" with a stale yes/no."""
    app = _app()

    assert app.state.uterm_webhooks._tunnel_tokens is app.state.uterm_tunnel_tokens


# ---------------------------------------------------------------------------
# The hub's limits and collaborators
# ---------------------------------------------------------------------------


def test_the_hub_is_given_this_servers_limits() -> None:
    """Each of these is a resource bound; ``None`` silently removes it."""
    config = default_server_config()
    config.max_workers = 7
    config.max_connections_per_principal = 3
    config.browser_rate_limit_per_sec = 11
    config.rest_acquire_rate_limit_per_sec = 13
    config.rest_send_rate_limit_per_sec = 17
    hub = _hub(_app(config))

    assert hub.max_workers == 7
    assert hub.max_connections_per_principal == 3
    assert hub.browser_rate_limit_per_sec == 11
    assert hub.limiter._rest_acquire_rate == 13
    assert hub.limiter._rest_send_rate == 17


def test_the_hub_is_given_the_worker_token_and_the_role_resolver() -> None:
    """The hub authenticates workers with this token and scopes browsers with this resolver."""
    config = default_server_config()
    config.auth.worker_bearer_token = "uterm-test-worker-bearer-value-32-bytes"
    hub = _hub(_app(config))

    assert hub._worker_token == "uterm-test-worker-bearer-value-32-bytes"
    assert callable(hub._resolve_browser_role)


def test_role_delegation_follows_the_configured_value() -> None:
    """Read through ``getattr`` with a True default — the value must still win."""
    config = default_server_config()
    config.auth.delegate_roles = False

    assert _hub(_app(config))._delegate_roles is False


def test_role_delegation_defaults_to_on_when_the_key_is_absent() -> None:
    """The ``getattr`` default is the behaviour for a config that predates the key."""
    config = default_server_config()
    del config.auth.delegate_roles

    assert _hub(_app(config))._delegate_roles is True


# ---------------------------------------------------------------------------
# The fan-out controller
# ---------------------------------------------------------------------------


async def test_the_fanout_controller_authorizes_through_this_servers_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three callbacks, each answering a different question about the caller.

    Crossed or dropped, the controller either refuses everyone or asks the wrong
    service whether a member may read a session.
    """
    app = _app()
    controller = _hub(app).fan_out_controller
    session = MagicMock()
    monkeypatch.setattr(app.state.uterm_registry, "get_definition", AsyncMock(return_value=session))
    monkeypatch.setattr(app.state.uterm_authz, "_provider", MagicMock())

    assert await controller._resolve_session("w1") is session
    assert callable(controller._is_global_admin)
    assert callable(controller._can_read_session)


def test_the_fanout_controller_follows_the_configured_membership_policy() -> None:
    """Whether an unknown member may join a fan-out group is a configured decision."""
    config = default_server_config()
    config.fanout_allow_unknown_members = True

    assert _hub(_app(config)).fan_out_controller.allow_unknown_members is True


# ---------------------------------------------------------------------------
# The API-only gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_the_environment_can_suppress_the_frontend_mount(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Three accepted spellings, and a headless deployment relies on them.

    Built WITHOUT ``api_only`` so the environment is the only thing suppressing
    the mount -- otherwise the argument would satisfy the gate on its own.
    """
    monkeypatch.setenv("UTERM_API_ONLY", value)
    mount = MagicMock()
    monkeypatch.setattr(factory_impl, "mount_frontend_assets", mount)

    create_server_app(default_server_config())

    mount.assert_not_called()


def test_an_unset_environment_leaves_the_mount_to_the_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side, so "never mount" cannot pass."""
    monkeypatch.delenv("UTERM_API_ONLY", raising=False)
    mount = MagicMock()
    monkeypatch.setattr(factory_impl, "mount_frontend_assets", mount)

    create_server_app(default_server_config())

    mount.assert_called_once()


def test_the_argument_alone_suppresses_the_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UTERM_API_ONLY", raising=False)
    mount = MagicMock()
    monkeypatch.setattr(factory_impl, "mount_frontend_assets", mount)

    create_server_app(default_server_config(), api_only=True)

    mount.assert_not_called()


def test_an_unrecognised_environment_value_does_not_suppress_the_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The membership test is a fixed set — an arbitrary value is not a yes."""
    monkeypatch.setenv("UTERM_API_ONLY", "maybe")
    mount = MagicMock()
    monkeypatch.setattr(factory_impl, "mount_frontend_assets", mount)

    create_server_app(default_server_config())

    mount.assert_called_once()


# ---------------------------------------------------------------------------
# What the app publishes about itself
# ---------------------------------------------------------------------------


def test_the_app_starts_not_ready() -> None:
    """/readyz gates on this, so a half-initialized pod must not pass a probe."""
    assert _app().state.uterm_ready is False


def test_the_durability_capabilities_endpoint_serves_the_computed_capabilities() -> None:
    from starlette.testclient import TestClient

    app = _app()
    with TestClient(app) as client:
        response = client.get("/api/durability/capabilities")

    assert response.status_code == 200
    assert response.json() == app.state.uterm_durability_capabilities


# ---------------------------------------------------------------------------
# The rest of the hub's collaborators
# ---------------------------------------------------------------------------


def test_the_hub_is_given_the_governance_gates_and_the_identity_provider() -> None:
    """These are the objects every policy decision is delegated to.

    Passed positionally alongside each other, so a crossed or dropped one sends
    the question to the wrong service — or to none, which fails open.
    """
    app = _app()
    hub = _hub(app)

    assert hub._identity_provider is app.state.uterm_idp
    assert hub._behavioral_audit_gate is not None
    assert hub._behavioral_thresholds is not None


def test_the_hub_gets_a_resume_store_backed_by_the_control_plane() -> None:
    """Resume tokens live in the control plane; without this they are lost on
    every reconnect."""
    app = _app()

    assert _hub(app).resume_store is not None


def test_the_behavioral_audit_interval_follows_the_configuration() -> None:
    config = default_server_config()
    config.governance.behavioral_audit_interval_s = 42.0

    assert _hub(_app(config))._behavioral_audit_interval_s == 42.0


def test_the_hub_follows_the_configured_worker_frame_policy() -> None:
    """What the hub does with a worker frame it cannot parse is a configured decision."""
    config = default_server_config()
    config.worker_frame_on_invalid = "reject"

    assert _hub(_app(config)).worker_frame_on_invalid == "reject"


def test_the_hub_follows_the_configured_stale_owner_resume_policy() -> None:
    config = default_server_config()
    config.allow_stale_owner_role_resume = True

    assert _hub(_app(config)).allow_stale_owner_role_resume is True


def test_the_event_bus_reports_through_this_apps_metrics() -> None:
    """A bus wired to no counter drops its queue statistics silently."""
    app = _app()

    assert _hub(app)._event_bus is not None
    _hub(app)._event_bus._on_metric("bus_widgets")
    assert app.state.uterm_metrics["bus_widgets"] == 1


def test_both_api_only_signals_together_still_suppress_the_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``and`` over two negatives: neither signal alone may re-enable the mount."""
    monkeypatch.setenv("UTERM_API_ONLY", "1")
    mount = MagicMock()
    monkeypatch.setattr(factory_impl, "mount_frontend_assets", mount)

    create_server_app(default_server_config(), api_only=True)

    mount.assert_not_called()


# ---------------------------------------------------------------------------
# The session registry's own wiring
# ---------------------------------------------------------------------------


def test_the_registry_is_given_this_servers_session_settings(tmp_path: Any) -> None:
    """Every one of these is read per session; a default silently replaces a decision.

    The values are deliberately non-default -- a kwarg that is dropped falls
    back to the constructor's own default, which is indistinguishable from
    correct whenever the test config happens to use that default.
    """
    config = default_server_config()
    config.server.max_sessions = 9
    config.server.public_base_url = "https://uterm.example/base"
    config.security.block_private_connector_targets = True
    config.auth.worker_bearer_token = "uterm-test-worker-bearer-value-32-bytes"
    app = _app(config)
    registry = app.state.uterm_registry

    assert registry._max_sessions == 9
    assert registry._public_base_url == "https://uterm.example/base"
    assert registry._block_private is True
    assert registry._worker_bearer_token == "uterm-test-worker-bearer-value-32-bytes"


def test_the_registry_shares_the_live_tunnel_token_store() -> None:
    """Same store the webhook manager reads, so a share is visible to both."""
    app = _app()

    assert app.state.uterm_registry._tunnel_tokens is app.state.uterm_tunnel_tokens


def test_the_registry_is_given_an_annotation_detector() -> None:
    """Without it, snapshot/send text is never scanned for security patterns."""
    assert _app().state.uterm_registry._detector is not None


def test_the_registry_is_given_the_configured_recording_store() -> None:
    """Built from config once and shared; a second store writes somewhere else."""
    app = _app()

    assert app.state.uterm_registry._recording_store is not None


def test_the_profile_store_is_rooted_at_the_configured_directory(tmp_path: Any) -> None:
    """Profiles are read from here; a default directory silently loses them."""
    config = default_server_config()
    config.profiles.directory = tmp_path / "profiles"

    assert _app(config).state.uterm_profile_store._directory == tmp_path / "profiles"


# ---------------------------------------------------------------------------
# Optional extras
# ---------------------------------------------------------------------------


def test_a_missing_annotation_extra_is_reported_as_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare ImportError names a module nobody recognises as an extra.

    ``annotation`` ships in a separate distribution, so the message has to say
    which one and how to install it -- that string is the entire remedy.
    """
    import sys

    monkeypatch.setitem(sys.modules, "provide.uterm.annotation", None)

    with pytest.raises(RuntimeError) as failure:
        _app()

    assert str(failure.value) == ("annotation support not installed; pip install 'provide-uterm-server[annotation]'")


def test_a_configured_authorization_webhook_becomes_the_provider() -> None:
    """Configuring a URL swaps the local provider for a webhook one, with that URL.

    Dropping the provider entirely leaves the service holding ``None``, which
    still constructs and still serves -- every authorization call then fails on
    a missing provider at request time rather than at startup.
    """
    config = default_server_config()
    config.governance.authz_webhook_url = "https://authz.example/decide"
    config.governance.authz_webhook_secret = _WEBHOOK_SHARED_VALUE
    config.governance.authz_webhook_timeout_s = 7.0

    provider = _app(config).state.uterm_authz._provider

    assert provider is not None
    assert provider.url == "https://authz.example/decide"
    assert provider.secret == _WEBHOOK_SHARED_VALUE
    assert provider.timeout == 7.0


def test_no_configured_webhook_leaves_the_local_provider_in_place() -> None:
    """The other arm, so "always webhook" cannot pass."""
    provider = _app().state.uterm_authz._provider

    assert provider is not None
    assert not hasattr(provider, "url")
