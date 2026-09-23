#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``ControlPlaneResumeStore`` (all methods except
``consume``) and the module-level ``_make_resume_record`` helper.

Covers ``ControlPlaneResumeStore.get``, ``.revoke``, ``.create``, ``._run_tx``
and ``_make_resume_record``. ``.consume`` and ``InMemoryResumeStore`` have
their own kill-suite and are not touched here.

``test_resume.py`` drives these methods through a real control-plane backend
(the ``memory``/``sqlite`` engines from ``bootstrap_control_plane``), so it
never pins the exact record or arguments a collaborator call receives: a
swapped argument, a dropped keyword, a mangled boundary comparison, or a
forced default all still produce a *plausible* session as long as the real
control-plane engine tolerates the call. That is exactly why the mutants
below survive it. Every fake here is a strict recorder -- ``_TokenStore``'s
methods record every call and this file's assertions pin the exact value
wherever it matters, never tolerating a substituted/dropped argument the way
``AsyncMock(return_value=...)`` would.

Measured against ``mutants/.../resume.py``'s
``xǁControlPlaneResumeStoreǁ{get,revoke,create,_run_tx}__mutmut_*`` and
``x__make_resume_record__mutmut_*`` families: this suite closes all 26
surviving mutants (get: 4, 9, 15, 23, 24, 27, 28, 30, 31, 38, 41, 55; revoke:
4, 5, 6, 11, 13; create: 2, 3, 12, 21; _run_tx: 6; _make_resume_record: 6, 7,
13, 14).

No documented equivalents: all 26 mutants are killed below.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.server.bridge.hub import resume as resume_module
from provide.uterm.server.bridge.hub.resume import ControlPlaneResumeStore, _make_resume_record

WID = "worker-1"


class _Tx:
    """Records commit/rollback calls; rollback can be scripted to raise."""

    def __init__(self, *, rollback_raises: BaseException | None = None) -> None:
        self.commits = 0
        self.rollbacks = 0
        self._rollback_raises = rollback_raises

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self._rollback_raises is not None:
            raise self._rollback_raises


class _TokenStore:
    """``_ControlPlaneResumeTokenStore`` double mirroring resume.py's
    Protocol exactly. Every call is recorded; ``consume_resume_token`` is
    out of this suite's scope and raises if it is ever reached."""

    def __init__(self, *, get_result: Any = None) -> None:
        self.create_calls: list[Any] = []
        self.get_calls: list[str] = []
        self.revoke_calls: list[tuple[str, float]] = []
        self._get_result = get_result

    async def create_resume_token(self, record: Any) -> None:
        self.create_calls.append(record)

    async def get_resume_token(self, token_value: str) -> Any | None:
        self.get_calls.append(token_value)
        return self._get_result

    async def revoke_resume_token(self, token_value: str, revoked_at: float) -> None:
        self.revoke_calls.append((token_value, revoked_at))

    async def consume_resume_token(self, token_value: str, revoked_at: float) -> Any | None:
        raise AssertionError("consume_resume_token is out of scope for this kill-suite")


class _Backend:
    """``_ControlPlaneResumeBackend`` double: one tx per instance, and
    ``token_store`` asserts it always gets that same tx back."""

    def __init__(self, store: _TokenStore, *, tx: _Tx | None = None) -> None:
        self._store = store
        self._tx = tx if tx is not None else _Tx()

    async def begin(self) -> Any:
        return self._tx

    def token_store(self, tx: Any) -> _TokenStore:
        assert tx is self._tx, f"token_store got unexpected tx={tx!r}"
        return self._store


class _TokenUrlsafe:
    """``secrets.token_urlsafe`` double: asserts the exact byte count."""

    def __init__(self, token: str) -> None:
        self.calls: list[int] = []
        self._token = token

    def __call__(self, nbytes: int) -> str:
        self.calls.append(nbytes)
        assert nbytes == 32, f"token_urlsafe got nbytes={nbytes!r}"
        return self._token


def _pin_clock(monkeypatch: pytest.MonkeyPatch, *, wall: float, mono: float) -> None:
    monkeypatch.setattr(resume_module, "time", SimpleNamespace(time=lambda: wall, monotonic=lambda: mono))


