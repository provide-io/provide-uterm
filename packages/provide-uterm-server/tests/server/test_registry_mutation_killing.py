#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Self-contained mutation-killing suite for server/registry.py (SessionRegistry).

This is the dedicated mutmut-binding suite for registry.py. It is deliberately
SELF-CONTAINED and FULLY MOCKED so it is safe in mutmut's forked workers:

* No external registry state — ``HostedSessionRuntime``, ``connectors.registered_types``
  and ``egress.assert_session_egress_allowed`` are patched, so it does not depend on
  which connectors happen to be registered in the mutants tree (the existing
  test_registry.py aborts the stats phase because ``shell`` isn't registered there).
* No real async streaming/timers — ``asyncio.sleep`` is patched (the 5s ephemeral
  grace period in ``_on_worker_empty``) and the EventBus + its queue are mocks whose
  ``get()`` side-effects drive the SSE loops deterministically. The SSE generator is
  consumed with a per-step ``asyncio.wait_for`` bound so a mutant that breaks loop
  termination fails fast (TimeoutError = a kill) instead of stalling the worker.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.registry import SessionRegistry, SessionValidationError

_KNOWN_TYPES = frozenset({"shell", "ssh", "telnet", "websocket", "pty"})


@pytest.fixture
def runtime() -> MagicMock:
    rt = MagicMock(name="runtime")
    rt.start = AsyncMock(name="start")
    rt.stop = AsyncMock(name="stop")
    rt.restart = AsyncMock(name="restart")
    rt.clear = AsyncMock(name="clear")
    rt.set_mode = AsyncMock(name="set_mode")
    rt.analyze = AsyncMock(name="analyze", return_value="ANALYSIS-OUT")
    rt.flush_recording = AsyncMock(name="flush_recording")
    rt.set_tunnel_state = MagicMock(name="set_tunnel_state")
    status = MagicMock(name="status-obj")
    status.recording_enabled = True
    rt.status = MagicMock(name="status", return_value=status)
    return rt


@pytest.fixture
def recstore() -> MagicMock:
    store = MagicMock(name="recording_store")
    store.recording_meta = AsyncMock(return_value={"bytes": 7})
    store.get_path = AsyncMock(return_value="/rec/path")
    store.get_entries = AsyncMock(return_value=[{"entry": 1}])
    return store


@pytest.fixture(autouse=True)
def _patches(runtime: MagicMock) -> Any:
    """Patch every external dependency registry.py reaches for."""
    rt_cls = MagicMock(name="HostedSessionRuntime", return_value=runtime)
    with (
        patch("provide.uterm.server.registry.HostedSessionRuntime", rt_cls),
        patch(
            "provide.uterm.server.connectors.registered_types",
            MagicMock(return_value=_KNOWN_TYPES),
        ),
        patch("provide.uterm.server.egress.assert_session_egress_allowed", AsyncMock()),
        patch("provide.uterm.server.registry.asyncio.sleep", AsyncMock()),
    ):
        yield rt_cls


def _make_hub(*, event_bus: Any = None) -> MagicMock:
    hub = MagicMock(name="hub")
    hub.force_release_hijack = AsyncMock(return_value=True)
    hub.set_input_mode = AsyncMock(return_value=(True, None))
    hub.get_last_snapshot = AsyncMock(return_value={"snap": 1})
    hub.wait_for_snapshot = AsyncMock(return_value=None)
    hub.get_recent_events = AsyncMock(return_value=[{"ev": 1}])
    hub.browser_count = AsyncMock(return_value=0)
    hub.event_bus = event_bus
    return hub


def _session(
    session_id: str = "sess1",
    *,
    auto_start: bool = False,
    ephemeral: bool = False,
    owner: str | None = None,
) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        connector_type="shell",
        auto_start=auto_start,
        ephemeral=ephemeral,
        owner=owner,
    )


def _make_registry(
    sessions: list[SessionDefinition] | None = None,
    *,
    hub: MagicMock | None = None,
    recstore: MagicMock | None = None,
    max_sessions: int | None = None,
    tunnel_tokens: dict[str, dict[str, object]] | None = None,
    block_private: bool = False,
) -> SessionRegistry:
    return SessionRegistry(
        sessions or [],
        hub=hub or _make_hub(),
        public_base_url="http://h:9999",
        recording=RecordingConfig(),
        recording_store=recstore or MagicMock(name="recstore"),
        worker_bearer_token="bearer-x",
        max_sessions=max_sessions,
        tunnel_tokens=tunnel_tokens,
        block_private_connector_targets=block_private,
    )


# ===========================================================================
# __init__
# ===========================================================================


class TestInit:
    def test_sessions_indexed_by_id(self) -> None:
        s1, s2 = _session("a"), _session("b")
        reg = _make_registry([s1, s2])
        assert reg._sessions == {"a": s1, "b": s2}

    def test_block_private_coerced_to_bool_and_stored(self) -> None:
        assert _make_registry(block_private=True)._block_private is True
        assert _make_registry()._block_private is False

    def test_on_worker_empty_callback_wired_to_hub(self) -> None:
        hub = _make_hub()
        reg = _make_registry(hub=hub)
        assert hub.on_worker_empty == reg._on_worker_empty

    def test_core_fields_stored(self) -> None:
        reg = _make_registry()
        assert reg._public_base_url == "http://h:9999"
        assert reg._worker_bearer_token == "bearer-x"
        assert reg._runtimes == {}

    def test_creates_local_store_when_none_passed(self) -> None:
        from provide.uterm.recording import LocalFileRecordingStore

        reg = SessionRegistry(
            [],
            hub=_make_hub(),
            public_base_url="http://h",
            recording=RecordingConfig(),
            recording_store=None,
        )
        assert isinstance(reg._recording_store, LocalFileRecordingStore)


# ===========================================================================
# _runtime_for / get_runtime / _require_session
# ===========================================================================


