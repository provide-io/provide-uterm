#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the factory's component construction and publication.

Everything here is assembly, and assembly is what a smoke test cannot check: an
app built with every collaborator set to ``None`` still constructs, still
serves, and still passes any test that only asks whether it came up. These are
the pieces that had no assertion at all.

Three of them carry more than plumbing:

*The governance gates.* Configured, they are the objects every policy,
behavioral-audit and telemetry decision is delegated to. Unwired, the hub
silently falls back to permissive defaults -- the deployment believes it is
governed and is not.

*The security-posture line.* One structured startup record of the effective
posture, so an operator can see at a glance whether a config copied between
environments is still hardened. It is emitted once and never again; a dropped
field is a question nobody can answer later.

*The graphical-target registry and its VNC dial factory.* Seeded from config at
startup so an invalid target fails the boot rather than the first request.

Tests that configure a webhook stub ``_http.async_client``. That is
load-bearing, not tidiness: building a real client runs proxy discovery, which
on macOS calls into SystemConfiguration and aborts a process that has
``fork()``ed without ``exec`` -- exactly how mutmut runs each mutant. See
``docs/mutmut-survivors-triage.md``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server import _http, create_server_app, default_server_config
from provide.uterm.server import app as server_app_pkg
from provide.uterm.server.app import factory_impl

_WEBHOOK_VALUE = "uterm-test-governance-webhook-32b"


@pytest.fixture()
def stub_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real HTTP clients: their proxy discovery aborts a forked child."""
    monkeypatch.setattr(_http, "async_client", lambda **_kwargs: MagicMock())


def _app(config: Any = None) -> Any:
    return create_server_app(config if config is not None else default_server_config(), api_only=True)


def _governed_config() -> Any:
    config = default_server_config()
    config.governance.policy_webhook_url = "https://policy.example/decide"
    config.governance.behavioral_audit_url = "https://audit.example/report"
    config.governance.telemetry_webhook_url = "https://telemetry.example/ingest"
    return config


# ---------------------------------------------------------------------------
# The security-posture line
# ---------------------------------------------------------------------------


def test_the_startup_posture_line_reports_every_field_it_promises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five fields, positionally — a dropped one leaves a gap nobody can fill later.

    Asserted as one exact call against the logger, because this is the only
    place the effective posture is ever stated.
    """
    recorder = MagicMock()
    monkeypatch.setattr(factory_impl, "logger", recorder)
    config = default_server_config()

    _app(config)

    posture = factory_impl.compute_security_posture(config)
    recorder.info.assert_any_call(
        "security_posture environment=%s bind=%s auth_mode=%s dev_opt_outs=%s secure=%s",
        posture["environment"],
        posture["bind_host"],
        posture["auth_mode"],
        posture["dev_opt_outs"],
        posture["secure"],
    )


# ---------------------------------------------------------------------------
# The frontend-asset presence check — the other api_only gate
# ---------------------------------------------------------------------------


def test_a_ui_serving_app_checks_its_assets_are_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails the boot rather than serving a 404 for every page."""
    monkeypatch.delenv("UTERM_API_ONLY", raising=False)
    validate = MagicMock()
    monkeypatch.setattr(server_app_pkg, "_validate_frontend_assets", validate)

    create_server_app(default_server_config())

    validate.assert_called_once()


@pytest.mark.parametrize(
    ("api_only", "env"),
    [(True, None), (False, "1"), (True, "1")],
)
def test_an_api_only_app_does_not_require_the_ui_to_exist(
    monkeypatch: pytest.MonkeyPatch, api_only: bool, env: str | None
) -> None:
    """Either signal suppresses the check, and both together still do.

    A headless deployment must not fail to start because a UI directory it
    never wanted is missing.
    """
    if env is None:
        monkeypatch.delenv("UTERM_API_ONLY", raising=False)
    else:
        monkeypatch.setenv("UTERM_API_ONLY", env)
    validate = MagicMock()
    monkeypatch.setattr(server_app_pkg, "_validate_frontend_assets", validate)

    create_server_app(default_server_config(), api_only=api_only)

    validate.assert_not_called()


# ---------------------------------------------------------------------------
# Identity, authorization and policy
# ---------------------------------------------------------------------------


def test_the_identity_provider_is_built_against_this_apps_key_store() -> None:
    """API-key auth resolves through this store; a different one knows no keys."""
    app = _app()

    assert app.state.uterm_api_key_store is not None
    assert app.state.uterm_idp is not None


def test_the_policy_resolver_is_built_from_this_auth_config_and_authz() -> None:
    """Both arguments: the policy decides roles from auth config, via this service."""
    config = default_server_config()
    app = _app(config)
    policy = app.state.uterm_policy

    assert policy.auth is config.auth
    assert app.state.uterm_authz is not None


async def test_the_fanout_admin_check_is_asked_about_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_admin(None)`` answers a question about nobody."""
    app = _app()
    seen: list[Any] = []
    controller = app.state.uterm_hub.fan_out_controller

    async def _record(principal: Any) -> bool:
        seen.append(principal)
        return True

    monkeypatch.setattr(type(app.state.uterm_authz), "is_admin", lambda _self, p: _record(p))
    principal = object()

    assert await controller._is_global_admin(principal) is True
    assert seen == [principal]


