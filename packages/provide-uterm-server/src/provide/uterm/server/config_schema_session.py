#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Config-backed session definition model, split out of ``config_schema``.

This module holds the single largest Pydantic model in the server config
schema (:class:`SessionDefinition`). It is a pure model class — mutmut SKIPS
every ``@model_validator`` / ``@field_validator`` / ``@classmethod`` by design,
and class-body field defaults aren't inside a function, so this module carries
**zero** mutants. The two undecorated, mutation-bearing helpers (``_clean_path``
and the ``_require_secure_url`` SSRF guard) deliberately stay in
``config_schema`` so that module keeps a non-empty mutant set (``total>0``).

``config_schema`` re-exports :class:`SessionDefinition` (and
``SERVER_BUILTIN_CONNECTOR_TYPES``) so existing imports of the form
``from provide.uterm.server.config_schema import SessionDefinition`` keep
resolving.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from provide.uterm.bridge.contracts import InputMode, Visibility  # noqa: TC001
from provide.uterm.server.config_schema import SERVER_BUILTIN_CONNECTOR_TYPES, ServerBaseModel


class SessionDefinition(ServerBaseModel):
    """Config-backed definition for a named hosted terminal session."""

    session_id: str
    display_name: str = ""
    connector_type: str = "shell"
    connector_config: dict[str, Any] = Field(default_factory=dict)
    input_mode: InputMode = "open"
    auto_start: bool = True
    tags: list[str] = Field(default_factory=list)
    recording_enabled: bool | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    owner: str | None = None
    visibility: Visibility = "public"
    ephemeral: bool = False
    presence: bool = False
    auto_transfer_idle_s: int = 30
    keystroke_queue: Literal["display", "replay"] = "display"

    @model_validator(mode="before")
    @classmethod
    def _collect_connector_config(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "display_name" not in data or data.get("display_name") is None:
            session_id = str(data.get("session_id", "")).strip()
            if session_id:
                data["display_name"] = session_id
        known_fields = set(cls.model_fields)
        connector_config = dict(data.get("connector_config", {}))
        for key in list(data):
            if key in known_fields:
                continue
            connector_config[key] = data.pop(key)
        data["connector_config"] = connector_config
        return data

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        session_id = value.strip()
        if not session_id:
            raise ValueError("session_id is required for each [[sessions]] entry")
        if not re.match(r"^[\w\-]+$", session_id):
            raise ValueError(f"session_id must match ^[\\w\\-]+$, got: {session_id!r}")
        return session_id

    @field_validator("connector_type")
    @classmethod
    def _validate_connector_type(cls, value: str, info: Any) -> str:
        connector_type = value.strip() or "shell"
        session_id = ""
        if isinstance(info.data, dict):
            session_id = str(info.data.get("session_id", "")).strip()

        # Only validate against the registry if it's already populated.
        # This prevents circular dependency issues during startup.
        try:
            from provide.uterm.server.connectors import registered_types

            known = registered_types()
            valid = known | SERVER_BUILTIN_CONNECTOR_TYPES
            if known and connector_type not in valid:
                label = session_id or "<unknown>"
                raise ValueError(
                    f"invalid connector_type for {label!r}: {connector_type!r} — must be one of {sorted(valid)}"
                )
        except ImportError:  # pragma: no cover — server.connectors is always present in this package
            # Fallback for environments where server.connectors isn't available
            pass
        return connector_type

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str, info: Any) -> str:
        if value == "":
            if isinstance(info.data, dict):
                return str(info.data.get("session_id", ""))
            return ""
        return str(value)

    @field_validator("input_mode", mode="before")
    @classmethod
    def _validate_input_mode(cls, value: Any, info: Any) -> Any:
        if value not in {"hijack", "open"}:
            session_id = ""
            if isinstance(info.data, dict):
                session_id = str(info.data.get("session_id", "")).strip()
            raise ValueError(f"invalid input_mode for {session_id or '<unknown>'}: {value}")
        return value

    @field_validator("visibility", mode="before")
    @classmethod
    def _validate_visibility(cls, value: Any, info: Any) -> Any:
        if value not in {"public", "operator", "private"}:
            session_id = ""
            if isinstance(info.data, dict):
                session_id = str(info.data.get("session_id", "")).strip()
            raise ValueError(f"invalid visibility for {session_id or '<unknown>'}: {value!r}")
        return value
