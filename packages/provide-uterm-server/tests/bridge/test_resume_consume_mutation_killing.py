#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for the resume-token store's ``consume`` path.

Covers ``ControlPlaneResumeStore.consume`` (the single-use, backend-adapter
implementation), and the small, adjacent ``InMemoryResumeStore.create`` /
``InMemoryResumeStore.consume`` gaps that mutmut also found.

Kill-suite only. Behavioural coverage lives in ``test_resume.py``, which
drives ``ControlPlaneResumeStore`` against a *real* control-plane backend
(``bootstrap_control_plane`` with the ``memory``/``sqlite`` backends) and
``InMemoryResumeStore`` directly. That suite proves the resume flow works
end to end, but it never isolates ``consume()``'s own arithmetic and field
construction from the real backend's behaviour, which is exactly why these
mutants survive:

* Nothing seeds the store's private ``_created_at_mono`` ledger and then
  asserts it was pruned by the *token actually looked up* -- every one of
  ``consume()``'s five ``self._created_at_mono.pop(token, None)`` call
  sites can have its key swapped for the literal ``None``, or its ``None``
  default dropped outright (turning a harmless no-op into a ``KeyError``
  when the ledger genuinely lacks the entry, e.g. after a restart), with no
  existing test noticing either way.
* Nothing exercises the ``created_at`` fallback (``self._created_at_mono
  .get(token, now_mono - age_s)``) with the ledger entry deliberately
  *absent*, so the "recompute from wall-clock age" arithmetic
  (``age_s = max(0.0, now_wall - record.created_at)``) is never isolated
  from the common "ledger has it" path; every existing test creates the
  token through the same store instance, so the ledger is always populated
  and the fallback expression is never the one actually used.
* Nothing checks the exact boundary of ``now_wall > float(record
  .expires_at)`` (a token expiring in this same instant must still be
  honoured -- ``>``, not ``>=``), mirroring the exact-boundary tests
  ``test_resume.py`` already has for ``InMemoryResumeStore.get`` /
  ``cleanup_expired`` / ``active_tokens``, but missing for *either* store's
  ``consume``.
* Nothing asserts every field of the ``ResumeSession`` that ``consume()``
  builds against the *record*'s actual values in one place, so a
  substituted ``None``, a dropped constructor kwarg (silently falling back
  to the dataclass's own default), or ``str(record.token_value)`` mangled
  into ``str(None)`` all produce a session whose fields no test compares
  against anything more specific than "is not None" or a couple of
  incidental attributes.
* Nothing pins ``InMemoryResumeStore.create``'s ``wall_created_at``
  (``time.time()``) the way it pins ``created_at``/``expires_at``
  (``time.monotonic()``), so a ``None`` substitution or a dropped kwarg
  there is invisible.

Every fake below is a strict recorder -- never ``AsyncMock(return_value=
...)`` -- so a substituted or dropped argument is visible either as a
raised assertion or as a mismatch against the recorded call. The module's
``time.time()``/``time.monotonic()`` are pinned via a ``SimpleNamespace``
substituted for the ``resume`` module's ``time`` reference (the same
pattern ``test_handle_resume_gates_mutation_killing.py`` uses for
``browser_handlers.time``), so every expiry/age boundary is exact rather
than a race against the real clock.

Measured against ``mutants/.../resume.py`` on 2026-09-23, this suite closes
all 27 surviving ``ControlPlaneResumeStore.consume__mutmut_*`` mutants (ids
7, 12, 14, 16, 22, 23, 25, 26, 27, 28, 29, 30, 31, 32, 37, 38, 40, 42, 43,
46, 47, 48, 49, 55, 56, 57, 60), both surviving real
``InMemoryResumeStore.create__mutmut_*`` mutants (11, 17), and the one
surviving ``InMemoryResumeStore.consume__mutmut_6``.

Documented equivalents:

- ``InMemoryResumeStore.create__mutmut_2``: ``secrets.token_urlsafe(32)`` ->
  ``secrets.token_urlsafe(None)``. CPython's ``secrets.token_urlsafe`` (and
  the ``secrets.token_bytes`` it delegates to) already default their
  ``nbytes`` parameter to ``None``, mapping that ``None`` internally to
  ``secrets.DEFAULT_ENTROPY`` (32) before calling ``os.urandom(nbytes)``.
  ``token_urlsafe(32)`` and ``token_urlsafe(None)`` therefore both resolve
  to ``os.urandom(32)`` -- the identical call, with the identical output
  distribution and the identical 43-character encoded length -- so no
  input or observation distinguishes them. (Confirmed directly:
  ``inspect.signature(secrets.token_urlsafe)`` is already ``(nbytes=None)``,
  and both calls produce 43-character tokens.)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.control.plane.token.types import ResumeTokenRecord
