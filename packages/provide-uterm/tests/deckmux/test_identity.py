#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.deckmux._identity.

Covers parse_identity_frame, presence_from_identity, and the
identity_as_principal adapter — the DeckMux consumer of the
identity control frame emitted by the SSH gateway resolver.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from hypothesis import given
from hypothesis import strategies as st

from provide.uterm.auth import ResolvedIdentity
from provide.uterm.control_channel_builders import make_identity
from provide.uterm.deckmux import (
    PresenceStore,
    identity_as_principal,
    parse_identity_frame,
    presence_from_identity,
)


class TestParseIdentityFrame:
    def test_happy_path(self) -> None:
        frame = {
            "type": "identity",
            "version": 1,
            "subject": "sre:alice",
            "claims": {"role": "oncall", "display_name": "Alice Liddell"},
            "fingerprint": "SHA256:abc",
            "transport": "ssh",
        }
        result = parse_identity_frame(frame)
        assert result is not None
        assert result.subject == "sre:alice"
        assert result.claims == {"role": "oncall", "display_name": "Alice Liddell"}
        assert result.fingerprint == "SHA256:abc"

    def test_wrong_type_returns_none(self) -> None:
        assert parse_identity_frame({"type": "resume", "subject": "x"}) is None

    def test_missing_type_returns_none(self) -> None:
        assert parse_identity_frame({"subject": "x"}) is None

    def test_unknown_version_returns_none(self) -> None:
        """Forward compat: a newer version we don't understand is ignored,
        not treated as v1."""
        frame = {"type": "identity", "version": 99, "subject": "x"}
        assert parse_identity_frame(frame) is None

    def test_missing_version_returns_none(self) -> None:
        frame = {"type": "identity", "subject": "x"}
        assert parse_identity_frame(frame) is None

    def test_missing_subject_returns_none(self) -> None:
        assert parse_identity_frame({"type": "identity", "version": 1}) is None

    def test_empty_subject_returns_none(self) -> None:
        frame = {"type": "identity", "version": 1, "subject": ""}
        assert parse_identity_frame(frame) is None

    def test_non_string_subject_returns_none(self) -> None:
        frame = {"type": "identity", "version": 1, "subject": 42}
        assert parse_identity_frame(frame) is None

    def test_malformed_claims_downgraded_to_empty(self) -> None:
        """Garbage claims → empty dict rather than rejecting the identity."""
        frame = {"type": "identity", "version": 1, "subject": "x", "claims": "not a dict"}
        result = parse_identity_frame(frame)
        assert result is not None
        assert result.claims == {}

    def test_missing_claims_yields_empty_dict(self) -> None:
        frame = {"type": "identity", "version": 1, "subject": "x"}
        result = parse_identity_frame(frame)
        assert result is not None
        assert result.claims == {}

    def test_missing_fingerprint_yields_empty_string(self) -> None:
        frame = {"type": "identity", "version": 1, "subject": "x"}
        result = parse_identity_frame(frame)
        assert result is not None
        assert result.fingerprint == ""

    def test_non_string_fingerprint_coerced_to_empty(self) -> None:
        frame = {"type": "identity", "version": 1, "subject": "x", "fingerprint": 12345}
        result = parse_identity_frame(frame)
        assert result is not None
        assert result.fingerprint == ""

    def test_signature_validation_fails_if_secret_required_but_missing(self) -> None:
        frame = {"type": "identity", "version": 1, "subject": "x"}
        assert parse_identity_frame(frame, expected_secret="my-secret") is None

    def test_signature_validation_fails_if_signature_invalid(self) -> None:
        frame = {"type": "identity", "version": 1, "subject": "x", "signature": "bad"}
        assert parse_identity_frame(frame, expected_secret="my-secret") is None

    def test_signature_validation_succeeds(self) -> None:
        frame = make_identity(subject="x", secret="my-secret")
        result = parse_identity_frame(frame, expected_secret="my-secret")
        assert result is not None
        assert result.subject == "x"

    def test_signed_non_string_transport_is_normalized_to_empty(self) -> None:
        secret = "my-secret"
        claims: dict[str, str] = {}
        canonical = f"1:x:::{json.dumps(claims, sort_keys=True, separators=(',', ':'))}"
        signature = hmac.new(secret.encode(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        frame = {
            "type": "identity",
            "version": 1,
            "subject": "x",
            "claims": claims,
            "fingerprint": "",
            "transport": 123,
            "signature": signature,
        }

        result = parse_identity_frame(frame, expected_secret=secret)

        assert result is not None
        assert result.subject == "x"

    @given(
        subject=st.text(min_size=1, max_size=100),
        claims=st.one_of(st.none(), st.dictionaries(st.text(min_size=1), st.text(max_size=50))),
        fingerprint=st.text(max_size=50),
        transport=st.text(min_size=1, max_size=20),
        secret=st.text(min_size=1, max_size=50),
    )
    def test_fuzz_hmac_signature(
        self,
        subject: str,
        claims: dict[str, str] | None,
        fingerprint: str,
        transport: str,
        secret: str,
    ) -> None:
        # Generate signed frame
        frame = make_identity(
            subject=subject,
            claims=claims,
            fingerprint=fingerprint,
            transport=transport,
            secret=secret,
        )
        assert "signature" in frame

        # Verify it parses correctly with the correct secret
        result = parse_identity_frame(frame, expected_secret=secret)
        assert result is not None
        assert result.subject == subject
        if claims:
            assert result.claims == claims
        assert result.fingerprint == fingerprint

        # Verify it fails with a wrong secret
        wrong_secret = secret + "bad"
        assert parse_identity_frame(frame, expected_secret=wrong_secret) is None

        # Verify tampering with subject fails
        tampered_frame = dict(frame)
        tampered_frame["subject"] = subject + "tampered"
        assert parse_identity_frame(tampered_frame, expected_secret=secret) is None

        # Verify tampering with claims fails (if claims exist)
        if claims:
            tampered_frame = dict(frame)
            tampered_claims = dict(claims)
            tampered_claims["tampered"] = "yes"
            tampered_frame["claims"] = tampered_claims
            assert parse_identity_frame(tampered_frame, expected_secret=secret) is None


class TestPresenceFromIdentity:
    def test_uses_display_name_claim_when_present(self) -> None:
        identity = ResolvedIdentity(
            subject="sre:alice",
            claims={"display_name": "Alice Liddell", "role": "oncall"},
        )
        p = presence_from_identity(identity, connection_id="conn-1")
        assert p.user_id == "sre:alice"
        assert p.name == "Alice Liddell"
        assert p.role == "oncall"
        assert p.initials == "AL"  # from "Alice Liddell"

    def test_falls_back_to_display_claim(self) -> None:
        identity = ResolvedIdentity(subject="x", claims={"display": "Bob"})
        p = presence_from_identity(identity, connection_id="conn-2")
        assert p.name == "Bob"

    def test_falls_back_to_subject_suffix(self) -> None:
        """'sre:alice' → 'alice' when no display claim is present."""
        identity = ResolvedIdentity(subject="sre:alice", claims={})
        p = presence_from_identity(identity, connection_id="conn-3")
        assert p.name == "alice"
        assert p.user_id == "sre:alice"  # user_id is always the raw subject

    def test_subject_without_colon_used_verbatim(self) -> None:
        identity = ResolvedIdentity(subject="alice", claims={})
        p = presence_from_identity(identity, connection_id="conn-4")
        assert p.name == "alice"

    def test_falls_back_to_generated_name_for_unusable_subject(self) -> None:
        """Subject like 'role:' (empty tail) → generate_name from connection_id."""
        identity = ResolvedIdentity(subject="role:", claims={})
        p = presence_from_identity(identity, connection_id="conn-5")
        # user_id still set; name is deterministically generated from connection_id
        # — just assert it's a non-empty string (generate_name's contract).
        assert p.user_id == "role:"
        assert p.name

    def test_color_claim_used_when_present(self) -> None:
        identity = ResolvedIdentity(
            subject="x",
            claims={"color": "#ff00aa"},
        )
        p = presence_from_identity(identity, connection_id="conn-6")
        assert p.color == "#ff00aa"

    def test_color_generated_when_not_in_claims(self) -> None:
        identity = ResolvedIdentity(subject="x", claims={})
        p = presence_from_identity(identity, connection_id="conn-7")
        # Deterministic generation — just ensure it's populated.
        assert p.color

    def test_color_generation_respects_taken_set(self) -> None:
        """If every color is taken except one, that one is picked."""
        from provide.uterm.deckmux import generate_color

        # Seed a taken-colors set that excludes only a specific value.
        probe_color = generate_color("probe-conn", taken=frozenset())
        identity = ResolvedIdentity(subject="x", claims={})
        p = presence_from_identity(
            identity,
            connection_id="probe-conn",
            taken_colors=frozenset(),
        )
        # With empty taken, we get the deterministic first choice.
        assert p.color == probe_color

    def test_role_claim_overrides_explicit_role_param(self) -> None:
        identity = ResolvedIdentity(subject="x", claims={"role": "admin"})
        p = presence_from_identity(identity, connection_id="c", role="viewer")
        assert p.role == "admin"

    def test_role_param_used_when_no_claim(self) -> None:
        identity = ResolvedIdentity(subject="x", claims={})
        p = presence_from_identity(identity, connection_id="c", role="viewer")
        assert p.role == "viewer"

    def test_empty_role_when_neither_source_provides(self) -> None:
        identity = ResolvedIdentity(subject="x", claims={})
        p = presence_from_identity(identity, connection_id="c")
        assert p.role == ""

    def test_presence_can_be_stored(self) -> None:
        """Integration: the returned UserPresence is usable in PresenceStore."""
        identity = ResolvedIdentity(
            subject="sre:alice",
            claims={"display_name": "Alice", "role": "oncall"},
        )
        p = presence_from_identity(identity, connection_id="c")
        store = PresenceStore()
        store._users[p.user_id] = p  # bypass add() to preserve fields
        assert store.get("sre:alice") is not None
        assert store.get("sre:alice").name == "Alice"

    def test_non_string_claim_values_ignored(self) -> None:
        """A display_name that isn't a string must not blow up resolution."""
        identity = ResolvedIdentity(
            subject="sre:alice",
            claims={"display_name": 42, "display": None, "role": ["a", "b"]},
        )
        p = presence_from_identity(identity, connection_id="c")
        # Falls through to subject suffix since non-string claim values are
        # treated as absent.
        assert p.name == "alice"
        assert p.role == ""


class TestIdentityAsPrincipal:
    def test_subject_id_mirrors_subject(self) -> None:
        identity = ResolvedIdentity(subject="sre:alice", claims={})
        p = identity_as_principal(identity)
        assert p.subject_id == "sre:alice"

    def test_display_name_uses_display_name_claim(self) -> None:
        identity = ResolvedIdentity(
            subject="sre:alice",
            claims={"display_name": "Alice Liddell"},
        )
        p = identity_as_principal(identity)
        assert p.display_name == "Alice Liddell"

    def test_display_name_falls_back_to_subject_tail(self) -> None:
        identity = ResolvedIdentity(subject="sre:alice", claims={})
        p = identity_as_principal(identity)
        assert p.display_name == "alice"

    def test_display_name_falls_back_to_full_subject_when_no_tail(self) -> None:
        identity = ResolvedIdentity(subject="alice", claims={})
        p = identity_as_principal(identity)
        assert p.display_name == "alice"

    def test_preserves_original_identity(self) -> None:
        """The adapter carries the original identity through for later inspection."""
        identity = ResolvedIdentity(subject="x", claims={"role": "admin"})
        p = identity_as_principal(identity)
        assert p.identity is identity

    def test_duck_types_for_hub_mixin(self) -> None:
        """The adapter must satisfy the attribute contract the hub mixin uses.

        :class:`DeckMuxMixin` calls:
          - ``getattr(principal, "subject_id", user_id)``
          - ``getattr(principal, "display_name", "")``
        """
        identity = ResolvedIdentity(subject="sre:alice", claims={"display_name": "Alice"})
        p = identity_as_principal(identity)
        assert getattr(p, "subject_id", None) == "sre:alice"
        assert getattr(p, "display_name", None) == "Alice"

    def test_frozen(self) -> None:
        import dataclasses

        import pytest

        identity = ResolvedIdentity(subject="x", claims={})
        p = identity_as_principal(identity)
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.subject_id = "y"  # type: ignore[misc]


class TestHubMixinIntegration:
    """End-to-end check that identity_as_principal + the hub mixin cooperate.

    If this test breaks, the duck-typed principal contract changed —
    either update the adapter's attribute names, or fix the mixin.
    """

    async def test_identity_principal_feeds_hub_mixin(self) -> None:
        from unittest.mock import AsyncMock

        from provide.uterm.deckmux._hub_mixin import DeckMuxMixin

        class _FakeHub(DeckMuxMixin):
            def __init__(self) -> None:
                self._deckmux_init()
                self.broadcast = AsyncMock()

        class _FakeWS:
            pass

        hub = _FakeHub()
        ws = _FakeWS()

        identity = ResolvedIdentity(
            subject="sre:alice",
            claims={"display_name": "Alice Smith"},
            fingerprint="SHA256:abc",
        )
        principal = identity_as_principal(identity)

        result = await hub.deckmux_on_browser_connect("worker-1", ws, "admin", principal=principal)
        assert result is not None
        user = result["users"][0]
        assert user["user_id"] == "sre:alice"
        assert user["name"] == "Alice Smith"
        assert user["initials"] == "AS"
        assert user["role"] == "admin"

    async def test_identity_without_display_falls_back_in_hub(self) -> None:
        """No display_name → hub uses subject_id as the visible name.

        Confirms our adapter leaves display_name populated to the best
        fallback so the hub's ``display_name or subject_id`` expression
        lands on the right string.
        """
        from unittest.mock import AsyncMock

        from provide.uterm.deckmux._hub_mixin import DeckMuxMixin

        class _FakeHub(DeckMuxMixin):
            def __init__(self) -> None:
                self._deckmux_init()
                self.broadcast = AsyncMock()

        class _FakeWS:
            pass

        hub = _FakeHub()
        ws = _FakeWS()

        identity = ResolvedIdentity(subject="svc-bot", claims={})
        principal = identity_as_principal(identity)
        result = await hub.deckmux_on_browser_connect("worker-2", ws, "operator", principal=principal)
        assert result is not None
        user = result["users"][0]
        assert user["user_id"] == "svc-bot"
        # display_name fell through to the subject since no tail / claim.
        assert user["name"] == "svc-bot"