def _record(**overrides: Any) -> Any:
    """A minimal stand-in for a control-plane ``ResumeTokenRecord`` row."""
    fields: dict[str, Any] = {
        "token_value": "rec-token",
        "session_id": WID,
        "role": "viewer",
        "created_at": 0.0,
        "expires_at": 0.0,
        "was_hijack_owner": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ---------------------------------------------------------------------------
# _run_tx
# ---------------------------------------------------------------------------


class _OpError(RuntimeError):
    """Distinct exception so a test can pin exactly what propagates."""


class _RollbackError(RuntimeError):
    """Distinct exception; must never be what a caller sees."""


async def test_run_tx_swallows_a_rollback_failure_and_still_raises_the_original_error() -> None:
    """Kills ``_run_tx__mutmut_6`` (``suppress(Exception)`` -> ``suppress(None)``).

    When the wrapped op raises, ``_run_tx`` attempts a compensating rollback;
    if the rollback itself also fails, that failure must be swallowed so the
    original error survives unchanged. ``suppress(None)`` cannot swallow
    anything -- ``contextlib.suppress.__exit__`` calls ``issubclass(exctype,
    (None,))``, which itself raises ``TypeError`` -- so the mutant would
    replace the original ``_OpError`` with something else entirely.
    """
    tx = _Tx(rollback_raises=_RollbackError("rollback failed"))
    store = ControlPlaneResumeStore(_Backend(_TokenStore(), tx=tx))

    async def _op(_store: Any) -> None:
        raise _OpError("op failed")

    with pytest.raises(_OpError):
        await store._run_tx(_op)

    assert tx.rollbacks == 1
    assert tx.commits == 0


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_requests_exactly_32_bytes_from_token_urlsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills ``create__mutmut_2`` (``token_urlsafe(None)``) and
    ``create__mutmut_3`` (``token_urlsafe(33)``): the real call always asks
    for exactly 32 bytes."""
    token_gen = _TokenUrlsafe("tok-abc")
    monkeypatch.setattr(resume_module, "secrets", SimpleNamespace(token_urlsafe=token_gen))
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    store = ControlPlaneResumeStore(_Backend(_TokenStore()))

    result = await store.create(WID, "admin", 30.0)

    assert result == "tok-abc"
    assert token_gen.calls == [32]


async def test_create_builds_the_exact_resume_token_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills ``create__mutmut_12`` (``was_hijack_owner=None``) and
    ``create__mutmut_21`` (``was_hijack_owner=True``): a newly created token
    always starts with ``was_hijack_owner=False`` -- checked by identity, not
    just truthiness, so a ``None`` substitution is caught too. Also pins
    every other field of the record handed to ``create_resume_token``."""
    token_gen = _TokenUrlsafe("tok-xyz")
    monkeypatch.setattr(resume_module, "secrets", SimpleNamespace(token_urlsafe=token_gen))
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    token_store = _TokenStore()
    store = ControlPlaneResumeStore(_Backend(token_store))

    token = await store.create("worker-9", "operator", 45.0)

    assert token == "tok-xyz"
    assert len(token_store.create_calls) == 1
    record = token_store.create_calls[0]
    assert record.token_value == "tok-xyz"
    assert record.session_id == "worker-9"
    assert record.role == "operator"
    assert record.created_at == 1_000.0
    assert record.expires_at == 1_045.0
    assert record.was_hijack_owner is False
    assert record.revoked_at is None
    assert store._created_at_mono[token] == 5_000.0


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_of_a_vanished_record_prunes_the_mono_tracking_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills ``get__mutmut_4`` (``pop(token, None)`` -> ``pop(None, None)``
    in the "record not found" branch): a token that was tracked locally but
    is no longer known to the backend must have its stale local entry
    pruned."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    token_store = _TokenStore(get_result=None)
    store = ControlPlaneResumeStore(_Backend(token_store))
    store._created_at_mono["gone-token"] = 4_000.0

    result = await store.get("gone-token")

    assert result is None
    assert "gone-token" not in store._created_at_mono


async def test_get_at_exact_expiry_boundary_is_not_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills ``get__mutmut_9`` (``>`` -> ``>=`` on the expiry check): at
    exactly ``expires_at`` the token is not yet expired. Also kills
    ``get__mutmut_38`` (returned ``expires_at``'s ``max(0.0, ...)`` ->
    ``max(1.0, ...)``): with zero seconds of TTL remaining, the returned
    monotonic ``expires_at`` must equal ``now_mono`` exactly, not
    ``now_mono + 1.0``."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    record = _record(created_at=1_000.0, expires_at=1_000.0)
    token_store = _TokenStore(get_result=record)
    store = ControlPlaneResumeStore(_Backend(token_store))

    result = await store.get("boundary-token")

    assert result is not None
    assert result.expires_at == 5_000.0
    assert token_store.revoke_calls == []


async def test_get_of_an_expired_record_revokes_and_prunes_the_mono_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills ``get__mutmut_15`` (``pop(token, None)`` -> ``pop(None, None)``
    in the "expired" branch): once a record is found expired by wall clock,
    its stale local tracking entry must be pruned too."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    record = _record(created_at=900.0, expires_at=999.0)
    token_store = _TokenStore(get_result=record)
    store = ControlPlaneResumeStore(_Backend(token_store))
    store._created_at_mono["expired-token"] = 4_000.0

    result = await store.get("expired-token")

    assert result is None
    assert token_store.revoke_calls == [("expired-token", 1_000.0)]
    assert "expired-token" not in store._created_at_mono


async def test_get_of_an_untracked_zero_age_session_uses_the_real_age_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills ``get__mutmut_23`` (``age_s``'s ``max(0.0, ...)`` ->
    ``max(1.0, ...)``), ``get__mutmut_24`` (``now_wall - record.created_at``
    -> ``now_wall + record.created_at``) and ``get__mutmut_28``/
    ``get__mutmut_30`` (the ``.get`` default forced to ``None`` / omitted
    entirely): for a session with zero age that ``create()`` never tracked
    locally (e.g. read back after a process restart), the default used by
    ``self._created_at_mono.get(token, ...)`` must equal ``now_mono``
    exactly."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    record = _record(created_at=1_000.0, expires_at=2_000.0)
    token_store = _TokenStore(get_result=record)
    store = ControlPlaneResumeStore(_Backend(token_store))

    result = await store.get("untracked-token")

    assert result is not None
    assert result.created_at == 5_000.0


async def test_get_of_an_untracked_aged_session_subtracts_age_from_now_mono(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills ``get__mutmut_31`` (the ``.get`` default's ``now_mono -
    age_s`` -> ``now_mono + age_s``): with ten seconds of real age and no
    local tracking entry, the default monotonic ``created_at`` must be
    ``now_mono - 10.0``, not ``now_mono + 10.0``. Also re-confirms
    ``get__mutmut_24``/``28``/``30`` with a nonzero age."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    record = _record(created_at=990.0, expires_at=2_000.0)
    token_store = _TokenStore(get_result=record)
    store = ControlPlaneResumeStore(_Backend(token_store))

    result = await store.get("untracked-aged-token")

    assert result is not None
    assert result.created_at == 4_990.0


async def test_get_of_a_tracked_session_uses_the_tracked_mono_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills ``get__mutmut_27`` (``self._created_at_mono.get(token, ...)``
    -> ``.get(None, ...)``): when the token *is* tracked locally, ``get()``
    must return that exact tracked value rather than falling through to the
    freshly-computed default (pinned here to a different number)."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    record = _record(created_at=990.0, expires_at=2_000.0)
    token_store = _TokenStore(get_result=record)
    store = ControlPlaneResumeStore(_Backend(token_store))
    store._created_at_mono["tracked-token"] = 777.0

    result = await store.get("tracked-token")

    assert result is not None
    assert result.created_at == 777.0


async def test_get_reads_the_token_field_from_the_record_not_the_input_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills ``get__mutmut_41`` (``token=str(record.token_value)`` ->
    ``token=None``) and ``get__mutmut_55`` (-> ``token=str(None)``): the
    returned session's ``token`` field is the record's own
    ``token_value``, stringified -- deliberately different from the lookup
    argument here so neither mutant can hide behind the two happening to
    match."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    record = _record(token_value="record-owns-this-value", created_at=1_000.0, expires_at=2_000.0)
    token_store = _TokenStore(get_result=record)
    store = ControlPlaneResumeStore(_Backend(token_store))

    result = await store.get("lookup-token")

    assert result is not None
    assert result.token == "record-owns-this-value"


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


async def test_revoke_of_a_never_tracked_missing_record_is_a_silent_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills ``revoke__mutmut_5`` (``pop(token, None)`` -> ``pop(None)``, a
    single-argument pop with no default) and ``revoke__mutmut_6`` (->
    ``pop(token,)``, likewise a single-argument pop with no default): with
    no local tracking entry present at all, either mutant's bare ``pop``
    raises ``KeyError`` on the missing key it tries to remove; the real
    two-argument ``pop`` is always a silent no-op here."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    token_store = _TokenStore(get_result=None)
    store = ControlPlaneResumeStore(_Backend(token_store))

    await store.revoke("never-tracked-token")  # must not raise

    assert token_store.revoke_calls == []


async def test_revoke_of_a_vanished_but_tracked_record_prunes_the_mono_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills ``revoke__mutmut_4`` (``pop(token, None)`` -> ``pop(None,
    None)`` in the "record not found" branch): a token that is tracked
    locally but no longer known to the backend must still have its stale
    entry pruned."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    token_store = _TokenStore(get_result=None)
    store = ControlPlaneResumeStore(_Backend(token_store))
    store._created_at_mono["gone-token"] = 4_000.0

    await store.revoke("gone-token")

    assert "gone-token" not in store._created_at_mono
    assert token_store.revoke_calls == []


async def test_revoke_of_an_untracked_but_valid_record_succeeds_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills ``revoke__mutmut_13`` (``pop(token, None)`` -> ``pop(token,)``,
    a single-argument pop with no default, on the success-path prune): a
    token found in the backend but never locally tracked (e.g. after a
    process restart) must still revoke cleanly, with the final untracked
    prune being a silent no-op rather than a ``KeyError``."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    token_store = _TokenStore(get_result=_record())
    store = ControlPlaneResumeStore(_Backend(token_store))

    await store.revoke("untracked-valid-token")  # must not raise

    assert token_store.revoke_calls == [("untracked-valid-token", 1_000.0)]


async def test_revoke_of_a_tracked_valid_record_revokes_and_prunes_the_mono_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills ``revoke__mutmut_11`` (``pop(token, None)`` -> ``pop(None,
    None)`` on the success-path prune): once the backend confirms the
    record and it is revoked, the local tracking entry for the real token
    must be removed."""
    _pin_clock(monkeypatch, wall=1_000.0, mono=5_000.0)
    token_store = _TokenStore(get_result=_record())
    store = ControlPlaneResumeStore(_Backend(token_store))
    store._created_at_mono["tracked-token"] = 4_000.0

    await store.revoke("tracked-token")

    assert token_store.revoke_calls == [("tracked-token", 1_000.0)]
    assert "tracked-token" not in store._created_at_mono


# ---------------------------------------------------------------------------
# _make_resume_record
# ---------------------------------------------------------------------------


def test_make_resume_record_passes_through_every_field_exactly() -> None:
    """Kills ``x__make_resume_record__mutmut_6`` (``was_hijack_owner``
    forced to ``None``), ``_mutmut_14`` (``was_hijack_owner`` dropped,
    defaulting to ``False``), ``_mutmut_7`` (``revoked_at`` forced to
    ``None``) and ``_mutmut_13`` (``revoked_at`` dropped, defaulting to
    ``None``): passing ``was_hijack_owner=True`` and a non-``None``
    ``revoked_at`` distinguishes every one of the four from the real
    pass-through, since each mutant's forced/default value differs from
    what was actually passed in."""
    record = _make_resume_record(
        token_value="tok",
        session_id="worker-1",
        role="admin",
        created_at=10.0,
        expires_at=20.0,
        was_hijack_owner=True,
        revoked_at=42.0,
    )

    assert record.token_value == "tok"
    assert record.session_id == "worker-1"
    assert record.role == "admin"
    assert record.created_at == 10.0
    assert record.expires_at == 20.0
    assert record.was_hijack_owner is True
    assert record.revoked_at == 42.0