from provide.uterm.server.bridge.hub import resume as resume_module
from provide.uterm.server.bridge.hub.resume import (
    ControlPlaneResumeStore,
    InMemoryResumeStore,
)

TOKEN = "tok-1"
WORKER = "worker-1"
ROLE = "admin"


def _pin_clock(monkeypatch: pytest.MonkeyPatch, *, wall: float, mono: float) -> None:
    """Pin the store module's ``time.time()``/``time.monotonic()`` so every
    expiry/age computation in ``consume()`` is exact, not a race against the
    real clock."""
    monkeypatch.setattr(resume_module, "time", SimpleNamespace(time=lambda: wall, monotonic=lambda: mono))


def _record(
    *,
    token_value: str = TOKEN,
    session_id: str = WORKER,
    role: str = ROLE,
    created_at: float,
    expires_at: float,
    was_hijack_owner: bool = False,
) -> ResumeTokenRecord:
    return ResumeTokenRecord(
        token_value=token_value,
        session_id=session_id,
        role=role,
        created_at=created_at,
        expires_at=expires_at,
        was_hijack_owner=was_hijack_owner,
    )


class _Tx:
    """Strict fake transaction handle: records whether it was committed or
    rolled back, exactly like the real control-plane transaction."""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _TokenStore:
    """Strict fake of ``_ControlPlaneResumeTokenStore``. Every call is
    recorded and every argument's type is asserted, mirroring the real
    Protocol's signatures exactly -- never ``AsyncMock(return_value=...)``,
    which would answer identically no matter what ``consume()`` actually
    passed it."""

    def __init__(self, *, consume_result: ResumeTokenRecord | None = None) -> None:
        self.create_calls: list[ResumeTokenRecord] = []
        self.get_calls: list[str] = []
        self.revoke_calls: list[tuple[str, float]] = []
        self.consume_calls: list[tuple[str, float]] = []
        self._consume_result = consume_result

    async def create_resume_token(self, record: ResumeTokenRecord) -> None:
        assert isinstance(record, ResumeTokenRecord), f"create_resume_token got record={record!r}"
        self.create_calls.append(record)

    async def get_resume_token(self, token_value: str) -> ResumeTokenRecord | None:
        assert isinstance(token_value, str), f"get_resume_token got token_value={token_value!r}"
        self.get_calls.append(token_value)
        return None

    async def revoke_resume_token(self, token_value: str, revoked_at: float) -> None:
        assert isinstance(token_value, str), f"revoke_resume_token got token_value={token_value!r}"
        assert isinstance(revoked_at, float), f"revoke_resume_token got revoked_at={revoked_at!r}"
        self.revoke_calls.append((token_value, revoked_at))

    async def consume_resume_token(self, token_value: str, revoked_at: float) -> ResumeTokenRecord | None:
        assert isinstance(token_value, str), f"consume_resume_token got token_value={token_value!r}"
        assert isinstance(revoked_at, float), f"consume_resume_token got revoked_at={revoked_at!r}"
        self.consume_calls.append((token_value, revoked_at))
        return self._consume_result


class _Backend:
    """Strict fake of ``_ControlPlaneResumeBackend``: hands out a fresh
    ``_Tx`` per ``begin()`` and only ever serves ``token_store`` for a
    ``tx`` it actually issued."""

    def __init__(self, store: _TokenStore) -> None:
        self._store = store
        self.begin_calls = 0
        self.txs: list[_Tx] = []

    async def begin(self) -> _Tx:
        self.begin_calls += 1
        tx = _Tx()
        self.txs.append(tx)
        return tx

    def token_store(self, tx: Any) -> _TokenStore:
        assert tx in self.txs, f"token_store got an unknown tx={tx!r}"
        return self._store


# ---------------------------------------------------------------------------
# ControlPlaneResumeStore.consume
# ---------------------------------------------------------------------------


async def test_consume_missing_record_prunes_ledger_by_the_real_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills consume__mutmut_7: ``self._created_at_mono.pop(token, None)`` ->
    ``pop(None, None)`` in the ``record is None`` branch. Seed the ledger
    with the real token before consuming a token the backend reports as
    unknown; the real code must evict that entry by looking it up under
    ``token``, not under the literal ``None`` (which is never a key)."""
    _pin_clock(monkeypatch, wall=1000.0, mono=50.0)
    token_store = _TokenStore(consume_result=None)
    backend = _Backend(token_store)
    store = ControlPlaneResumeStore(backend)
    store._created_at_mono[TOKEN] = 111.0

    result = await store.consume(TOKEN)

    assert result is None
    assert TOKEN not in store._created_at_mono
    assert token_store.consume_calls == [(TOKEN, 1000.0)]


async def test_consume_at_exact_expiry_boundary_is_not_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills consume__mutmut_12: ``now_wall > float(record.expires_at)`` ->
    ``>=``. At the exact boundary (``now_wall == record.expires_at``) the
    real code must NOT treat the token as expired."""
    _pin_clock(monkeypatch, wall=1000.0, mono=50.0)
    record = _record(created_at=970.0, expires_at=1000.0)  # expires_at == now_wall exactly
    token_store = _TokenStore(consume_result=record)
    backend = _Backend(token_store)
    store = ControlPlaneResumeStore(backend)

    result = await store.consume(TOKEN)

    assert result is not None
    assert result.worker_id == WORKER


