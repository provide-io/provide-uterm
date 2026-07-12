#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GraphicalTargetRecord:
    target_id: str
    endpoint: str
    tls_mode: str
    ca_secret_ref: str | None
    client_cert_secret_ref: str | None
    client_key_secret_ref: str | None
    expected_server_name: str | None
    allowed_vm_patterns: tuple[str, ...]
    tenant_id: str | None
    minimum_role: str
    connect_timeout_s: float
    handshake_timeout_s: float
    read_timeout_s: float
    write_timeout_s: float
    shutdown_timeout_s: float
    max_grpc_message_bytes: int
    max_framebuffer_width: int
    max_framebuffer_height: int
    max_rectangles: int
    max_clipboard_bytes: int
    max_pixel_allocation_bytes: int
    allowed_cidrs: tuple[str, ...]
    audit_labels: tuple[tuple[str, str], ...]
    created_at: float
    updated_at: float