def test_the_hub_route_authz_resolves_the_registry_lazily() -> None:
    """A late-bound getter: the registry does not exist yet when this is built.

    Binding ``None`` — or a getter that returns it — makes every hub route
    authorize against no session registry at all.
    """
    app = _app()

    assert app.state.uterm_registry is not None


# ---------------------------------------------------------------------------
# The governance gates
# ---------------------------------------------------------------------------


async def test_configured_governance_gates_reach_the_hub(stub_http: None) -> None:
    """Unwired, the hub falls back to permissive defaults and nothing says so.

    Async because a configured behavioral-audit gate makes the hub start its
    audit loop at construction, which needs a running event loop.
    """
    hub = _app(_governed_config()).state.uterm_hub

    # ``policy_gate`` is the INPUT interception gate, distinct from
    # ``output_policy_gate`` (redaction); the factory wires the configured
    # webhook to the former, and an unwired one silently becomes a no-op.
    assert type(hub._policy_gate).__name__ == "WebhookPolicyGate"
    assert hub._telemetry_sink is not None
    assert type(hub._behavioral_audit_gate).__name__ == "WebhookBehavioralAuditGate"


async def test_the_configured_fanout_policy_gate_reaches_the_controller(stub_http: None) -> None:
    """A separate gate from the output policy, and it has its own destination."""
    controller = _app(_governed_config()).state.uterm_hub.fan_out_controller

    assert controller._fanout_policy_gate is not None


def test_an_ungoverned_deployment_gets_no_gates() -> None:
    """The other side: gates are opt-in, so a plain config must wire none."""
    hub = _app().state.uterm_hub

    assert type(hub._policy_gate).__name__ == "NoOpPolicyGate"
    assert hub._telemetry_sink is None
    assert hub.fan_out_controller._fanout_policy_gate is None


def test_the_behavioral_thresholds_are_always_provided() -> None:
    """Not optional — the audit loop reads them whether or not a gate exists."""
    assert _app().state.uterm_hub._behavioral_thresholds is not None


# ---------------------------------------------------------------------------
# The hub, the fan-out controller and the buses
# ---------------------------------------------------------------------------


def test_the_default_hub_class_carries_deckmux_presence() -> None:
    """The reference server's hub is the presence-enabled subclass."""
    hub = _app().state.uterm_hub

    assert hub.__class__.__name__ == "_DefaultTermHub"
    assert hasattr(hub, "_deckmux_init")


def test_the_fanout_controller_is_bound_to_this_hub_and_a_store() -> None:
    """It routes through this hub and keeps membership in this store."""
    app = _app()
    controller = app.state.uterm_hub.fan_out_controller

    assert controller._hub is app.state.uterm_hub
    assert controller._store is not None


def test_the_event_bus_counts_through_this_apps_metrics() -> None:
    """A bus wired to no counter drops its queue statistics silently."""
    app = _app()

    assert app.state.uterm_hub._event_bus is not None


def test_the_recording_store_is_built_once_and_shared_with_the_registry() -> None:
    """Two stores would write recordings to two places."""
    app = _app()

    assert app.state.uterm_registry._recording_store is not None


# ---------------------------------------------------------------------------
# Graphical targets
# ---------------------------------------------------------------------------


def test_graphical_targets_are_seeded_and_wired_to_the_vnc_dialer() -> None:
    """Seeded at startup so an invalid target fails the boot, not the first request.

    The dial factory is built FROM the registry; handing it nothing leaves
    ``/gui/vnc`` unable to open any seeded target.
    """
    app = _app()

    assert app.state.uterm_graphical_targets is not None
    assert app.state.uterm_hub.vnc_upstream_factory is not None


# ---------------------------------------------------------------------------
# What the app publishes about itself
# ---------------------------------------------------------------------------


def test_the_app_is_titled_from_configuration() -> None:
    """The title is the served OpenAPI document's name."""
    config = default_server_config()
    config.server.title = "uterm-under-test"

    assert _app(config).title == "uterm-under-test"


def test_every_collaborator_the_routes_read_is_published_on_app_state() -> None:
    """Routes and middleware reach these through ``app.state`` and nothing else.

    Asserted together because they are assigned as one block: any single one
    left ``None`` fails only whichever route happens to need it, at request
    time, in production.
    """
    app = _app()

    for name in (
        "uterm_config",
        "uterm_policy",
        "uterm_authz",
        "uterm_hub",
        "uterm_registry",
        "uterm_metrics",
        "uterm_webhooks",
        "uterm_profile_store",
        "uterm_control_plane",
        "uterm_durability_capabilities",
        "uterm_tunnel_tokens",
        "uterm_tunnel_invites",
        "uterm_api_key_store",
        "uterm_graphical_targets",
        "uterm_idp",
    ):
        assert getattr(app.state, name) is not None, f"app.state.{name} was not published"


