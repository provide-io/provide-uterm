#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/sessions.py``: ``list_sessions`` + ``bulk_delete``.

``sessions.py`` sits at 46.12% — the table-driven suite in
``test_routes_capability_mutation_killing.py`` killed ~440 mutants of the shared
handler skeleton (authorize → 403 → 404 → forward the id), but has no purchase
on the handler *bodies*. These two carry the most body logic in the module.

Split across three files for the 777-line cap: this one, ``_crud_`` (create /
get / patch / delete / lifecycle) and ``_reads_`` (annotate / analyze / snapshot
/ events / recording).

What is load-bearing here:

* **Listing is an authorization boundary, not a view.** Every row is filtered by
  ``can_read_session`` BEFORE any query filter runs; a mutation that reorders or
  drops that check leaks other tenants' sessions through the search box.
* **Bulk delete is admin-only and filter-driven.** The ``older_than_s`` and
  ``state`` filters decide what gets destroyed, so an inverted comparison
  deletes the sessions the operator meant to keep.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

MODULE = "provide.uterm.server.routes.sessions"


def _handler(name: str) -> Any:
    from provide.uterm.server.routes.sessions import session_capability_handlers

    return session_capability_handlers()[name]


def _principal(subject_id: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject_id, roles=set())


def _authz(*, can_read: bool = True, admin: bool = True, can_mutate: bool = True) -> MagicMock:
    az = MagicMock(name="authz")
    az.can_read_session = AsyncMock(return_value=can_read)
    az.can_mutate_session = AsyncMock(return_value=can_mutate)
    az.is_admin = AsyncMock(return_value=admin)
    return az


def _request(*, registry: Any, authz_obj: Any, principal: Any = None, client_host: str = "1.2.3.4") -> MagicMock:
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(uterm_registry=registry, uterm_authz=authz_obj)
    req.state = SimpleNamespace(uterm_principal=principal if principal is not None else _principal())
    req.client = SimpleNamespace(host=client_host)
    return req


def _row(session_id: str, **fields: Any) -> dict[str, Any]:
    """A listing row. ``model_dump`` is patched to identity, so this IS the status."""
    row = {"session_id": session_id, "display_name": session_id, "tags": [], "created_at": session_id}
    row.update(fields)
    return row


def _pairs(*rows: dict[str, Any]) -> list[tuple[dict[str, Any], Any]]:
    return [(row, SimpleNamespace(session_id=row["session_id"])) for row in rows]


def _registry_listing(*rows: dict[str, Any]) -> MagicMock:
    reg = MagicMock(name="registry")
    reg.list_sessions_with_definitions = AsyncMock(return_value=_pairs(*rows))
    reg.delete_session = AsyncMock()
    return reg


async def _list(rows: list[dict[str, Any]], *, authz_obj: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
    reg = _registry_listing(*rows)
    req = _request(registry=reg, authz_obj=authz_obj if authz_obj is not None else _authz())
    with patch(f"{MODULE}.model_dump", side_effect=lambda status: status):
        result = await _handler("sessions.list")(req, **kwargs)
    return list(result)


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["session_id"] for row in rows]


# ===========================================================================
# list_sessions — authorization
# ===========================================================================


class TestListAuthorizationFilter:
    async def test_only_readable_sessions_are_returned(self) -> None:
        az = _authz()
        az.can_read_session = AsyncMock(side_effect=lambda _p, d: d.session_id == "mine")

        rows = await _list([_row("mine"), _row("theirs")], authz_obj=az)

        assert _ids(rows) == ["mine"]

    async def test_readability_is_asked_about_the_calling_principal(self) -> None:
        az = _authz()
        principal = _principal("bob")
        reg = _registry_listing(_row("s1"))
        req = _request(registry=reg, authz_obj=az, principal=principal)

        with patch(f"{MODULE}.model_dump", side_effect=lambda status: status):
            await _handler("sessions.list")(req)

        assert az.can_read_session.await_args.args[0] is principal

    async def test_an_unreadable_listing_is_empty_not_an_error(self) -> None:
        rows = await _list([_row("a"), _row("b")], authz_obj=_authz(can_read=False))

        assert rows == []


# ===========================================================================
# list_sessions — filters
# ===========================================================================


