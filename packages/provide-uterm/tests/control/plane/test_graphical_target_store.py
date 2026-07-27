#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Graphical-target store parity tests.

Every test runs against BOTH backends through the same fixture, so the memory
engine cannot silently drift from the SQLite one — the two are meant to be
interchangeable behind the registry.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from provide.uterm.control.plane import ControlPlaneConfig
from provide.uterm.control.plane.graphical_target import GraphicalTargetRecord
from provide.uterm.control.plane.memory.engine import MemoryControlPlane
from provide.uterm.control.plane.sqlite import SqliteControlPlane

if TYPE_CHECKING:
    from pathlib import Path


def _record(target_id: str = "gt-1", **overrides: Any) -> GraphicalTargetRecord:
    base: dict[str, Any] = {
        "target_id": target_id,
        "tenant_id": "acme",
        "display_name": "console",
        "protocol": "rfb",
        "width": 640,
        "height": 480,
        "created_at": 100.0,
    }
    base.update(overrides)
    return GraphicalTargetRecord(**base)


@pytest.fixture(params=["memory", "sqlite"])
async def plane(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        engine = MemoryControlPlane(ControlPlaneConfig(database_url=":memory:"))
    else:
        engine = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await engine.open()
    await engine.migrate()
    yield engine
    await engine.close()


async def _put(plane: Any, record: GraphicalTargetRecord) -> None:
    tx = await plane.begin()
    await plane.graphical_target_store(tx).put_graphical_target(record)
    await tx.commit()


@pytest.mark.asyncio
async def test_put_then_get_round_trips(plane: Any) -> None:
    await _put(plane, _record())

    tx = await plane.begin()
    got = await plane.graphical_target_store(tx).get_graphical_target("gt-1")
    await tx.rollback()

    assert got is not None
    assert got.target_id == "gt-1"
    assert got.tenant_id == "acme"
    assert got.protocol == "rfb"
    assert got.width == 640


@pytest.mark.asyncio
async def test_get_absent_returns_none(plane: Any) -> None:
    tx = await plane.begin()
    got = await plane.graphical_target_store(tx).get_graphical_target("nope")
    await tx.rollback()
    assert got is None


@pytest.mark.asyncio
async def test_put_is_upsert(plane: Any) -> None:
    await _put(plane, _record(display_name="before"))
    await _put(plane, _record(display_name="after", updated_at=200.0, updated_by="ops"))

    tx = await plane.begin()
    store = plane.graphical_target_store(tx)
    got = await store.get_graphical_target("gt-1")
    rows = await store.list_graphical_targets()
    await tx.rollback()

    assert got is not None
    assert got.display_name == "after"
    assert got.updated_at == 200.0
    assert got.updated_by == "ops"
    # Upsert, not insert — exactly one row survives.
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_list_is_ordered_by_target_id(plane: Any) -> None:
    for target_id in ("gt-c", "gt-a", "gt-b"):
        await _put(plane, _record(target_id))

    tx = await plane.begin()
    rows = await plane.graphical_target_store(tx).list_graphical_targets()
    await tx.rollback()

    assert [r.target_id for r in rows] == ["gt-a", "gt-b", "gt-c"]


@pytest.mark.asyncio
async def test_delete_reports_whether_a_row_went(plane: Any) -> None:
    await _put(plane, _record())

    tx = await plane.begin()
    store = plane.graphical_target_store(tx)
    first = await store.delete_graphical_target("gt-1")
    second = await store.delete_graphical_target("gt-1")
    await tx.commit()

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_nullable_and_config_fields_round_trip(plane: Any) -> None:
    await _put(
        plane,
        _record(
            endpoint="host:5900",
            secret="s3cret",  # pragma: allowlist secret
            ca_secret_ref="env:CA",  # pragma: allowlist secret
            client_cert_secret_ref="file:/tmp/cert.pem",  # pragma: allowlist secret
            client_key_secret_ref=None,
            config={"vm_name": "vm-1", "nested": {"k": 1}},
            created_by="alice",
            is_static=True,
        ),
    )

    tx = await plane.begin()
    got = await plane.graphical_target_store(tx).get_graphical_target("gt-1")
    await tx.rollback()

    assert got is not None
    assert got.endpoint == "host:5900"
    assert got.secret == "s3cret"  # pragma: allowlist secret
    assert got.ca_secret_ref == "env:CA"  # pragma: allowlist secret
    assert got.client_cert_secret_ref == "file:/tmp/cert.pem"  # pragma: allowlist secret
    assert got.client_key_secret_ref is None
    assert got.config == {"vm_name": "vm-1", "nested": {"k": 1}}
    assert got.created_by == "alice"
    assert got.is_static is True
    assert got.is_system is False


@pytest.mark.asyncio
async def test_rollback_discards_the_write(plane: Any) -> None:
    tx = await plane.begin()
    await plane.graphical_target_store(tx).put_graphical_target(_record())
    await tx.rollback()

    tx = await plane.begin()
    got = await plane.graphical_target_store(tx).get_graphical_target("gt-1")
    await tx.rollback()

    assert got is None


@pytest.mark.asyncio
async def test_sqlite_survives_reopen(tmp_path: Path) -> None:
    """The whole point of the table: targets outlive the process."""
    db_path = str(tmp_path / "cp.db")

    plane = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
    await plane.open()
    await plane.migrate()
    await _put(plane, _record(config={"vm_name": "vm-1"}))
    await plane.close()

    reopened = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
    await reopened.open()
    await reopened.migrate()
    tx = await reopened.begin()
    got = await reopened.graphical_target_store(tx).get_graphical_target("gt-1")
    await tx.rollback()
    await reopened.close()

    assert got is not None
    assert got.config == {"vm_name": "vm-1"}


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("not-json", id="undecodable"),
        # Valid JSON that simply isn't an object. json.loads succeeds here, so this
        # takes a different branch than the decode failure above — config must still
        # come back as a dict rather than a list/str leaking into the record.
        pytest.param("[1, 2, 3]", id="json-array"),
        pytest.param('"a string"', id="json-string"),
        pytest.param("42", id="json-number"),
        pytest.param("null", id="json-null"),
    ],
)
@pytest.mark.asyncio
async def test_sqlite_non_object_config_degrades_to_empty(tmp_path: Path, stored: str) -> None:
    """A config blob that isn't a JSON object must not take out the whole listing."""
    db_path = str(tmp_path / "cp.db")
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
    await plane.open()
    await plane.migrate()
    await _put(plane, _record())
    await plane.close()

    raw = sqlite3.connect(db_path)
    try:
        raw.execute("UPDATE cp_graphical_targets SET config = ? WHERE target_id = ?", (stored, "gt-1"))
        raw.commit()
    finally:
        raw.close()

    reopened = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
    await reopened.open()
    tx = await reopened.begin()
    rows = await reopened.graphical_target_store(tx).list_graphical_targets()
    await tx.rollback()
    await reopened.close()

    assert len(rows) == 1
    assert rows[0].config == {}


@pytest.mark.asyncio
async def test_sqlite_undecodable_config_degrades_to_empty(tmp_path: Path) -> None:
    """A corrupt config blob must not take out the whole listing."""
    db_path = str(tmp_path / "cp.db")
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
    await plane.open()
    await plane.migrate()
    await _put(plane, _record())
    await plane.close()

    raw = sqlite3.connect(db_path)
    try:
        raw.execute("UPDATE cp_graphical_targets SET config = ? WHERE target_id = ?", ("not-json", "gt-1"))
        raw.commit()
    finally:
        raw.close()

    reopened = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
    await reopened.open()
    tx = await reopened.begin()
    rows = await reopened.graphical_target_store(tx).list_graphical_targets()
    await tx.rollback()
    await reopened.close()

    assert len(rows) == 1
    assert rows[0].config == {}
