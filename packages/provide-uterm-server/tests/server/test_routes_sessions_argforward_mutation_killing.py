#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/sessions.py``: argument forwarding and absent keys.

Fourth file of the ``sessions.py`` repair. The first three cover handler
behaviour; these are the families that survive a behavioural test because the
*outcome* is unchanged:

* **Nulled arguments.** ``session_definition(request, None)`` looks up the wrong
  session, but an ``AsyncMock`` returns the same definition whatever it is
  handed, so the handler proceeds and every outcome assertion still passes.
  Twelve handlers share this mutant; it dies only to an assertion on the call.
* **Absent-key defaults.** ``s.get("tags", [])`` mutated to ``s.get("tags")``
  only misbehaves when the key is MISSING — every fixture that always populates
  the key hides it. These rows deliberately omit fields.
* **The sort whitelist.** Its members can only be distinguished when the target
  field's ordering DISAGREES with ``created_at``; a fixture where both agree
  passes under every mutation of the set.
* **``continue`` → ``break``** in the bulk-delete sweep, which is invisible
  unless a skipped row is followed by one that must still be deleted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

MODULE = "provide.uterm.server.routes.sessions"
_SID = "s-1"


def _handler(name: str) -> Any:
    from provide.uterm.server.routes.sessions import session_capability_handlers

    return session_capability_handlers()[name]


def _principal(subject_id: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject_id, roles=set())


def _authz(*, admin: bool = True) -> MagicMock:
    az = MagicMock(name="authz")
    az.is_admin = AsyncMock(return_value=admin)
    az.can_create_session = AsyncMock(return_value=True)
    az.can_mutate_session = AsyncMock(return_value=True)
    az.can_read_session = AsyncMock(return_value=True)
    az.can_read_recording = AsyncMock(return_value=True)
    return az


def _registry(**overrides: Any) -> MagicMock:
    reg = MagicMock(name="registry")
    reg.get_definition = AsyncMock(return_value=SimpleNamespace(session_id=_SID))
    for name in (
        "create_session",
        "get_session",
        "update_session",
        "delete_session",
        "start_session",
        "stop_session",
        "restart_session",
        "set_mode",
        "clear_session",
    ):
        setattr(reg, name, AsyncMock(return_value={"ok": name}))
    reg.analyze_session = AsyncMock(return_value={})
    reg.last_snapshot = AsyncMock(return_value={})
    reg.events = AsyncMock(return_value=[])
    reg.watch_session_events = AsyncMock(return_value={})
    reg.recording_meta = AsyncMock(return_value={})
    reg.recording_entries = AsyncMock(return_value=[])
    reg.recording_path = AsyncMock(return_value=None)
    reg.get_runtime = MagicMock(return_value=SimpleNamespace(_logger=None))
    reg.list_sessions_with_definitions = AsyncMock(return_value=[])
    for name, value in overrides.items():
        setattr(reg, name, value)
    return reg


def _request(*, registry: Any, authz_obj: Any = None, principal: Any = None) -> MagicMock:
    hub = MagicMock(name="hub")
    hub.append_event = AsyncMock(return_value={"seq": 1})
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(
        uterm_registry=registry,
        uterm_authz=authz_obj if authz_obj is not None else _authz(),
        uterm_hub=hub,
        uterm_tunnel_tokens={},
        uterm_config=SimpleNamespace(recording=None),
    )
    req.state = SimpleNamespace(uterm_principal=principal if principal is not None else _principal())
    req.client = SimpleNamespace(host="1.2.3.4")
    return req


# Handlers that resolve the session definition, with the extra args each needs.
_DEFINITION_HANDLERS: list[tuple[str, tuple[Any, ...]]] = [
    ("sessions.get", ()),
    ("sessions.update", ({},)),
    ("sessions.delete", ()),
    ("sessions.connect", ()),
    ("sessions.disconnect", ()),
    ("sessions.restart", ()),
    ("sessions.set_mode", ({"input_mode": "open"},)),
    ("sessions.clear", ()),
    ("sessions.annotate", ({"label": "x"},)),
    ("sessions.analyze", ()),
    ("sessions.snapshot", ()),
    ("sessions.events", ()),
    ("sessions.events_watch", ()),
    ("sessions.recording", ()),
    ("sessions.recording_entries", ()),
]