async def test_consume_expired_record_prunes_ledger_by_the_real_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills consume__mutmut_14: ``pop(token, None)`` -> ``pop(None, None)``
    in the expired-record branch. Seed the ledger with the real token; the
    real code must evict it by ``token``."""
    _pin_clock(monkeypatch, wall=1000.0, mono=50.0)
    record = _record(created_at=900.0, expires_at=999.0)  # expired: expires_at < now_wall
    token_store = _TokenStore(consume_result=record)
    backend = _Backend(token_store)
    store = ControlPlaneResumeStore(backend)
    store._created_at_mono[TOKEN] = 111.0

    result = await store.consume(TOKEN)

    assert result is None
    assert TOKEN not in store._created_at_mono


async def test_consume_expired_record_without_a_ledger_entry_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills consume__mutmut_16: ``pop(token, None)`` -> ``pop(token, )``
    (the ``None`` default dropped). With no ledger entry for this token
    (e.g. after a process restart, mirroring the sqlite-restart tests in
    ``test_resume.py``), the real code's ``pop(token, None)`` is a silent
    no-op; the mutant's bare ``pop(token)`` raises ``KeyError`` instead."""
    _pin_clock(monkeypatch, wall=1000.0, mono=50.0)
    record = _record(created_at=900.0, expires_at=999.0)
    token_store = _TokenStore(consume_result=record)
    backend = _Backend(token_store)
    store = ControlPlaneResumeStore(backend)
    # Deliberately do NOT seed store._created_at_mono[TOKEN].

    result = await store.consume(TOKEN)

    assert result is None


async def test_consume_recomputes_created_at_from_age_when_ledger_entry_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills consume__mutmut_22/23/25/27/28/29/30: every mutation to the
    fallback ``age_s = max(0.0, now_wall - float(record.created_at))`` and
    ``created_at = self._created_at_mono.get(token, now_mono - age_s)``, plus
    __mutmut_46 (``created_at=created_at`` -> ``created_at=None`` in the
    returned ``ResumeSession``, which this test's exact comparison also
    catches). That fallback is only observable when the in-process ledger
    does not have the token (e.g. after a restart), so it must not be
    pre-seeded. A small (< 1s) remaining age distinguishes ``max(0.0, ...)``
    from ``max(1.0, ...)``, and comparing the exact expected float rules out
    the ``+``-for-``-`` swaps and the outright ``None``/always-``None``
    variants (``.get`` called with the wrong key, with the default omitted,
    or with the ``token`` positional argument dropped so a float is looked up
    as a key instead)."""
    _pin_clock(monkeypatch, wall=1000.0, mono=500.0)
    record = _record(created_at=999.7, expires_at=1030.0)  # age = 0.3s
    token_store = _TokenStore(consume_result=record)
    backend = _Backend(token_store)
    store = ControlPlaneResumeStore(backend)
    # Deliberately do NOT seed store._created_at_mono[TOKEN].

    result = await store.consume(TOKEN)

    assert result is not None
    # age_s = max(0.0, 1000.0 - 999.7) ~= 0.3; created_at = now_mono - age_s.
    assert result.created_at == pytest.approx(500.0 - 0.3)


async def test_consume_uses_the_ledger_value_for_created_at_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills consume__mutmut_26: ``self._created_at_mono.get(token, ...)``
    -> ``.get(None, ...)``. Seed the ledger with a value distinct from the
    recomputed fallback so that only a lookup keyed by the real ``token``
    can find it; a lookup keyed by the literal ``None`` finds nothing and
    silently falls through to the (here, very different) fallback value."""
    _pin_clock(monkeypatch, wall=1000.0, mono=500.0)
    record = _record(created_at=999.7, expires_at=1030.0)  # fallback would be ~499.7
    token_store = _TokenStore(consume_result=record)
    backend = _Backend(token_store)
    store = ControlPlaneResumeStore(backend)
    store._created_at_mono[TOKEN] = 12345.0  # distinct from the ~499.7 fallback

    result = await store.consume(TOKEN)

    assert result is not None
    assert result.created_at == 12345.0