class TestListFilters:
    async def test_tag_filter_keeps_any_overlap(self) -> None:
        rows = await _list(
            [_row("a", tags=["prod", "db"]), _row("b", tags=["dev"]), _row("c", tags=[])],
            tag=["prod", "stage"],
        )

        assert _ids(rows) == ["a"]

    async def test_no_tag_filter_keeps_everything(self) -> None:
        rows = await _list([_row("a", tags=["x"]), _row("b", tags=[])], tag=None)

        assert sorted(_ids(rows)) == ["a", "b"]

    async def test_an_empty_tag_list_is_not_a_filter(self) -> None:
        """Falsy, so it must not intersect to nothing and empty the listing."""
        rows = await _list([_row("a", tags=["x"])], tag=[])

        assert _ids(rows) == ["a"]

    async def test_connector_type_matches_exactly(self) -> None:
        rows = await _list(
            [_row("a", connector_type="ssh"), _row("b", connector_type="telnet")],
            connector_type="ssh",
        )

        assert _ids(rows) == ["a"]

    async def test_visibility_matches_exactly(self) -> None:
        rows = await _list(
            [_row("a", visibility="private"), _row("b", visibility="operator")],
            visibility="operator",
        )

        assert _ids(rows) == ["b"]

    async def test_state_matches_the_lifecycle_field(self) -> None:
        rows = await _list(
            [_row("a", lifecycle_state="running"), _row("b", lifecycle_state="stopped")],
            state="stopped",
        )

        assert _ids(rows) == ["b"]

    async def test_filters_compose(self) -> None:
        rows = await _list(
            [
                _row("a", connector_type="ssh", visibility="private"),
                _row("b", connector_type="ssh", visibility="operator"),
            ],
            connector_type="ssh",
            visibility="operator",
        )

        assert _ids(rows) == ["b"]


class TestListSearch:
    async def test_search_matches_the_session_id(self) -> None:
        rows = await _list([_row("alpha"), _row("beta")], q="lph")

        assert _ids(rows) == ["alpha"]

    async def test_search_matches_the_display_name(self) -> None:
        rows = await _list([_row("a", display_name="Production box"), _row("b", display_name="dev")], q="production")

        assert _ids(rows) == ["a"]

    async def test_search_matches_a_tag(self) -> None:
        rows = await _list([_row("a", tags=["prod"]), _row("b", tags=["dev"])], q="pro")

        assert _ids(rows) == ["a"]

    async def test_search_is_case_insensitive_on_both_sides(self) -> None:
        rows = await _list([_row("ALPHA", display_name="X")], q="alpha")

        assert _ids(rows) == ["ALPHA"]

    async def test_search_is_a_substring_not_a_prefix(self) -> None:
        rows = await _list([_row("web-alpha-1")], q="alpha")

        assert _ids(rows) == ["web-alpha-1"]

    async def test_a_search_matching_nothing_returns_nothing(self) -> None:
        rows = await _list([_row("a"), _row("b")], q="zzz")

        assert rows == []


# ===========================================================================
# list_sessions — sort and pagination
# ===========================================================================


class TestListSort:
    @pytest.mark.parametrize("field", ["created_at", "display_name", "session_id"])
    async def test_every_allowed_sort_field_is_honoured(self, field: str) -> None:
        # Built then mutated rather than passed as a kwarg: `field` may be
        # "session_id", which _row() already takes positionally.
        high, low = _row("b"), _row("a")
        high[field], low[field] = "2", "1"

        rows = await _list([high, low], sort=field, order="asc")

        assert [row[field] for row in rows] == ["1", "2"]

    async def test_an_unknown_sort_field_falls_back_to_created_at(self) -> None:
        """A whitelist, not a passthrough: sorting by an arbitrary key would let
        a caller order by a field the row may not carry."""
        rows = await _list(
            [_row("b", created_at="1"), _row("a", created_at="2")],
            sort="; drop",
            order="asc",
        )

        assert _ids(rows) == ["b", "a"]

    async def test_descending_is_the_default(self) -> None:
        rows = await _list([_row("a"), _row("b")])

        assert _ids(rows) == ["b", "a"]

    async def test_only_asc_reverses_the_default(self) -> None:
        rows = await _list([_row("a"), _row("b")], order="asc")

        assert _ids(rows) == ["a", "b"]

    async def test_an_unrecognised_order_stays_descending(self) -> None:
        """``order != "asc"`` — anything that is not exactly "asc" is descending."""
        rows = await _list([_row("a"), _row("b")], order="ASC")

        assert _ids(rows) == ["b", "a"]


class TestListPagination:
    async def test_the_limit_caps_the_page(self) -> None:
        rows = await _list([_row(f"s{i}") for i in range(5)], order="asc", limit=2)

        assert _ids(rows) == ["s0", "s1"]

    async def test_the_offset_skips_from_the_start(self) -> None:
        rows = await _list([_row(f"s{i}") for i in range(5)], order="asc", offset=2, limit=2)

        assert _ids(rows) == ["s2", "s3"]

    async def test_the_window_is_offset_plus_limit_not_limit_alone(self) -> None:
        """``results[offset : offset + limit]`` — dropping the addition would
        return a short page, or none at all once offset exceeds limit."""
        rows = await _list([_row(f"s{i}") for i in range(10)], order="asc", offset=4, limit=3)

        assert _ids(rows) == ["s4", "s5", "s6"]

    async def test_an_offset_past_the_end_is_empty(self) -> None:
        rows = await _list([_row("s0")], offset=5)

        assert rows == []


# ===========================================================================
# bulk_delete_sessions
# ===========================================================================


