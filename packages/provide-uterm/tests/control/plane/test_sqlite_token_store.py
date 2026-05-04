from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
import sys
from pathlib import Path

import aiosqlite
import pytest

from provide.terminal.control.plane import ControlPlaneConfig
from provide.terminal.control.plane.sqlite import SqliteControlPlane
from provide.terminal.control.plane.token import ResumeTokenRecord, SessionTokenRecord


@pytest.mark.asyncio
async def test_sqlite_token_store_round_trip(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await plane.migrate()
    tx = await plane.begin()
    store = plane.token_store(tx)
    assert isinstance(store._conn, aiosqlite.Connection)

    session_token = SessionTokenRecord(
        session_id="s1",
        token_kind="share",
        token_value="share-token",
        created_at=1.0,
        expires_at=2.0,
        revoked_at=None,
    )
    resume_token = ResumeTokenRecord(
        token_value="resume-token",
        session_id="s1",
        role="viewer",
        created_at=1.0,
        expires_at=2.0,
        was_hijack_owner=True,
        revoked_at=None,
    )

    await store.put_session_token(session_token)
    await store.create_resume_token(resume_token)
    await tx.commit()

    tx2 = await plane.begin()
    store2 = plane.token_store(tx2)
    fetched_session = await store2.get_session_token("s1", "share")
    fetched_resume = await store2.get_resume_token("resume-token")
    await store2.revoke_resume_token("resume-token", 3.0)
    await tx2.commit()

    tx3 = await plane.begin()
    revoked_resume = await plane.token_store(tx3).get_resume_token("resume-token")
    await tx3.rollback()
    await plane.close()

    assert fetched_session == session_token
    assert fetched_resume == resume_token
    assert revoked_resume is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "token_kind", "token_value", "role"),
    [
        ("s1'; DROP TABLE cp_resume_tokens; --", "share", "tok'; DROP TABLE cp_session_tokens; --", "viewer"),
        ('s2" OR "1"="1', "control", 'tok" OR "1"="1', "admin"),
        ("semi;colon", "share", "value with -- comment", "operator"),
    ],
)
async def test_sqlite_token_store_treats_sql_like_strings_as_data(
    tmp_path: Path,
    session_id: str,
    token_kind: str,
    token_value: str,
    role: str,
) -> None:
    db_path = tmp_path / "cp.db"
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(db_path)))
    await plane.migrate()

    tx = await plane.begin()
    store = plane.token_store(tx)
    session_token = SessionTokenRecord(
        session_id=session_id,
        token_kind=token_kind,
        token_value=token_value,
        created_at=1.0,
        expires_at=2.0,
        revoked_at=None,
    )
    resume_token = ResumeTokenRecord(
        token_value=f"resume::{token_value}",
        session_id=session_id,
        role=role,
        created_at=1.0,
        expires_at=2.0,
        was_hijack_owner=False,
        revoked_at=None,
    )

    await store.put_session_token(session_token)
    await store.create_resume_token(resume_token)
    await tx.commit()

    tx2 = await plane.begin()
    fetched_session = await plane.token_store(tx2).get_session_token(session_id, token_kind)
    fetched_resume = await plane.token_store(tx2).get_resume_token(f"resume::{token_value}")
    await tx2.rollback()
    await plane.close()

    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        resume_count = conn.execute("SELECT COUNT(*) FROM cp_resume_tokens").fetchone()[0]
        session_count = conn.execute("SELECT COUNT(*) FROM cp_session_tokens").fetchone()[0]
    finally:
        conn.close()

    assert fetched_session == session_token
    assert fetched_resume == resume_token
    assert "cp_resume_tokens" in tables
    assert "cp_session_tokens" in tables
    assert resume_count == 1
    assert session_count == 1


@pytest.mark.asyncio
async def test_sqlite_control_plane_handles_concurrent_transactions(tmp_path: Path) -> None:
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=str(tmp_path / "cp.db")))
    await plane.migrate()

    async def _write_token(index: int) -> None:
        tx = await plane.begin()
        store = plane.token_store(tx)
        await store.create_resume_token(
            ResumeTokenRecord(
                token_value=f"resume-{index}",
                session_id=f"s{index}",
                role="viewer",
                created_at=float(index),
                expires_at=float(index + 10),
                was_hijack_owner=False,
                revoked_at=None,
            )
        )
        await tx.commit()

    await asyncio.gather(*(_write_token(i) for i in range(5)))

    tx = await plane.begin()
    store = plane.token_store(tx)
    fetched = [await store.get_resume_token(f"resume-{i}") for i in range(5)]
    await tx.rollback()
    await plane.close()

    assert [record.session_id if record is not None else None for record in fetched] == [f"s{i}" for i in range(5)]


def _write_resume_token_worker(db_path: str, index: int, queue) -> None:
    async def _run() -> None:
        plane = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
        await plane.migrate()
        tx = await plane.begin()
        await plane.token_store(tx).create_resume_token(
            ResumeTokenRecord(
                token_value=f"proc-{index}",
                session_id=f"proc-session-{index}",
                role="viewer",
                created_at=float(index),
                expires_at=float(index + 30),
                was_hijack_owner=False,
                revoked_at=None,
            )
        )
        await tx.commit()
        await plane.close()

    try:
        asyncio.run(_run())
    except Exception as exc:  # pragma: no cover - child process reporting path
        queue.put(("error", repr(exc)))
    else:
        queue.put(("ok", index))


def test_sqlite_control_plane_handles_multiprocess_writers(tmp_path: Path) -> None:
    if sys.platform == "darwin":
        pytest.skip("multiprocess sqlite writer test is unstable under pytest import-mode on macOS")

    db_path = str(tmp_path / "cp.db")
    plane = SqliteControlPlane(ControlPlaneConfig(database_url=db_path))
    asyncio.run(plane.migrate())
    asyncio.run(plane.close())

    ctx = multiprocessing.get_context("fork" if "fork" in multiprocessing.get_all_start_methods() else None)
    queue = ctx.Queue()
    procs = [ctx.Process(target=_write_resume_token_worker, args=(db_path, index, queue)) for index in range(4)]

    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=10)

    results = [queue.get(timeout=5) for _ in procs]
    assert all(proc.exitcode == 0 for proc in procs)
    assert sorted(result for status, result in results if status == "ok") == [0, 1, 2, 3]

    conn = sqlite3.connect(db_path)
    try:
        rows = list(conn.execute("SELECT token_value, session_id FROM cp_resume_tokens ORDER BY token_value"))
    finally:
        conn.close()

    assert rows == [
        ("proc-0", "proc-session-0"),
        ("proc-1", "proc-session-1"),
        ("proc-2", "proc-session-2"),
        ("proc-3", "proc-session-3"),
    ]
