#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for server/models.py — _clean_path, model_dump, validation_error_message."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from provide.terminal.server.config_schema import _clean_path
from provide.terminal.server.models import (
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