async def _bulk(
    rows: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    authz_obj: Any = None,
    now: float = 1000.0,
) -> tuple[dict[str, int], MagicMock, MagicMock]:
    reg = _registry_listing(*rows)
    audit = MagicMock()
    req = _request(registry=reg, authz_obj=authz_obj if authz_obj is not None else _authz())
    with (
        patch(f"{MODULE}.model_dump", side_effect=lambda status: status),
        patch(f"{MODULE}.time.time", return_value=now),
        patch(f"{MODULE}.audit_event", audit),
    ):
        result = await _handler("sessions.bulk_delete")(req, payload)
    return result, reg, audit


def _deleted(reg: MagicMock) -> list[str]:
    return [call.args[0] for call in reg.delete_session.await_args_list]


class TestBulkDeleteAuthorization:
    async def test_a_non_admin_is_refused(self) -> None:
        reg = _registry_listing(_row("a"))
        req = _request(registry=reg, authz_obj=_authz(admin=False))

        with pytest.raises(HTTPException) as exc:
            await _handler("sessions.bulk_delete")(req, {})

        assert exc.value.status_code == 403
        assert exc.value.detail == "admin privileges required for bulk delete"
        reg.delete_session.assert_not_awaited()

    async def test_adminness_is_asked_about_the_calling_principal(self) -> None:
        az = _authz()
        principal = _principal("root")
        reg = _registry_listing()
        req = _request(registry=reg, authz_obj=az, principal=principal)

        with patch(f"{MODULE}.model_dump", side_effect=lambda s: s), patch(f"{MODULE}.audit_event"):
            await _handler("sessions.bulk_delete")(req, {})

        az.is_admin.assert_awaited_once_with(principal)


class TestBulkDeleteFilters:
    async def test_no_filter_deletes_everything_readable(self) -> None:
        result, reg, _audit = await _bulk([_row("a"), _row("b")], {})

        assert _deleted(reg) == ["a", "b"]
        assert result == {"deleted": 2}

    async def test_the_state_filter_selects_by_lifecycle(self) -> None:
        result, reg, _audit = await _bulk(
            [_row("a", lifecycle_state="stopped"), _row("b", lifecycle_state="running")],
            {"filter": {"state": "stopped"}},
        )

        assert _deleted(reg) == ["a"]
        assert result == {"deleted": 1}

    async def test_a_blank_state_filter_is_no_filter(self) -> None:
        """``.strip() or None`` — whitespace must not match a real state."""
        _result, reg, _audit = await _bulk(
            [_row("a", lifecycle_state="stopped"), _row("b", lifecycle_state="running")],
            {"filter": {"state": "   "}},
        )

        assert _deleted(reg) == ["a", "b"]

    async def test_older_than_keeps_sessions_stopped_too_recently(self) -> None:
        _result, reg, _audit = await _bulk(
            [_row("old", stopped_at=100.0), _row("fresh", stopped_at=990.0)],
            {"filter": {"older_than_s": 60}},
            now=1000.0,
        )

        assert _deleted(reg) == ["old"]

    async def test_a_session_exactly_at_the_age_boundary_is_deleted(self) -> None:
        """``< older_than_s`` continues, so an age of exactly the threshold
        qualifies. An inverted comparison would spare it and delete the rest."""
        _result, reg, _audit = await _bulk(
            [_row("edge", stopped_at=940.0)],
            {"filter": {"older_than_s": 60}},
            now=1000.0,
        )

        assert _deleted(reg) == ["edge"]

    async def test_a_never_stopped_session_is_never_aged_out(self) -> None:
        _result, reg, _audit = await _bulk(
            [_row("running", stopped_at=None)],
            {"filter": {"older_than_s": 1}},
        )

        assert _deleted(reg) == []

    async def test_state_and_age_filters_compose(self) -> None:
        _result, reg, _audit = await _bulk(
            [
                _row("a", lifecycle_state="stopped", stopped_at=100.0),
                _row("b", lifecycle_state="stopped", stopped_at=999.0),
                _row("c", lifecycle_state="running", stopped_at=100.0),
            ],
            {"filter": {"state": "stopped", "older_than_s": 60}},
            now=1000.0,
        )

        assert _deleted(reg) == ["a"]


class TestBulkDeleteAudit:
    async def test_the_audit_names_every_deleted_session(self) -> None:
        _result, _reg, audit = await _bulk([_row("a"), _row("b")], {"filter": {}})

        audit.assert_called_once_with(
            "session.bulk_delete",
            principal="alice",
            session_id="a,b",
            source_ip="1.2.3.4",
            detail={"count": 2, "filter": {}},
        )

    async def test_deleting_nothing_writes_no_audit_record(self) -> None:
        """An empty sweep is not an event; logging it would bury the real ones."""
        result, _reg, audit = await _bulk([], {})

        assert result == {"deleted": 0}
        audit.assert_not_called()
