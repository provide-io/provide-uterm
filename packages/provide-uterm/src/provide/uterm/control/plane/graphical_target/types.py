#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GraphicalTargetRecord:
    """A persisted graphical-target definition.

    The persistence shape of the canonical model in
    ``packages/provide-uterm-csharp/src/Provide.Uterm/Server/GraphicalTargets.cs``
    (Go port: ``graphical.Definition``).  Timestamps are epoch seconds to match
    every other ``cp_*`` table; the in-memory model uses richer types and
    converts at the registry boundary.

    ``config`` is the decoded protocol-specific parameter object.  It is stored
    as a JSON document so a new protocol needs no migration, and it is NOT a
    secret — it survives the public/redacted copy that crosses REST.
    """

    target_id: str
    tenant_id: str
    display_name: str
    protocol: str
    width: int
    height: int
    created_at: float
    endpoint: str | None = None
    secret: str | None = None
    is_system: bool = False
    is_static: bool = False
    ca_secret_ref: str | None = None
    client_cert_secret_ref: str | None = None
    client_key_secret_ref: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    created_by: str | None = None
    updated_by: str | None = None
    updated_at: float | None = None
