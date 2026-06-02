#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for server config_schema/models helpers.

Covers the two *undecorated* module-level functions in ``config_schema.py``
that mutmut actually mutates (it skips every ``@model_validator`` /
``@field_validator`` / ``@classmethod`` by design — see
``_skip_node_and_children`` in mutmut's ``file_mutation.py``): ``_clean_path``
and the SSRF guard ``_require_secure_url``. Also covers ``models.py``'s
``model_dump`` / ``validation_error_message``.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from provide.uterm.server.config_schema import _clean_path, _require_secure_url
from provide.uterm.server.models import (
    SessionDefinition,
    model_dump,
    validation_error_message,
)

# ---------------------------------------------------------------------------
# _clean_path
# ---------------------------------------------------------------------------


class TestCleanPath:
    def test_path_with_leading_slash_not_doubled(self) -> None:
        """Path already starting with '/' must not get a second slash prepended.

        Kills _mutmut_6: startswith('/') → startswith('XX/XX').
        """
        result = _clean_path("/admin", "/fallback")
        assert result == "/admin", f"Expected '/admin', got {result!r}"

    def test_trailing_slash_stripped(self) -> None:
        """Trailing slash is removed.

        Kills _mutmut_11: rstrip('/') → rstrip(None).
        """
        result = _clean_path("/admin/", "/fallback")
        assert result == "/admin", f"Expected '/admin', got {result!r}"
        result2 = _clean_path("admin/", "/fallback")
        assert result2 == "/admin", f"Expected '/admin', got {result2!r}"

    def test_root_slash_fallback(self) -> None:
        """When path is empty or '/', the function returns '/'.

        Kills _mutmut_14: or '/' → or 'XX/XX'.
        """
        assert _clean_path("", "/") == "/"
        assert _clean_path("/", "/") == "/"

    def test_path_without_leading_slash_gets_one(self) -> None:
        """Bare path gets a leading slash."""
        result = _clean_path("worker", "/fallback")
        assert result == "/worker", f"Expected '/worker', got {result!r}"

    def test_rstrip_only_removes_slash_not_x(self) -> None:
        """rstrip('/') must strip only '/' — not the character 'X'.

        Kills mutmut_13: rstrip('/') → rstrip('XX/XX').
        rstrip('XX/XX') strips the char-set {X, /} from the right, so a path
        ending in 'X' (e.g. '/pathX/') would lose the 'X' with the mutation.
        """
        result = _clean_path("/pathX/", "/fallback")
        assert result == "/pathX", f"Expected '/pathX', trailing slash removed but X preserved, got {result!r}"

    def test_rstrip_preserves_trailing_uppercase_in_segment(self) -> None:
        """Path '/adminX' should not have 'X' stripped.

        With rstrip('/') (correct): '/adminX' → '/adminX'.
        With rstrip('XX/XX') (mutation): '/adminX' → '/admin' (X stripped).
        """
        result = _clean_path("/adminX", "/fallback")
        assert result == "/adminX", f"Expected '/adminX', got {result!r}"


# ---------------------------------------------------------------------------
# model_dump
# ---------------------------------------------------------------------------


class TestModelDump:
    def test_returns_datetime_object_not_string(self) -> None:
        """model_dump(mode='python') must return datetime instances, not ISO strings.

        Kills _mutmut_2 (mode='XXpythonXX') and _mutmut_3 (mode='PYTHON') which
        would cause pydantic to raise or use json mode (returns strings).
        """
        s = SessionDefinition(session_id="dump-test")
        result = model_dump(s)
        assert isinstance(result, dict), "model_dump must return a dict"
        assert "created_at" in result, "created_at should be in dump"
        assert isinstance(result["created_at"], datetime), (
            f"created_at must be a datetime in python mode, got {type(result['created_at'])}"
        )

    def test_returns_dict(self) -> None:
        """model_dump returns a plain dict."""
        s = SessionDefinition(session_id="dump-test-2")
        result = model_dump(s)
        assert isinstance(result, dict)
        assert result["session_id"] == "dump-test-2"


# ---------------------------------------------------------------------------
# validation_error_message
# ---------------------------------------------------------------------------


class TestValidationErrorMessage:
    def _trigger_error(self) -> ValidationError:
        """Trigger a pydantic ValidationError for testing."""
        with pytest.raises(ValidationError) as exc_info:
            SessionDefinition(session_id=123)  # type: ignore[arg-type]
        return exc_info.value

    def test_returns_string(self) -> None:
        """validation_error_message always returns a string."""
        exc = self._trigger_error()
        result = validation_error_message(exc)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_no_url_in_message(self) -> None:
        """include_url=False means the URL is excluded from errors dict.

        Kills _mutmut_3: include_url=False → include_url=True.
        The 'url' field in pydantic errors comes from include_url=True.
        We test that the message does not contain 'https://errors.pydantic.dev'.
        """
        exc = self._trigger_error()
        result = validation_error_message(exc)
        assert "https://errors.pydantic.dev" not in result, f"URL should not appear in error message, got: {result!r}"

    def test_fallback_when_no_msg_key(self) -> None:
        """When first error has no 'msg' key, falls back to str(exc).

        Kills _mutmut_11 (fallback=None) and _mutmut_13 (fallback missing arg).
        We mock the exc.errors() to return a dict without 'msg'.
        """
        exc = self._trigger_error()
        from unittest.mock import patch

        no_msg_errors = [{"loc": ("session_id",), "type": "string_type"}]
        with patch.object(type(exc), "errors", return_value=no_msg_errors):
            result = validation_error_message(exc)
        # Without 'msg', should fall back to str(exc) which is non-empty
        assert isinstance(result, str)
        assert len(result) > 0
        # Must NOT be 'None' (which mutmut_11 would produce via str(None))
        assert result != "None", f"Fallback should not be 'None', got {result!r}"


# ---------------------------------------------------------------------------
# _require_secure_url — the outbound-URL SSRF guard
#
# urlparse() lowercases both the scheme and the hostname, so every case-flip
# mutation on the "https"/"http"/".localhost" literals changes behaviour (the
# mutated literal can never match the always-lowercase parsed value). Each test
# names the mutant ids it distinguishes; together they leave only the one
# documented-equivalent mutant (mutmut_14, the unreachable-fallback string),
# which is excused in mutation_equivalents.toml.
# ---------------------------------------------------------------------------


class TestRequireSecureUrl:
    FIELD = "auth.webhook_idp_url"

    def test_none_and_empty_are_noops(self) -> None:
        """Falsy url short-circuits before any parsing — must not raise."""
        assert _require_secure_url(None, self.FIELD) is None
        assert _require_secure_url("", self.FIELD) is None

    def test_https_is_always_allowed(self) -> None:
        """An https:// url returns None with no parsing/scheme errors.

        Kills mutmut_2 (parsed=None → AttributeError), mutmut_3
        (urlparse(None)), mutmut_4 (== → !=), mutmut_5 ("XXhttpsXX"),
        mutmut_6 ("HTTPS" — scheme is always lowercased so it never matches).
        """
        assert _require_secure_url("https://example.com/jwks", self.FIELD) is None

    def test_http_loopback_ip_is_allowed(self) -> None:
        """Cleartext http:// to 127.0.0.1 is permitted (local dev).

        Kills mutmut_7 (!= → ==), mutmut_8 ("XXhttpXX"), mutmut_9 ("HTTP"),
        mutmut_11 (host=None → AttributeError), mutmut_13 (hostname or "" →
        hostname and "" → empty host raises), mutmut_15 (or → and),
        mutmut_16 (in → not in).
        """
        assert _require_secure_url("http://127.0.0.1:8080/cb", self.FIELD) is None

    def test_http_localhost_name_is_allowed(self) -> None:
        """Cleartext http:// to the literal host 'localhost' is permitted.

        Kills mutmut_12 (.lower() → .upper(): the uppercased host no longer
        matches the lowercase _LOOPBACK_HOSTS membership, so it would raise).
        """
        assert _require_secure_url("http://localhost/cb", self.FIELD) is None

    def test_http_dot_localhost_suffix_is_allowed(self) -> None:
        """Cleartext http:// to a *.localhost host is permitted via endswith.

        Kills mutmut_18 ("XX.localhostXX") and mutmut_19 (".LOCALHOST" — host
        is always lowercase so the uppercased suffix never matches).
        """
        assert _require_secure_url("http://api.localhost/cb", self.FIELD) is None

    def test_non_http_scheme_rejected_with_specific_message(self) -> None:
        """A non-http(s) scheme raises ValueError carrying the field + reason.

        Kills mutmut_10 (ValueError(None) — its 'None' text fails the match).
        """
        with pytest.raises(ValueError, match=r"auth\.webhook_idp_url must use http\(s\)"):
            _require_secure_url("ftp://files.example.com", self.FIELD)

    def test_routable_http_rejected_as_valueerror_with_message(self) -> None:
        """Cleartext http:// to a routable host raises ValueError (not None/loopback).

        Kills mutmut_16 (in → not in would *allow* the routable host — no raise),
        mutmut_17 (endswith(None) → TypeError, not ValueError) and mutmut_20
        (ValueError(None) — its 'None' text fails the match).
        """
        with pytest.raises(ValueError, match=r"auth\.webhook_idp_url must use https://"):
            _require_secure_url("http://evil.example.com/steal", self.FIELD)