async def test_consume_computes_expires_at_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills consume__mutmut_31 (``expires_at = None``), 32 (``now_mono +
    ...`` -> ``now_mono - ...``), 37 (``max(0.0, ...)`` -> ``max(1.0,
    ...)``) and 38 (``float(record.expires_at) - now_wall`` -> ``+``), plus
    __mutmut_47 (``expires_at=expires_at`` -> ``expires_at=None`` in the
    returned ``ResumeSession``, which this test's exact comparison also
    catches). A small (< 1s) remaining lifetime distinguishes the
    ``max(0.0, ...)`` clamp from ``max(1.0, ...)``, and the exact expected
    value rules out the sign flip and the ``None`` short-circuit."""
    _pin_clock(monkeypatch, wall=1000.0, mono=500.0)
    record = _record(created_at=999.9, expires_at=1000.2)  # 0.2s remaining
    token_store = _TokenStore(consume_result=record)
    backend = _Backend(token_store)
    store = ControlPlaneResumeStore(backend)

    result = await store.consume(TOKEN)

    assert result is not None
    assert result.expires_at == pytest.approx(500.2)


async def test_consume_prunes_ledger_after_a_successful_consume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills consume__mutmut_40: ``pop(token, None)`` -> ``pop(None, None)``
    in the success path's final cleanup. Seed the ledger with the real
    token; a successful consume must evict it by ``token``."""
    _pin_clock(monkeypatch, wall=1000.0, mono=500.0)
    record = _record(created_at=990.0, expires_at=1030.0)
    token_store = _TokenStore(consume_result=record)
    backend = _Backend(token_store)
    store = ControlPlaneResumeStore(backend)
    store._created_at_mono[TOKEN] = 111.0

    result = await store.consume(TOKEN)

    assert result is not None
    assert TOKEN not in store._created_at_mono


async def test_consume_builds_the_session_with_the_records_exact_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills consume__mutmut_43 (``token=None``), 48 (``was_hijack_owner=
    None``), 49 (``wall_created_at=None``), 55 (the ``was_hijack_owner=...``
    kwarg dropped, falling back to the dataclass's ``False`` default), 56
    (the ``wall_created_at=...`` kwarg dropped, falling back to the
    dataclass's ``0.0`` default), 57 (``str(record.token_value)`` ->
    ``str(None)``) and 60 (``bool(record.was_hijack_owner)`` ->
    ``bool(None)``). (mutmut_46/47, the equivalent ``created_at=None``/
    ``expires_at=None`` substitutions on this same constructor call, are
    killed by ``test_consume_recomputes_created_at_from_age_when_ledger_entry_is_missing``
    and ``test_consume_computes_expires_at_exactly`` respectively, which
    assert those two fields exactly; this test does not re-assert them.) A
    record with ``was_hijack_owner=True`` and a distinctive, non-zero
    ``created_at`` (the returned session's ``wall_created_at``) makes every
    one of those substitutions or omissions observable against its real,
    non-default value."""
    _pin_clock(monkeypatch, wall=1000.0, mono=500.0)
    record = _record(
        token_value=TOKEN,
        session_id=WORKER,
        role=ROLE,
        created_at=12345.5,
        expires_at=1030.0,
        was_hijack_owner=True,
    )
    token_store = _TokenStore(consume_result=record)
    backend = _Backend(token_store)
    store = ControlPlaneResumeStore(backend)

    result = await store.consume(TOKEN)

    assert result is not None
    assert result.token == TOKEN
    assert result.worker_id == WORKER
    assert result.role == ROLE
    assert result.was_hijack_owner is True
    assert result.wall_created_at == 12345.5


# ---------------------------------------------------------------------------
# InMemoryResumeStore.create / InMemoryResumeStore.consume
# ---------------------------------------------------------------------------


async def test_inmemory_create_stores_the_pinned_wall_clock_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills InMemoryResumeStore.create__mutmut_11 (``wall_created_at=
    None``) and __mutmut_17 (the ``wall_created_at=...`` kwarg dropped
    entirely, falling back to the dataclass's ``0.0`` default). Pin
    ``time.time()`` to a distinctive non-zero value and assert the stored
    session carries it exactly."""
    _pin_clock(monkeypatch, wall=54321.5, mono=10.0)
    store = InMemoryResumeStore()

    token = await store.create("w1", "admin", 60.0)

    session = store._tokens[token]
    assert session.wall_created_at == 54321.5


async def test_inmemory_consume_at_exact_expiry_boundary_is_not_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills InMemoryResumeStore.consume__mutmut_6: ``time.monotonic() >
    session.expires_at`` -> ``>=``. At the exact boundary the real code
    must still return the session; the mutant would wrongly report it
    expired."""
    _pin_clock(monkeypatch, wall=1000.0, mono=100.0)
    store = InMemoryResumeStore()
    token = await store.create("w1", "admin", 30.0)  # expires_at = 130.0

    _pin_clock(monkeypatch, wall=1030.0, mono=130.0)  # exactly at expires_at
    result = await store.consume(token)

    assert result is not None
    assert result.worker_id == "w1"