class TestSessionDefinitionForwarding:
    """Every handler must resolve the session it was asked about.

    An ``AsyncMock`` answers identically whatever id it receives, so
    ``session_definition(request, None)`` changes no outcome and survives every
    behavioural assertion in the other three files. This is the whole reason the
    file exists.
    """

    @pytest.mark.parametrize(("capability", "extra"), _DEFINITION_HANDLERS)
    async def test_the_named_session_is_the_one_resolved(self, capability: str, extra: tuple[Any, ...]) -> None:
        reg = _registry()
        req = _request(registry=reg)

        with patch(f"{MODULE}.model_dump", side_effect=lambda s: s), patch(f"{MODULE}.audit_event"):
            await _handler(capability)(req, _SID, *extra)

        reg.get_definition.assert_awaited_once_with(_SID)

    async def test_recording_download_resolves_the_named_session(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        with pytest.raises(HTTPException):
            await _handler("sessions.recording_download")(req, _SID)

        reg.get_definition.assert_awaited_once_with(_SID)


class TestPrincipalForwarding:
    async def test_patch_asks_about_the_calling_principal_before_reassigning(self) -> None:
        az = _authz(admin=True)
        principal = _principal("root")
        req = _request(registry=_registry(), authz_obj=az, principal=principal)

        with patch(f"{MODULE}.model_dump", side_effect=lambda s: s):
            await _handler("sessions.update")(req, _SID, {"owner": "mallory"})

        az.is_admin.assert_awaited_once_with(principal)

    async def test_bulk_delete_checks_each_row_for_this_principal_and_action(self) -> None:
        az = _authz(admin=True)
        principal = _principal("root")
        definition = SimpleNamespace(session_id="a")
        reg = _registry(list_sessions_with_definitions=AsyncMock(return_value=[({"session_id": "a"}, definition)]))
        req = _request(registry=reg, authz_obj=az, principal=principal)

        with (
            patch(f"{MODULE}.model_dump", side_effect=lambda s: s),
            patch(f"{MODULE}.audit_event"),
            patch(f"{MODULE}.time.time", return_value=0.0),
        ):
            await _handler("sessions.bulk_delete")(req, {})

        az.can_mutate_session.assert_awaited_once_with(principal, definition, "session.control.delete")


class TestModuleLevelHelpers:
    """Two functions outside the factory that no suite had bound at all.

    Their mutants sat in mutmut's ``"no tests"`` state — not survivors, but
    still in the denominator, which held the file at 98.39% with zero survivors.
    """

    async def test_the_unregistered_placeholder_refuses_to_run(self) -> None:
        """Exact equality, not pytest.raises(match=...): match is a regex
        SEARCH, so an "XX…XX"-padded message still matches and survives."""
        from provide.uterm.server.routes.sessions import _unregistered_capability_handler

        with pytest.raises(RuntimeError) as exc:
            await _unregistered_capability_handler()
        assert str(exc.value) == "unregistered shared API capability invoked"

    async def test_no_required_roles_authorizes_vacuously(self) -> None:
        from provide.uterm.server.routes.sessions import authorize_session_route_roles

        req = _request(registry=_registry(), principal=_principal())

        assert await authorize_session_route_roles(req, ()) is True

    async def test_admin_is_resolved_through_the_authorization_service(self) -> None:
        from provide.uterm.server.routes.sessions import authorize_session_route_roles

        az = _authz(admin=True)
        principal = _principal()
        req = _request(registry=_registry(), authz_obj=az, principal=principal)

        assert await authorize_session_route_roles(req, ("admin",)) is True
        az.is_admin.assert_awaited_once_with(principal)

    async def test_a_self_asserted_admin_claim_is_not_enough(self) -> None:
        """The claim set is attacker-influenced; the service is the authority."""
        from provide.uterm.server.routes.sessions import authorize_session_route_roles

        req = _request(
            registry=_registry(),
            authz_obj=_authz(admin=False),
            principal=SimpleNamespace(subject_id="mallory", roles={"admin"}),
        )

        assert await authorize_session_route_roles(req, ("admin",)) is False

    async def test_a_non_admin_role_is_matched_against_the_claim_set(self) -> None:
        from provide.uterm.server.routes.sessions import authorize_session_route_roles

        az = _authz(admin=False)
        req = _request(
            registry=_registry(),
            authz_obj=az,
            principal=SimpleNamespace(subject_id="op", roles={"operator"}),
        )

        assert await authorize_session_route_roles(req, ("operator",)) is True
        az.is_admin.assert_not_awaited()

    async def test_a_missing_claim_refuses(self) -> None:
        from provide.uterm.server.routes.sessions import authorize_session_route_roles

        req = _request(
            registry=_registry(),
            authz_obj=_authz(admin=False),
            principal=SimpleNamespace(subject_id="v", roles={"viewer"}),
        )

        assert await authorize_session_route_roles(req, ("operator",)) is False

    async def test_every_required_role_must_hold(self) -> None:
        """Conjunctive, unlike the PAM variant in pam_events.py which accepts
        any ONE alternative. Inverting this turns "admin AND operator" into
        "admin OR operator" and widens every multi-role route."""
        from provide.uterm.server.routes.sessions import authorize_session_route_roles

        req = _request(
            registry=_registry(),
            authz_obj=_authz(admin=True),
            principal=SimpleNamespace(subject_id="root", roles=set()),
        )

        assert await authorize_session_route_roles(req, ("admin", "operator")) is False

    async def test_all_roles_present_authorizes(self) -> None:
        from provide.uterm.server.routes.sessions import authorize_session_route_roles

        req = _request(
            registry=_registry(),
            authz_obj=_authz(admin=True),
            principal=SimpleNamespace(subject_id="root", roles={"operator"}),
        )

        assert await authorize_session_route_roles(req, ("admin", "operator")) is True

    async def test_a_failing_admin_check_refuses_even_with_the_other_claims(self) -> None:
        from provide.uterm.server.routes.sessions import authorize_session_route_roles

        req = _request(
            registry=_registry(),
            authz_obj=_authz(admin=False),
            principal=SimpleNamespace(subject_id="op", roles={"operator"}),
        )

        assert await authorize_session_route_roles(req, ("admin", "operator")) is False


class TestAnnotateForwarding:
    """The annotate body reaches four collaborators; each takes real arguments."""

    async def test_an_unauthorized_annotator_is_refused(self) -> None:
        az = _authz()
        az.can_mutate_session = AsyncMock(return_value=False)
        req = _request(registry=_registry(), authz_obj=az)

        with pytest.raises(HTTPException) as exc:
            await _handler("sessions.annotate")(req, _SID, {"label": "x"})

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_the_mutation_check_names_the_principal_session_and_action(self) -> None:
        az = _authz()
        principal = _principal("root")
        definition = SimpleNamespace(session_id=_SID)
        reg = _registry(get_definition=AsyncMock(return_value=definition))
        req = _request(registry=reg, authz_obj=az, principal=principal)

        with patch(f"{MODULE}.audit_event"):
            await _handler("sessions.annotate")(req, _SID, {"label": "x"})

        az.can_mutate_session.assert_awaited_once_with(principal, definition, "session.control.update")

    async def test_the_runtime_is_looked_up_for_the_named_session(self) -> None:
        reg = _registry()
        req = _request(registry=reg)

        with patch(f"{MODULE}.audit_event"):
            await _handler("sessions.annotate")(req, _SID, {"label": "x"})

        reg.get_runtime.assert_called_once_with(_SID)

    async def test_the_recording_logger_receives_the_annotation_itself(self) -> None:
        """Not a null: the recording is the durable copy of the annotation."""
        logger = SimpleNamespace(log_event=AsyncMock())
        reg = _registry(get_runtime=MagicMock(return_value=SimpleNamespace(_logger=logger)))
        req = _request(registry=reg, principal=_principal("root"))

        with patch(f"{MODULE}.audit_event"):
            await _handler("sessions.annotate")(req, _SID, {"label": "boom", "severity": "high"})

        logger.log_event.assert_awaited_once_with(
            "annotation",
            {
                "label": "boom",
                "description": "",
                "severity": "high",
                "source": "agent",
                "principal": "root",
            },
        )


class TestDeleteObservability:
    async def test_the_tracer_is_named_for_this_module(self) -> None:
        tracer = MagicMock(name="tracer")
        tracer.start_as_current_span.return_value.__enter__.return_value = MagicMock()
        get_tracer = MagicMock(return_value=tracer)
        req = _request(registry=_registry())

        with patch(f"{MODULE}.audit_event"), patch(f"{MODULE}.get_tracer", get_tracer):
            await _handler("sessions.delete")(req, _SID)

        get_tracer.assert_called_once_with(MODULE)


class TestCreateSessionStatusCodes:
    async def test_the_privilege_refusal_is_exactly_a_403(self) -> None:
        az = _authz(admin=False)
        az.can_create_session = AsyncMock(return_value=False)
        req = _request(registry=_registry(), authz_obj=az)

        with pytest.raises(HTTPException) as exc:
            await _handler("sessions.create")(req, {})

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_an_unnamed_session_audits_an_empty_id_not_the_string_none(self) -> None:
        """``.get("session_id", "")`` — the registry assigns the id when the
        payload omits it, and "None" in the audit trail is a fabricated id."""
        audit = MagicMock()
        req = _request(registry=_registry(), authz_obj=_authz(admin=True))

        with patch(f"{MODULE}.model_dump", side_effect=lambda s: s), patch(f"{MODULE}.audit_event", audit):
            await _handler("sessions.create")(req, {})

        assert audit.call_args.kwargs["session_id"] == ""

    async def test_an_unnamed_session_spans_an_empty_id(self) -> None:
        span = MagicMock(name="span")
        tracer = MagicMock(name="tracer")
        tracer.start_as_current_span.return_value.__enter__.return_value = span
        req = _request(registry=_registry(), authz_obj=_authz(admin=True))

        with (
            patch(f"{MODULE}.model_dump", side_effect=lambda s: s),
            patch(f"{MODULE}.audit_event"),
            patch(f"{MODULE}.get_tracer", MagicMock(return_value=tracer)),
        ):
            await _handler("sessions.create")(req, {})

        recorded = dict(call.args for call in span.set_attribute.call_args_list)
        # set_span_attrs drops None values, so an empty id must still be recorded.
        assert recorded["uterm.session_id"] == ""


# ===========================================================================
# list_sessions — rows with fields ABSENT
# ===========================================================================


def _sparse_pairs(*rows: dict[str, Any]) -> list[tuple[dict[str, Any], Any]]:
    return [(row, SimpleNamespace(session_id=row.get("session_id", "?"))) for row in rows]


async def _list(rows: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    reg = _registry(list_sessions_with_definitions=AsyncMock(return_value=_sparse_pairs(*rows)))
    req = _request(registry=reg)
    with patch(f"{MODULE}.model_dump", side_effect=lambda status: status):
        return list(await _handler("sessions.list")(req, **kwargs))


class TestListingRowsMissingFields:
    """A status dict need not carry every field; the defaults are load-bearing."""

    async def test_a_row_without_tags_is_filtered_not_fatal(self) -> None:
        """``s.get("tags", [])`` — without the default, set(None) raises."""
        rows = await _list([{"session_id": "a"}, {"session_id": "b", "tags": ["keep"]}], tag=["keep"])

        assert [row["session_id"] for row in rows] == ["b"]

    async def test_a_row_without_tags_survives_a_text_search(self) -> None:
        rows = await _list([{"session_id": "alpha"}], q="lph")

        assert [row["session_id"] for row in rows] == ["alpha"]

    async def test_a_row_without_a_session_id_or_display_name_is_searchable(self) -> None:
        """Both fall back to "" — a missing key must not read as the literal
        "None", which would match a search for "none"."""
        rows = await _list([{}], q="none")

        assert rows == []

    async def test_a_missing_field_reads_as_empty_not_as_a_placeholder(self) -> None:
        """The default is "", so a row without an id matches nothing. Any other
        default is itself searchable text, and a caller who happens to search
        for it gets back every row that lacks the field."""
        rows = await _list([{}], q="xxxx")

        assert rows == []

    async def test_rows_missing_the_sort_key_still_sort(self) -> None:
        """``s.get(sort_key, "")`` — without the default, sorted() compares None
        against a string and raises TypeError."""
        rows = await _list(
            [{"session_id": "b", "created_at": "2"}, {"session_id": "a"}],
            sort="created_at",
            order="asc",
        )

        assert [row["session_id"] for row in rows] == ["a", "b"]


class TestListSortWhitelistMembers:
    """Each member is only distinguishable when it disagrees with created_at."""

    @pytest.mark.parametrize("field", ["display_name", "session_id"])
    async def test_sorting_by_a_field_that_contradicts_created_at(self, field: str) -> None:
        # created_at orders these the OPPOSITE way round, so a mutation that
        # mangles this member out of the whitelist (falling back to created_at)
        # produces the reverse of what is asserted.
        first = {"session_id": "z", "display_name": "z", "created_at": "1"}
        second = {"session_id": "a", "display_name": "a", "created_at": "2"}

        rows = await _list([first, second], sort=field, order="asc")

        assert [row[field] for row in rows] == ["a", "z"]

    async def test_sorting_by_created_at_when_it_contradicts_the_id(self) -> None:
        rows = await _list(
            [{"session_id": "a", "created_at": "2"}, {"session_id": "z", "created_at": "1"}],
            sort="created_at",
            order="asc",
        )

        assert [row["session_id"] for row in rows] == ["z", "a"]

    async def test_an_unknown_sort_field_really_sorts_by_created_at(self) -> None:
        """Pins the fallback literal too: a mangled "created_at" would find no
        such key on any row, leaving the input order untouched."""
        rows = await _list(
            [{"session_id": "a", "created_at": "2"}, {"session_id": "z", "created_at": "1"}],
            sort="nonsense",
            order="asc",
        )

        assert [row["session_id"] for row in rows] == ["z", "a"]

    async def test_an_unknown_sort_field_is_never_used_as_the_key(self) -> None:
        """Inverting the membership test would sort by the caller's arbitrary
        field instead of rejecting it."""
        rows = await _list(
            [{"session_id": "a", "created_at": "2", "evil": "1"}, {"session_id": "z", "created_at": "1", "evil": "2"}],
            sort="evil",
            order="asc",
        )

        assert [row["session_id"] for row in rows] == ["z", "a"]


class TestListDefaultPageSize:
    async def test_the_default_page_is_fifty_rows(self) -> None:
        """A default of 51 is invisible on any fixture smaller than the page."""
        rows = await _list([{"session_id": f"s{i:03d}"} for i in range(60)])

        assert len(rows) == 50


# ===========================================================================
# bulk_delete — the sweep must not stop at the first skip
# ===========================================================================


async def _bulk(rows: list[dict[str, Any]], payload: dict[str, Any], *, now: float = 1000.0) -> list[str]:
    pairs = [(row, SimpleNamespace(session_id=row["session_id"])) for row in rows]
    reg = _registry(list_sessions_with_definitions=AsyncMock(return_value=pairs))
    reg.delete_session = AsyncMock()
    req = _request(registry=reg)
    with (
        patch(f"{MODULE}.model_dump", side_effect=lambda s: s),
        patch(f"{MODULE}.time.time", return_value=now),
        patch(f"{MODULE}.audit_event"),
    ):
        await _handler("sessions.bulk_delete")(req, payload)
    return [call.args[0] for call in reg.delete_session.await_args_list]


class TestBulkDeleteSweepContinues:
    """``continue``, not ``break``: one skipped row must not end the sweep."""

    async def test_a_state_mismatch_does_not_stop_the_scan(self) -> None:
        deleted = await _bulk(
            [
                {"session_id": "skipped", "lifecycle_state": "running"},
                {"session_id": "wanted", "lifecycle_state": "stopped"},
            ],
            {"filter": {"state": "stopped"}},
        )

        assert deleted == ["wanted"]

    async def test_a_too_young_session_does_not_stop_the_scan(self) -> None:
        deleted = await _bulk(
            [
                {"session_id": "fresh", "stopped_at": 999.0},
                {"session_id": "old", "stopped_at": 100.0},
            ],
            {"filter": {"older_than_s": 60}},
        )

        assert deleted == ["old"]

    async def test_a_never_stopped_session_does_not_stop_the_scan(self) -> None:
        deleted = await _bulk(
            [
                {"session_id": "running", "stopped_at": None},
                {"session_id": "old", "stopped_at": 100.0},
            ],
            {"filter": {"older_than_s": 60}},
        )

        assert deleted == ["old"]

    async def test_an_unmutatable_row_does_not_stop_the_scan(self) -> None:
        """The per-row permission check also ``continue``s. An admin normally
        passes every row, so this branch needs a refusal to reach it at all —
        and a ``break`` here would silently halve a sweep the operator believes
        completed."""
        az = _authz(admin=True)
        az.can_mutate_session = AsyncMock(side_effect=[False, True])
        pairs = [
            ({"session_id": "denied"}, SimpleNamespace(session_id="denied")),
            ({"session_id": "wanted"}, SimpleNamespace(session_id="wanted")),
        ]
        reg = _registry(list_sessions_with_definitions=AsyncMock(return_value=pairs))
        reg.delete_session = AsyncMock()
        req = _request(registry=reg, authz_obj=az)

        with (
            patch(f"{MODULE}.model_dump", side_effect=lambda s: s),
            patch(f"{MODULE}.time.time", return_value=0.0),
            patch(f"{MODULE}.audit_event"),
        ):
            await _handler("sessions.bulk_delete")(req, {})

        assert [call.args[0] for call in reg.delete_session.await_args_list] == ["wanted"]
