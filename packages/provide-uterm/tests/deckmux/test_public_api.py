#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Public API smoke checks for deckmux exports."""

from __future__ import annotations

import pytest

from provide.uterm import deckmux


def test_deckmux_mixin_is_public() -> None:
    """``DeckMuxMixin`` must be importable from the package namespace."""
    assert "DeckMuxMixin" in deckmux.__all__
    assert hasattr(deckmux, "DeckMuxMixin")


def test_deckmux_init_has_public_name() -> None:
    """Consumers should have a public initialization hook name."""
    from provide.uterm.deckmux import DeckMuxMixin

    assert hasattr(DeckMuxMixin, "_deckmux_init")
    assert hasattr(DeckMuxMixin, "deckmux_init")


def test_deckmux_init_calls_private_initializer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public hook delegates to the existing private initializer."""
    from provide.uterm.deckmux import DeckMuxMixin

    calls: list[DeckMuxMixin] = []
    monkeypatch.setattr(DeckMuxMixin, "_deckmux_init", lambda self: calls.append(self))
    hub = DeckMuxMixin()
    hub.deckmux_init()

    assert calls == [hub]