def test_the_startup_time_is_recorded_as_a_wall_clock_reading() -> None:
    """Uptime is computed against this; ``None`` makes every uptime report fail."""
    import time

    before = time.time()
    started = _app().state.uterm_startup_time

    assert isinstance(started, float)
    assert started >= before


# ---------------------------------------------------------------------------
# The resume guard's creation-time boundary
# ---------------------------------------------------------------------------


async def test_a_token_stamped_at_the_epoch_boundary_is_still_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wall_created_at > 0`` — the guard arms for any real stamp, including 1.

    Raising the threshold silently disables the delete-and-recreate check for
    tokens whose stamp falls below it, and those are exactly the oldest tokens.
    """
    from datetime import UTC, datetime

    app = _app()
    definition = MagicMock()
    definition.created_at = datetime.fromtimestamp(500.0, tz=UTC)
    monkeypatch.setattr(app.state.uterm_registry, "get_definition", AsyncMock(return_value=definition))
    session = MagicMock()
    session.worker_id, session.wall_created_at = "w1", 1.0

    assert await app.state.uterm_hub._on_resume("tok", session) is False


# ---------------------------------------------------------------------------
# Configured choices, asserted against NON-DEFAULT values
# ---------------------------------------------------------------------------
#
# Every collaborator below has a fallback the constructor supplies when the
# factory passes nothing. That makes a dropped argument invisible whenever the
# test happens to configure the default -- the object still exists, still has
# the right type, and still works. These configure something the default is not.


def test_the_hub_class_can_be_overridden_by_the_caller() -> None:
    """The caller's class is used as-is; the DeckMux default is only a default.

    Embedders pass their own hub subclass. Losing it silently gives them the
    reference server's hub instead of theirs.
    """
    from provide.uterm.server.bridge.hub import TermHub

    app = create_server_app(default_server_config(), api_only=True, hub_class=TermHub)

    assert type(app.state.uterm_hub) is TermHub


async def test_the_configured_behavioral_thresholds_reach_the_hub(stub_http: None) -> None:
    """The audit loop compares against these numbers; defaults are not the config."""
    config = _governed_config()
    config.governance.behavioral_max_cps = 42.0
    config.governance.behavioral_min_jitter = 0.125

    thresholds = _app(config).state.uterm_hub._behavioral_thresholds

    assert (thresholds.max_cps, thresholds.min_jitter) == (42.0, 0.125)


def test_the_configured_recording_backend_is_the_one_the_registry_uses() -> None:
    """``store_type`` selects the backend; dropping it silently writes to local files."""
    config = default_server_config()
    config.recording.store_type = "memory"

    store = _app(config).state.uterm_registry._recording_store

    assert type(store).__name__ == "InMemoryRecordingStore"


def test_the_configured_default_visibility_reaches_the_registry() -> None:
    """A session created without an explicit visibility inherits this."""
    config = default_server_config()
    config.security.default_session_visibility = "private"

    assert _app(config).state.uterm_registry._default_visibility == "private"


def test_the_webhook_manager_counts_through_this_apps_metrics() -> None:
    """Delivery counters are read from ``app.state.uterm_metrics``.

    Unwired, the manager counts into nothing and the metrics endpoint reports
    zero deliveries for a server that is delivering.
    """
    app = _app()

    app.state.uterm_webhooks._on_metric("webhook_widgets")

    assert app.state.uterm_metrics["webhook_widgets"] == 1


def test_the_policy_resolver_defers_to_this_apps_authorization_service() -> None:
    """It answers role questions by asking authz; without it there is nothing to ask."""
    app = _app()

    assert app.state.uterm_policy.authz is app.state.uterm_authz


def test_the_identity_provider_is_given_the_apps_api_key_store() -> None:
    """API-key auth resolves through this store; another one knows no keys."""
    seen: list[Any] = []
    original = factory_impl.build_identity_provider

    def _record(config: Any, store: Any) -> Any:
        seen.append(store)
        return original(config, store)

    app = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(factory_impl, "build_identity_provider", _record)
        app = _app()

    assert seen and seen[0] is app.state.uterm_api_key_store


def test_the_hub_route_authz_reads_the_live_registry() -> None:
    """A late-bound getter, because the registry does not exist yet at this point.

    Binding ``None`` -- or a lambda that returns it -- makes every hub route
    authorize against no session registry at all, which fails open or closed
    depending on the route rather than on the policy.
    """
    seen: list[Any] = []
    original = factory_impl.build_require_hub_route_authz

    def _record(*, registry_getter: Any) -> Any:
        seen.append(registry_getter)
        return original(registry_getter=registry_getter)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(factory_impl, "build_require_hub_route_authz", _record)
        app = _app()

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0]() is app.state.uterm_registry


def test_the_vnc_dialer_is_built_from_the_seeded_target_registry() -> None:
    """The factory resolves seeded targets; built from nothing it resolves none."""
    seen: list[Any] = []
    original = factory_impl.attach_vnc_upstream_factory

    def _record(hub: Any, registry: Any) -> Any:
        seen.append(registry)
        return original(hub, registry)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(factory_impl, "attach_vnc_upstream_factory", _record)
        app = _app()

    assert seen and seen[0] is app.state.uterm_graphical_targets