class TestRuntimeFor:
    def test_get_runtime_returns_cached_or_none(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        assert reg.get_runtime("a") is None  # not yet built
        reg._runtimes["a"] = runtime
        assert reg.get_runtime("a") is runtime

    def test_runtime_for_builds_once_and_caches(self, runtime: MagicMock, _patches: MagicMock) -> None:
        reg = _make_registry()
        s = _session("a")
        rt1 = reg._runtime_for(s)
        rt2 = reg._runtime_for(s)
        assert rt1 is runtime and rt2 is runtime
        _patches.assert_called_once()  # constructed exactly once (cached on second call)
        assert reg._runtimes["a"] is runtime

    def test_runtime_for_passes_expected_kwargs(self, _patches: MagicMock) -> None:
        reg = _make_registry()
        s = _session("a")
        reg._runtime_for(s)
        kwargs = _patches.call_args.kwargs
        assert _patches.call_args.args[0] is s
        assert kwargs["public_base_url"] == "http://h:9999"
        assert kwargs["worker_bearer_token"] == "bearer-x"
        assert kwargs["hub"] is reg._hub
        assert kwargs["block_private_connector_targets"] is False

    async def test_require_session_raises_keyerror_for_unknown(self) -> None:
        reg = _make_registry()
        with pytest.raises(KeyError):
            await reg.get_session("nope")


# ===========================================================================
# list / get
# ===========================================================================


class TestListGet:
    async def test_list_sessions_returns_status_per_session(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a"), _session("b")])
        result = await reg.list_sessions()
        assert result == [runtime.status.return_value, runtime.status.return_value]

    async def test_list_with_definitions_pairs_status_and_def(self, runtime: MagicMock) -> None:
        s = _session("a")
        reg = _make_registry([s])
        result = await reg.list_sessions_with_definitions()
        assert result == [(runtime.status.return_value, s)]

    async def test_get_session_returns_status(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        assert await reg.get_session("a") is runtime.status.return_value

    async def test_get_definition_known_and_unknown(self) -> None:
        s = _session("a")
        reg = _make_registry([s])
        assert await reg.get_definition("a") is s
        assert await reg.get_definition("nope") is None


# ===========================================================================
# _validate_create_payload
# ===========================================================================


class TestValidateCreatePayload:
    def _v(self, payload: dict[str, Any]) -> tuple[str, str, str, str]:
        # _validate_create_payload is an instance method (it reads
        # self._default_visibility); a default registry's default is "public".
        return _make_registry()._validate_create_payload(payload)

    def test_valid_payload_returns_fields(self) -> None:
        out = self._v({"session_id": "abc-1", "connector_type": "ssh", "input_mode": "hijack", "visibility": "private"})
        assert out == ("abc-1", "ssh", "hijack", "private")

    def test_operator_visibility_accepted(self) -> None:
        # "operator" is a valid visibility — pins it as accepted so the mutants
        # that corrupt the "operator" entry of the allowed-set
        # ({"public", "operator", "private"}) are killed (they'd reject it).
        assert self._v({"session_id": "x", "visibility": "operator"}) == ("x", "shell", "open", "operator")

    def test_defaults_when_omitted(self) -> None:
        assert self._v({"session_id": "x"}) == ("x", "shell", "open", "public")

    def test_bad_session_id_rejected_with_message(self) -> None:
        with pytest.raises(SessionValidationError) as exc:
            self._v({"session_id": "has space"})
        assert "session_id must match" in str(exc.value)
        assert "has space" in str(exc.value)

    def test_unknown_connector_type_rejected_with_sorted_known(self) -> None:
        with pytest.raises(SessionValidationError) as exc:
            self._v({"session_id": "x", "connector_type": "bogus"})
        assert "connector_type must be one of" in str(exc.value)
        assert "bogus" in str(exc.value)

    def test_bad_input_mode_rejected(self) -> None:
        with pytest.raises(SessionValidationError) as exc:
            self._v({"session_id": "x", "input_mode": "weird"})
        assert str(exc.value) == "input_mode must be 'open' or 'hijack', got: 'weird'"

    def test_bad_visibility_rejected(self) -> None:
        with pytest.raises(SessionValidationError) as exc:
            self._v({"session_id": "x", "visibility": "weird"})
        assert "visibility must be 'public', 'operator', or 'private'" in str(exc.value)
        assert "weird" in str(exc.value)


# ===========================================================================
# create_session
# ===========================================================================


class TestCreateSession:
    async def test_creates_stores_and_returns_status(self, runtime: MagicMock) -> None:
        reg = _make_registry()
        result = await reg.create_session({"session_id": "new1"})
        assert result is runtime.status.return_value
        assert "new1" in reg._sessions

    async def test_builds_definition_fields_from_payload(self) -> None:
        reg = _make_registry()
        await reg.create_session(
            {
                "session_id": "new1",
                "display_name": "My Session",
                "connector_type": "ssh",
                "input_mode": "hijack",
                "visibility": "private",
                "auto_start": True,
                "tags": ["a", 2],
                "recording_enabled": True,
                "owner": "alice",
                "ephemeral": True,
            }
        )
        s = reg._sessions["new1"]
        assert s.display_name == "My Session"
        assert s.connector_type == "ssh"
        assert s.input_mode == "hijack"
        assert s.visibility == "private"
        assert s.auto_start is True
        assert s.tags == ["a", "2"]  # each tag str()-ed
        assert s.recording_enabled is True
        assert s.owner == "alice"
        assert s.ephemeral is True

    async def test_recording_enabled_and_owner_none_preserved(self) -> None:
        reg = _make_registry()
        await reg.create_session({"session_id": "n", "recording_enabled": None, "owner": None})
        s = reg._sessions["n"]
        assert s.recording_enabled is None
        assert s.owner is None

    async def test_visibility_defaults_to_init_default_public(self) -> None:
        # A registry built WITHOUT an explicit default_visibility uses the
        # __init__ default "public"; a session created without `visibility` must
        # come out public. Kills the __init__ default mutants ("public" ->
        # "XXpublicXX"/"PUBLIC") and the validator's drop-default mutant — each
        # would make the defaulted value fail the visibility validator (raise).
        reg = _make_registry()
        await reg.create_session({"session_id": "n"})
        assert reg._sessions["n"].visibility == "public"

    async def test_visibility_takes_configured_default(self) -> None:
        # A non-"public" configured default must propagate to a session created
        # without an explicit `visibility`. Kills a mutant that hardcodes
        # "public" or ignores self._default_visibility.
        reg = SessionRegistry(
            [],
            hub=_make_hub(),
            public_base_url="http://h:9999",
            recording=RecordingConfig(),
            recording_store=MagicMock(name="recstore"),
            worker_bearer_token="bearer-x",
            default_visibility="private",
        )
        await reg.create_session({"session_id": "n"})
        assert reg._sessions["n"].visibility == "private"

    async def test_default_display_name_is_session_id(self) -> None:
        reg = _make_registry()
        await reg.create_session({"session_id": "n"})
        assert reg._sessions["n"].display_name == "n"

    async def test_auto_start_true_starts_runtime(self, runtime: MagicMock) -> None:
        reg = _make_registry()
        await reg.create_session({"session_id": "n", "auto_start": True})
        runtime.start.assert_awaited_once()

    async def test_auto_start_false_does_not_start(self, runtime: MagicMock) -> None:
        reg = _make_registry()
        await reg.create_session({"session_id": "n", "auto_start": False})
        runtime.start.assert_not_awaited()

    async def test_max_sessions_limit_enforced(self) -> None:
        reg = _make_registry([_session("a")], max_sessions=1)
        with pytest.raises(ValueError, match="session limit reached"):
            await reg.create_session({"session_id": "b"})

    async def test_duplicate_session_rejected(self) -> None:
        reg = _make_registry([_session("a")])
        with pytest.raises(ValueError, match="session already exists"):
            await reg.create_session({"session_id": "a"})

    async def test_egress_block_becomes_validation_error(self) -> None:
        from provide.uterm.server.egress import EgressBlockedError

        reg = _make_registry()
        with patch(
            "provide.uterm.server.egress.assert_session_egress_allowed",
            AsyncMock(side_effect=EgressBlockedError("blocked-host")),
        ):
            with pytest.raises(SessionValidationError, match="blocked-host"):
                await reg.create_session({"session_id": "n"})


# ===========================================================================
# update_session
# ===========================================================================


class TestUpdateSession:
    async def test_applies_mutable_field(self, runtime: MagicMock) -> None:
        s = _session("a")
        reg = _make_registry([s])
        await reg.update_session("a", {"display_name": "Renamed"})
        assert reg._sessions["a"].display_name == "Renamed"

    async def test_ignores_immutable_fields(self) -> None:
        s = _session("a")
        reg = _make_registry([s])
        await reg.update_session("a", {"session_id": "hacked", "connector_type": "ssh"})
        assert "a" in reg._sessions
        assert reg._sessions["a"].connector_type == "shell"  # unchanged

    async def test_input_mode_update_calls_set_mode_and_hub(self, runtime: MagicMock) -> None:
        s = _session("a")
        reg = _make_registry([s])
        await reg.update_session("a", {"input_mode": "hijack"})
        runtime.set_mode.assert_awaited_once_with("hijack")
        reg._hub.set_input_mode.assert_awaited_once_with("a", "hijack")

    async def test_non_input_mode_update_skips_set_mode(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        await reg.update_session("a", {"display_name": "x"})
        runtime.set_mode.assert_not_awaited()

    async def test_unknown_session_raises(self) -> None:
        reg = _make_registry()
        with pytest.raises(KeyError):
            await reg.update_session("nope", {"display_name": "x"})

    async def test_invalid_update_value_becomes_validation_error(self) -> None:
        reg = _make_registry([_session("a")])
        with pytest.raises(SessionValidationError):
            await reg.update_session("a", {"input_mode": "not-a-mode"})

    # ---- owner reassignment (allow_owner_change) --------------------------
    # ``owner`` is absent from _MUTABLE_SESSION_FIELDS, so the only way it can
    # change is the explicit opt-in the admin-gated HTTP path passes. Each test
    # below pins one half of that predicate.

    async def test_owner_changes_only_through_the_opt_in(self) -> None:
        reg = _make_registry([_session("a", owner="alice")])
        await reg.update_session("a", {"owner": "bob"}, allow_owner_change=True)
        assert reg._sessions["a"].owner == "bob"

    async def test_owner_ignored_without_the_opt_in(self) -> None:
        """Kills `and` → `or`: a payload owner must not leak through unopted."""
        reg = _make_registry([_session("a", owner="alice")])
        await reg.update_session("a", {"owner": "bob"}, allow_owner_change=False)
        assert reg._sessions["a"].owner == "alice"

    async def test_owner_opt_in_defaults_off(self) -> None:
        """Kills the signature default flipping to True."""
        reg = _make_registry([_session("a", owner="alice")])
        await reg.update_session("a", {"owner": "bob"})
        assert reg._sessions["a"].owner == "alice"

    async def test_opt_in_without_an_owner_key_is_a_noop(self) -> None:
        """Kills `"owner" in payload` → `not in`, which would KeyError here."""
        reg = _make_registry([_session("a", owner="alice")])
        await reg.update_session("a", {"display_name": "Renamed"}, allow_owner_change=True)
        assert reg._sessions["a"].owner == "alice"
        assert reg._sessions["a"].display_name == "Renamed"

    async def test_owner_reassignment_can_clear_the_owner(self) -> None:
        """An explicit null owner is a value, not a missing key."""
        reg = _make_registry([_session("a", owner="alice")])
        await reg.update_session("a", {"owner": None}, allow_owner_change=True)
        assert reg._sessions["a"].owner is None

    async def test_owner_key_name_is_exactly_owner(self) -> None:
        """Kills the `"owner"` string mutants: a near-miss key must not apply."""
        reg = _make_registry([_session("a", owner="alice")])
        await reg.update_session("a", {"XXownerXX": "bob"}, allow_owner_change=True)
        assert reg._sessions["a"].owner == "alice"

    async def test_owner_reassignment_alone_still_validates(self) -> None:
        """The owner update goes through model_validate like any other field."""
        reg = _make_registry([_session("a", owner="alice")])
        with pytest.raises(SessionValidationError):
            await reg.update_session("a", {"owner": 42}, allow_owner_change=True)

    async def test_update_egress_block_becomes_validation_error(self) -> None:
        from provide.uterm.server.egress import EgressBlockedError

        reg = _make_registry([_session("a")])
        with patch(
            "provide.uterm.server.egress.assert_session_egress_allowed",
            AsyncMock(side_effect=EgressBlockedError("bad-target")),
        ):
            with pytest.raises(SessionValidationError, match="bad-target"):
                await reg.update_session("a", {"connector_config": {"host": "x"}})


# ===========================================================================
# lifecycle delegation: delete / start / stop / restart / clear / analyze
# ===========================================================================


class TestLifecycleDelegation:
    async def test_delete_removes_state_and_stops_runtime(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")], tunnel_tokens={"a": {"t": 1}})
        reg._runtimes["a"] = runtime
        await reg.delete_session("a")
        assert "a" not in reg._sessions
        assert "a" not in reg._runtimes
        assert "a" not in reg._tunnel_tokens  # tokens revoked
        runtime.stop.assert_awaited_once()

    async def test_delete_unknown_is_noop_no_stop(self, runtime: MagicMock) -> None:
        reg = _make_registry()
        await reg.delete_session("nope")
        runtime.stop.assert_not_awaited()

    async def test_start_session_delegates(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        assert await reg.start_session("a") is runtime.status.return_value
        runtime.start.assert_awaited_once()

    async def test_stop_session_delegates(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        await reg.stop_session("a")
        runtime.stop.assert_awaited_once()

    async def test_restart_session_delegates(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        await reg.restart_session("a")
        runtime.restart.assert_awaited_once()

    async def test_clear_session_delegates(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        await reg.clear_session("a")
        runtime.clear.assert_awaited_once()

    async def test_analyze_session_returns_runtime_analysis(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        assert await reg.analyze_session("a") == "ANALYSIS-OUT"
        runtime.analyze.assert_awaited_once()

    async def test_shutdown_stops_all_runtimes(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        reg._runtimes["a"] = runtime
        await reg.shutdown()
        runtime.stop.assert_awaited_once()

    async def test_start_auto_start_sessions_only_starts_flagged(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a", auto_start=True), _session("b", auto_start=False)])
        await reg.start_auto_start_sessions()
        runtime.start.assert_awaited_once()  # only "a"


# ===========================================================================
# set_mode
# ===========================================================================


class TestSetMode:
    async def test_open_mode_releases_hijack_then_sets(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        await reg.set_mode("a", "open")
        reg._hub.force_release_hijack.assert_awaited_once_with("a")
        runtime.set_mode.assert_awaited_once_with("open")
        reg._hub.set_input_mode.assert_awaited_once_with("a", "open")

    async def test_hijack_mode_does_not_release(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        await reg.set_mode("a", "hijack")
        reg._hub.force_release_hijack.assert_not_awaited()
        runtime.set_mode.assert_awaited_once_with("hijack")

    async def test_invalid_mode_rejected(self) -> None:
        reg = _make_registry([_session("a")])
        with pytest.raises(SessionValidationError):
            await reg.set_mode("a", "bogus")


# ===========================================================================
# set_tunnel_connected
# ===========================================================================


class TestSetTunnelConnected:
    async def test_unknown_session_returns_none(self, runtime: MagicMock) -> None:
        reg = _make_registry()
        assert await reg.set_tunnel_connected("nope", True) is None
        runtime.set_tunnel_state.assert_not_called()

    async def test_known_session_sets_state_and_returns_status(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        result = await reg.set_tunnel_connected("a", True)
        assert result is runtime.status.return_value
        runtime.set_tunnel_state.assert_called_once_with(True)


# ===========================================================================
# pass-through reads: last_snapshot / events / _force_release_hijack
# ===========================================================================


class TestPassThroughReads:
    async def test_last_snapshot_delegates_with_recipient(self) -> None:
        reg = _make_registry()
        sentinel = object()
        result = await reg.last_snapshot("a", recipient=sentinel)
        assert result is not None
        assert result["snap"] == 1
        reg._hub.get_last_snapshot.assert_awaited_once_with("a", recipient=sentinel)

    async def test_a_plain_read_does_not_poll_the_worker(self) -> None:
        """No ``wait_ms`` means no round trip: the cheap read stays cheap."""
        reg = _make_registry()
        await reg.last_snapshot("a")
        reg._hub.wait_for_snapshot.assert_not_awaited()

    async def test_a_plain_read_says_it_came_from_cache(self) -> None:
        """The whole point: a cached screen must announce itself as cached.

        Without this the caller sees a screen and cannot tell an idle terminal
        from a worker that stopped answering — the two are byte-identical.
        """
        reg = _make_registry()
        result = await reg.last_snapshot("a")
        assert result is not None
        assert result["snapshot_source"] == "cache"
        assert result["snapshot_requested"] is False

    async def test_wait_ms_polls_the_worker_and_is_forwarded_as_a_timeout(self) -> None:
        reg = _make_registry()
        await reg.last_snapshot("a", wait_ms=900)
        reg._hub.wait_for_snapshot.assert_awaited_once_with("a", timeout_ms=900)

    async def test_a_poll_that_produced_a_new_snapshot_reports_fresh(self) -> None:
        reg = _make_registry()
        reg._hub.wait_for_snapshot = AsyncMock(return_value={"snap": 2})
        result = await reg.last_snapshot("a", wait_ms=900)
        assert result is not None
        assert result["snapshot_source"] == "fresh"

    async def test_a_poll_that_timed_out_still_reports_cache(self) -> None:
        """A timed-out poll returns the cached screen, so it must say ``cache``.

        Reporting ``fresh`` merely because freshness was *requested* would put
        the endpoint back where it started: a stale screen wearing a live label.
        """
        reg = _make_registry()
        reg._hub.wait_for_snapshot = AsyncMock(return_value=None)
        result = await reg.last_snapshot("a", wait_ms=900)
        assert result is not None
        assert result["snapshot_source"] == "cache"
        assert result["snapshot_requested"] is True

    async def test_the_returned_payload_is_always_the_redacted_one(self) -> None:
        """``wait_for_snapshot`` bypasses output redaction, so its result drives
        freshness only — the body still comes from ``get_last_snapshot``.

        Returning the polled snapshot directly would let ``?wait_ms=`` read a
        screen the requester's role is not allowed to see.
        """
        reg = _make_registry()
        reg._hub.wait_for_snapshot = AsyncMock(return_value={"snap": "UNREDACTED"})
        result = await reg.last_snapshot("a", recipient=object(), wait_ms=900)
        assert result is not None
        assert result["snap"] == 1

    async def test_the_age_is_measured_from_the_snapshot_timestamp(self) -> None:
        reg = _make_registry()
        reg._hub.get_last_snapshot = AsyncMock(return_value={"snap": 1, "ts": time.time() - 2.0})
        result = await reg.last_snapshot("a")
        assert result is not None
        assert 1500 <= result["snapshot_age_ms"] <= 5000

    async def test_a_snapshot_without_a_timestamp_reports_an_unknown_age(self) -> None:
        """``None`` age, not ``0``: a snapshot with no ``ts`` is of unknown
        vintage, and reporting zero would claim it had just arrived."""
        reg = _make_registry()
        result = await reg.last_snapshot("a")
        assert result is not None
        assert result["snapshot_age_ms"] is None

    async def test_the_age_is_in_milliseconds_not_seconds(self) -> None:
        """Pins the 1000x scale. An age reported in seconds reads as a snapshot
        that just arrived, which is the exact confusion these fields exist to
        remove."""
        reg = _make_registry()
        reg._hub.get_last_snapshot = AsyncMock(return_value={"snap": 1, "ts": time.time() - 30.0})
        result = await reg.last_snapshot("a")
        assert result is not None
        assert 29_000 <= result["snapshot_age_ms"] <= 40_000

    async def test_a_timestamp_from_the_future_clamps_to_zero(self) -> None:
        """Clock skew between the worker and this process must not produce a
        NEGATIVE age. Zero says "as fresh as it gets"; -4000 says nothing, and
        any consumer comparing against a staleness threshold would sail past
        it."""
        reg = _make_registry()
        reg._hub.get_last_snapshot = AsyncMock(return_value={"snap": 1, "ts": time.time() + 60.0})
        result = await reg.last_snapshot("a")
        assert result is not None
        assert result["snapshot_age_ms"] == 0

    async def test_a_zero_timestamp_is_unknown_not_ancient(self) -> None:
        """``ts=0`` is the absent-timestamp default, not 1970. Treating it as a
        real time would report an age of ~57 years and mark every such snapshot
        catastrophically stale."""
        reg = _make_registry()
        reg._hub.get_last_snapshot = AsyncMock(return_value={"snap": 1, "ts": 0})
        result = await reg.last_snapshot("a")
        assert result is not None
        assert result["snapshot_age_ms"] is None

    @pytest.mark.parametrize("ts", ["1700000000", None, [], {"t": 1}])
    async def test_a_non_numeric_timestamp_is_unknown_rather_than_an_error(self, ts: Any) -> None:
        """A worker sending a malformed ``ts`` must not take down the read. The
        snapshot is still served; only its age is unknown."""
        reg = _make_registry()
        reg._hub.get_last_snapshot = AsyncMock(return_value={"snap": 1, "ts": ts})
        result = await reg.last_snapshot("a")
        assert result is not None
        assert result["snapshot_age_ms"] is None

    async def test_a_boolean_timestamp_is_not_treated_as_a_number(self) -> None:
        """``bool`` is a subclass of ``int``, so ``isinstance(ts, int | float)``
        accepts ``True`` and would date the snapshot to 1970 plus one second."""
        reg = _make_registry()
        reg._hub.get_last_snapshot = AsyncMock(return_value={"snap": 1, "ts": True})
        result = await reg.last_snapshot("a")
        assert result is not None
        assert result["snapshot_age_ms"] is None

    async def test_the_smallest_positive_wait_still_polls(self) -> None:
        """The poll threshold is "any positive wait", not some larger number."""
        reg = _make_registry()
        result = await reg.last_snapshot("a", wait_ms=1)
        reg._hub.wait_for_snapshot.assert_awaited_once_with("a", timeout_ms=1)
        # And it is REPORTED as requested. The flag and the poll are computed
        # from the same threshold, so they have to agree: a read that polled but
        # reported `snapshot_requested=False` would make a timed-out poll
        # indistinguishable from a plain cache read, which is the whole
        # distinction this field exists to draw.
        assert result is not None
        assert result["snapshot_requested"] is True

    async def test_the_age_is_computed_exactly_not_approximately(self) -> None:
        """Pinned against a frozen clock, so the arithmetic itself is asserted.

        A tolerance-based bound cannot see a factor that is off by a fraction of
        a percent -- 30s at x1001 instead of x1000 is 30030ms, inside any range
        loose enough to survive a real clock. Freezing time is what makes the
        conversion exact rather than plausible.
        """
        from types import SimpleNamespace

        from provide.uterm.server import registry as registry_module

        snapshot_ts = 1_700_000_000.0
        reg = _make_registry()
        reg._hub.get_last_snapshot = AsyncMock(return_value={"snap": 1, "ts": snapshot_ts})
        frozen = SimpleNamespace(time=lambda: snapshot_ts + 12.5)
        with patch.object(registry_module, "time", frozen):
            result = await reg.last_snapshot("a")

        assert result is not None
        assert result["snapshot_age_ms"] == 12_500

    async def test_any_positive_timestamp_dates_the_snapshot(self) -> None:
        """The unknown-vintage cutoff is zero, not "some small number".

        ``ts=1`` is 1970-01-01T00:00:01 -- absurd, but a real instant, and
        reporting it as catastrophically stale is useful. Reporting it as
        ``None`` would say "unknown", which is the one thing it is not.
        """
        reg = _make_registry()
        reg._hub.get_last_snapshot = AsyncMock(return_value={"snap": 1, "ts": 1})
        result = await reg.last_snapshot("a")
        assert result is not None
        assert result["snapshot_age_ms"] is not None
        assert result["snapshot_age_ms"] > 1_000_000_000

    async def test_a_negative_wait_does_not_poll(self) -> None:
        reg = _make_registry()
        await reg.last_snapshot("a", wait_ms=-1)
        reg._hub.wait_for_snapshot.assert_not_awaited()

    async def test_an_absent_snapshot_is_not_stamped(self) -> None:
        reg = _make_registry()
        reg._hub.get_last_snapshot = AsyncMock(return_value=None)
        assert await reg.last_snapshot("a") is None

    async def test_events_delegates_with_limit(self) -> None:
        reg = _make_registry()
        result = await reg.events("a", limit=42)
        assert result == [{"ev": 1}]
        reg._hub.get_recent_events.assert_awaited_once_with("a", 42)

    async def test_force_release_hijack_delegates(self) -> None:
        reg = _make_registry()
        assert await reg._force_release_hijack("a") is True
        reg._hub.force_release_hijack.assert_awaited_once_with("a")


# ===========================================================================
# _on_worker_empty — ephemeral grace-period cleanup (asyncio.sleep mocked)
# ===========================================================================


class TestOnWorkerEmpty:
    async def test_non_ephemeral_session_not_deleted(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a", ephemeral=False)])
        reg._runtimes["a"] = runtime
        await reg._on_worker_empty("a")
        assert "a" in reg._sessions
        runtime.stop.assert_not_awaited()

    async def test_unknown_session_noop(self, runtime: MagicMock) -> None:
        reg = _make_registry()
        await reg._on_worker_empty("nope")
        runtime.stop.assert_not_awaited()

    async def test_ephemeral_with_browsers_present_not_deleted(self, runtime: MagicMock) -> None:
        hub = _make_hub()
        hub.browser_count = AsyncMock(return_value=2)  # browsers reconnected during grace
        reg = _make_registry([_session("a", ephemeral=True)], hub=hub)
        reg._runtimes["a"] = runtime
        await reg._on_worker_empty("a")
        assert "a" in reg._sessions
        runtime.stop.assert_not_awaited()

    async def test_ephemeral_with_no_browsers_deleted(self, runtime: MagicMock) -> None:
        hub = _make_hub()
        hub.browser_count = AsyncMock(return_value=0)
        s = _session("a", ephemeral=True)
        reg = _make_registry([s], hub=hub)
        reg._runtimes["a"] = runtime
        await reg._on_worker_empty("a")
        assert "a" not in reg._sessions
        runtime.stop.assert_awaited_once()

    async def test_ephemeral_deleted_when_no_runtime_built(self) -> None:
        """Ephemeral cleanup with no runtime ever built — exercises the
        `if runtime is not None` False branch (no stop to await)."""
        hub = _make_hub()
        hub.browser_count = AsyncMock(return_value=0)
        reg = _make_registry([_session("a", ephemeral=True)], hub=hub)
        # deliberately do NOT populate reg._runtimes
        await reg._on_worker_empty("a")
        assert "a" not in reg._sessions

    async def test_identity_changed_during_grace_not_deleted(self, runtime: MagicMock) -> None:
        hub = _make_hub()
        hub.browser_count = AsyncMock(return_value=0)
        reg = _make_registry([_session("a", ephemeral=True)], hub=hub)
        # Simulate the session being replaced by a fresh object under the same id
        # during the (mocked-instant) grace sleep.
        replacement = _session("a", ephemeral=True)

        async def _swap(*_a: Any, **_k: Any) -> int:
            reg._sessions["a"] = replacement
            return 0

        hub.browser_count = AsyncMock(side_effect=_swap)
        await reg._on_worker_empty("a")
        assert reg._sessions["a"] is replacement  # not deleted


# ===========================================================================
# recording: recording_meta / recording_path / recording_entries / _flush
# ===========================================================================


class TestRecording:
    async def test_recording_meta_merges_enabled_flag(self, runtime: MagicMock, recstore: MagicMock) -> None:
        reg = _make_registry([_session("a")], recstore=recstore)
        result = await reg.recording_meta("a")
        assert result == {"bytes": 7, "enabled": True}
        recstore.recording_meta.assert_awaited_once_with("a")

    async def test_recording_path_delegates(self, recstore: MagicMock) -> None:
        reg = _make_registry(recstore=recstore)
        assert await reg.recording_path("a") == "/rec/path"
        recstore.get_path.assert_awaited_once_with("a")

    async def test_recording_entries_flushes_then_reads(self, runtime: MagicMock, recstore: MagicMock) -> None:
        reg = _make_registry([_session("a")], recstore=recstore)
        reg._runtimes["a"] = runtime
        result = await reg.recording_entries("a", limit=5, offset=3, event="snapshot")
        assert result == [{"entry": 1}]
        runtime.flush_recording.assert_awaited_once()
        recstore.get_entries.assert_awaited_once_with("a", limit=5, offset=3, event="snapshot")

    async def test_flush_runtime_recording_only_when_runtime_exists(self, runtime: MagicMock) -> None:
        reg = _make_registry([_session("a")])
        await reg._flush_runtime_recording("a")  # no runtime built → no flush
        runtime.flush_recording.assert_not_awaited()
        reg._runtimes["a"] = runtime
        await reg._flush_runtime_recording("a")
        runtime.flush_recording.assert_awaited_once()


# ===========================================================================
# watch_session_events — long-poll (EventBus mocked; bounded)
# ===========================================================================


def _bus_with(get_results: list[Any], *, dropped: int = 0) -> tuple[MagicMock, MagicMock]:
    sub = MagicMock(name="sub")
    sub.queue = MagicMock(name="queue")
    sub.queue.get = AsyncMock(side_effect=get_results)
    sub.dropped = dropped
    cm = MagicMock(name="watch_cm")
    cm.__aenter__ = AsyncMock(return_value=sub)
    cm.__aexit__ = AsyncMock(return_value=False)
    bus = MagicMock(name="event_bus")
    bus.watch = MagicMock(return_value=cm)
    return bus, sub


class TestWatchSessionEvents:
    async def test_no_bus_falls_back_to_ring_buffer(self) -> None:
        hub = _make_hub(event_bus=None)
        hub.get_recent_events = AsyncMock(return_value=[{"r": 1}])
        reg = _make_registry(hub=hub)
        result = await reg.watch_session_events("a", max_events=9)
        assert result == {"events": [{"r": 1}], "dropped_count": 0, "timed_out": False}
        hub.get_recent_events.assert_awaited_once_with("a", limit=9)

    async def test_collects_until_sentinel(self) -> None:
        bus, _sub = _bus_with([{"e": 1}, {"e": 2}, None], dropped=3)
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        result = await asyncio.wait_for(reg.watch_session_events("a", timeout_ms=5000, max_events=50), timeout=5.0)
        assert result == {"events": [{"e": 1}, {"e": 2}], "dropped_count": 3, "timed_out": False}

    async def test_stops_at_max_events(self) -> None:
        bus, _sub = _bus_with([{"e": 1}, {"e": 2}, {"e": 3}])
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        result = await asyncio.wait_for(reg.watch_session_events("a", max_events=2), timeout=5.0)
        assert result["events"] == [{"e": 1}, {"e": 2}]
        assert result["timed_out"] is False

    async def test_timeout_sets_timed_out(self) -> None:
        bus, _sub = _bus_with([TimeoutError()])
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        result = await asyncio.wait_for(reg.watch_session_events("a", max_events=5), timeout=5.0)
        assert result["events"] == []
        assert result["timed_out"] is True

    async def test_passes_filters_to_watch(self) -> None:
        bus, _sub = _bus_with([None])
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        await asyncio.wait_for(reg.watch_session_events("a", event_types=["snapshot"], pattern=r"\$"), timeout=5.0)
        bus.watch.assert_called_once_with("a", event_types=["snapshot"], pattern=r"\$")


# ===========================================================================
# stream_session_events — SSE generator (bounded per-step wait_for)
# ===========================================================================


async def _collect_sse(gen: Any, *, max_chunks: int = 12, per_timeout: float = 3.0) -> list[str]:
    """Consume an SSE async-generator with a per-step timeout bound.

    A mutant that breaks loop termination (infinite yield, or a hang on queue.get)
    is killed deterministically: extra/garbled chunks fail the equality assertion,
    and a true hang raises TimeoutError — never a 30s worker stall.
    """
    chunks: list[str] = []
    try:
        for _ in range(max_chunks):
            chunks.append(await asyncio.wait_for(gen.__anext__(), timeout=per_timeout))
    except StopAsyncIteration:
        pass
    return chunks


class TestStreamSessionEvents:
    async def test_no_bus_yields_nothing(self) -> None:
        reg = _make_registry(hub=_make_hub(event_bus=None))
        assert await _collect_sse(reg.stream_session_events("a")) == []

    async def test_event_is_sse_formatted(self) -> None:
        bus, _sub = _bus_with([{"type": "snapshot", "seq": 1}, None])
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        chunks = await _collect_sse(reg.stream_session_events("a"))
        assert chunks == [
            'data: {"type": "snapshot", "seq": 1}\n\n',
            'data: {"type":"worker_disconnected"}\n\n',
        ]

    async def test_idle_yields_heartbeat_then_continues(self) -> None:
        bus, _sub = _bus_with([TimeoutError(), {"type": "x"}, None])
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        chunks = await _collect_sse(reg.stream_session_events("a", heartbeat_s=0.01))
        assert chunks == [
            'data: {"type":"heartbeat"}\n\n',
            'data: {"type": "x"}\n\n',
            'data: {"type":"worker_disconnected"}\n\n',
        ]

    async def test_sentinel_stops_generator(self) -> None:
        bus, _sub = _bus_with([None, {"never": "delivered"}])
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        chunks = await _collect_sse(reg.stream_session_events("a"))
        # stops at the sentinel — the second item must NOT be delivered
        assert chunks == ['data: {"type":"worker_disconnected"}\n\n']

    async def test_passes_filters_to_watch(self) -> None:
        bus, _sub = _bus_with([None])
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        await _collect_sse(reg.stream_session_events("a", event_types=["snapshot"], pattern=r"\$"))
        bus.watch.assert_called_once_with("a", event_types=["snapshot"], pattern=r"\$")

    async def test_real_heartbeat_uses_wait_for_timeout(self) -> None:
        """A REAL empty queue makes the heartbeat fire via wait_for's own timeout.

        Kills mutmut: ``timeout=heartbeat_s`` -> ``timeout=None`` (which would block
        forever on the empty queue rather than time out into a heartbeat — the
        per-step wait_for bound turns that hang into a fast kill).
        """
        q: asyncio.Queue[Any] = asyncio.Queue()  # empty → get() blocks
        sub = MagicMock(name="sub")
        sub.queue = q
        sub.dropped = 0
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=sub)
        cm.__aexit__ = AsyncMock(return_value=False)
        bus = MagicMock()
        bus.watch = MagicMock(return_value=cm)
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        gen = reg.stream_session_events("a", heartbeat_s=0.05)
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
        assert chunk == 'data: {"type":"heartbeat"}\n\n'
        await gen.aclose()


# ===========================================================================
# Targeted kills: exact call args / messages / timing / defaults
# ===========================================================================


class TestKills:
    # --- __init__ / _runtime_for: detector + store threaded ---------------
    def test_init_stores_detector(self) -> None:
        det = object()
        reg = SessionRegistry(
            [],
            hub=_make_hub(),
            public_base_url="h",
            recording=RecordingConfig(),
            recording_store=MagicMock(),
            detector=det,
        )
        assert reg._detector is det  # kills self._detector = detector -> None

    def test_runtime_for_threads_store_and_detector(self, _patches: MagicMock) -> None:
        det = object()
        reg = SessionRegistry(
            [],
            hub=_make_hub(),
            public_base_url="h",
            recording=RecordingConfig(),
            recording_store=MagicMock(name="store"),
            detector=det,
        )
        reg._runtime_for(_session("a"))
        kwargs = _patches.call_args.kwargs
        assert kwargs["recording_store"] is reg._recording_store  # kills recording_store=None / dropped
        assert kwargs["detector"] is det  # kills detector=None / dropped

    # --- _require_session message -----------------------------------------
    async def test_require_session_keyerror_message(self) -> None:
        reg = _make_registry()
        with pytest.raises(KeyError) as exc:
            await reg.get_session("ghost")
        assert "unknown session" in str(exc.value)  # kills KeyError(None)

    # --- create_session: egress args + connector_config + defaults --------
    async def test_create_egress_and_connector_config_exact(self) -> None:
        reg = _make_registry(block_private=True)
        egress = AsyncMock()
        with patch("provide.uterm.server.egress.assert_session_egress_allowed", egress):
            await reg.create_session({"session_id": "n", "connector_type": "ssh", "connector_config": {"host": "h"}})
        egress.assert_awaited_once_with("ssh", {"host": "h"}, block_private=True)
        assert reg._sessions["n"].connector_config == {"host": "h"}

    async def test_create_omitted_flags_default_false(self, runtime: MagicMock) -> None:
        reg = _make_registry()
        await reg.create_session({"session_id": "n"})  # no auto_start, no ephemeral
        s = reg._sessions["n"]
        assert s.auto_start is False  # kills auto_start default True (the segfault mutant)
        assert s.ephemeral is False  # kills ephemeral default True
        runtime.start.assert_not_awaited()  # auto_start False → no start

    # --- update_session: egress args + message + input_mode deferral ------
    async def test_update_egress_args_exact(self) -> None:
        reg = _make_registry([_session("a")], block_private=True)
        egress = AsyncMock()
        with patch("provide.uterm.server.egress.assert_session_egress_allowed", egress):
            await reg.update_session("a", {"connector_config": {"host": "h"}})
        egress.assert_awaited_once_with("shell", {"host": "h"}, block_private=True)

    async def test_update_invalid_value_message_not_none(self) -> None:
        reg = _make_registry([_session("a")])
        with pytest.raises(SessionValidationError) as exc:
            await reg.update_session("a", {"input_mode": "bad-mode"})
        assert str(exc.value) and str(exc.value) != "None"  # kills SessionValidationError(None)

    async def test_update_input_mode_deferred_not_setattr(self) -> None:
        """input_mode is committed via runtime.set_mode (mocked → no-op), NOT setattr'd
        on the session directly. Kills `field != "input_mode"` string mutations that
        would apply it directly."""
        reg = _make_registry([_session("a")])  # input_mode defaults "open"
        await reg.update_session("a", {"input_mode": "hijack"})
        assert reg._sessions["a"].input_mode == "open"  # unchanged on the definition

    # --- set_mode message --------------------------------------------------
    async def test_set_mode_invalid_message_not_none(self) -> None:
        reg = _make_registry([_session("a")])
        with pytest.raises(SessionValidationError) as exc:
            await reg.set_mode("a", "bad-mode")
        assert str(exc.value) and str(exc.value) != "None"  # kills SessionValidationError(None)

    # --- _on_worker_empty: sleep/browser_count/revoke args + >0 boundary --
    async def test_on_worker_empty_call_args(self, runtime: MagicMock) -> None:
        hub = _make_hub()
        hub.browser_count = AsyncMock(return_value=0)
        reg = _make_registry([_session("a", ephemeral=True)], hub=hub, tunnel_tokens={"a": {"t": 1}})
        reg._runtimes["a"] = runtime
        with patch("provide.uterm.server.registry.asyncio.sleep", AsyncMock()) as slp:
            await reg._on_worker_empty("a")
        slp.assert_awaited_once_with(5)  # kills sleep(None) / sleep(6)
        hub.browser_count.assert_awaited_with("a")  # kills browser_count(None)
        assert "a" not in reg._tunnel_tokens  # kills _revoke_tunnel_tokens(None)

    async def test_on_worker_empty_one_browser_keeps_session(self, runtime: MagicMock) -> None:
        hub = _make_hub()
        hub.browser_count = AsyncMock(return_value=1)  # 1 > 0 True (keep); 1 > 1 False (mutant deletes)
        reg = _make_registry([_session("a", ephemeral=True)], hub=hub)
        reg._runtimes["a"] = runtime
        await reg._on_worker_empty("a")
        assert "a" in reg._sessions  # kills `> 0` -> `> 1`

    # --- _revoke_tunnel_tokens: pop default --------------------------------
    async def test_delete_revokes_absent_token_without_error(self, runtime: MagicMock) -> None:
        # session id NOT among tunnel_tokens → pop(id, None) is a no-op; pop(id) raises.
        reg = _make_registry([_session("a")], tunnel_tokens={"other": {"t": 1}})
        reg._runtimes["a"] = runtime
        await reg.delete_session("a")  # must not raise KeyError
        assert "a" not in reg._sessions

    # --- recording_meta flush arg -----------------------------------------
    async def test_recording_meta_flushes_this_session(self, runtime: MagicMock, recstore: MagicMock) -> None:
        reg = _make_registry([_session("a")], recstore=recstore)
        reg._runtimes["a"] = runtime
        await reg.recording_meta("a")
        runtime.flush_recording.assert_awaited_once()  # kills _flush_runtime_recording(None)

    # --- watch_session_events: timeout arithmetic + deadline branch -------
    async def test_watch_wait_for_timeout_value(self) -> None:
        """Spy on wait_for to pin the computed timeout == timeout_ms/1000.

        Kills the arithmetic mutants: `/1000`->`*1000`, `/1000`->`/1001`,
        and `timeout=remaining`->`timeout=None`. Uses a LARGE timeout_ms so the
        ``/1001`` mutant's gap is ~1.0s (well above timing jitter) and an instant
        sentinel so the recorded timeout is never actually waited. NB: NOT wrapped in
        an outer asyncio.wait_for — patching registry.asyncio.wait_for patches the
        shared module attr, so an outer wait_for would record its own timeout first.
        """
        recorded: list[float | None] = []
        real_wait_for = asyncio.wait_for

        async def _spy(coro: Any, *, timeout: Any) -> Any:
            recorded.append(timeout)
            return await real_wait_for(coro, timeout=timeout)

        bus, _sub = _bus_with([None])  # sentinel → one iteration, get() returns instantly
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        with patch("provide.uterm.server.registry.asyncio.wait_for", _spy):
            await reg.watch_session_events("a", timeout_ms=1_000_000)
        # original: 1_000_000/1000 = 1000.0; *1000 -> 1e9; /1001 -> 999.0; None -> None
        assert recorded and recorded[0] == pytest.approx(1000.0, abs=0.5)

    async def test_watch_zero_timeout_hits_deadline_branch(self) -> None:
        """timeout_ms=0 forces ``remaining <= 0`` → the deadline-exceeded branch.

        Kills: timed_out=True->None, timed_out=True->False, and break->return
        (return would exit the function as None instead of the result dict)."""
        bus, _sub = _bus_with([{"e": 1}])  # never read — the branch breaks before get()
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        result = await asyncio.wait_for(reg.watch_session_events("a", timeout_ms=0), timeout=5.0)
        assert isinstance(result, dict)  # kills break->return (None)
        assert result["timed_out"] is True  # kills timed_out True->None / True->False
        assert result["events"] == []

    async def test_watch_subsecond_timeout_does_not_early_break(self) -> None:
        """timeout_ms=500 → remaining≈0.5: the `<= 0` guard must stay False so the
        event is collected. Kills `remaining <= 0` -> `remaining <= 1`."""
        bus, _sub = _bus_with([{"e": 1}, None])
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        result = await asyncio.wait_for(reg.watch_session_events("a", timeout_ms=500), timeout=5.0)
        assert result["events"] == [{"e": 1}]  # mutant `<= 1` would break before collecting
        assert result["timed_out"] is False

    async def test_watch_dropped_reflects_sub(self) -> None:
        bus, _sub = _bus_with([None], dropped=4)
        reg = _make_registry(hub=_make_hub(event_bus=bus))
        result = await asyncio.wait_for(reg.watch_session_events("a"), timeout=5.0)
        assert result["dropped_count"] == 4
