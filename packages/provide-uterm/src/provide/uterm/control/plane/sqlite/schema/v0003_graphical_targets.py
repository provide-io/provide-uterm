#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

SQL = """
CREATE TABLE IF NOT EXISTS cp_graphical_targets (
    target_id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    tls_mode TEXT NOT NULL,
    ca_secret_ref TEXT,
    client_cert_secret_ref TEXT,
    client_key_secret_ref TEXT,
    expected_server_name TEXT,
    allowed_vm_patterns TEXT NOT NULL,
    tenant_id TEXT,
    minimum_role TEXT NOT NULL,
    connect_timeout_s REAL NOT NULL,
    handshake_timeout_s REAL NOT NULL,
    read_timeout_s REAL NOT NULL,
    write_timeout_s REAL NOT NULL,
    shutdown_timeout_s REAL NOT NULL,
    max_grpc_message_bytes INTEGER NOT NULL,
    max_framebuffer_width INTEGER NOT NULL,
    max_framebuffer_height INTEGER NOT NULL,
    max_rectangles INTEGER NOT NULL,
    max_clipboard_bytes INTEGER NOT NULL,
    max_pixel_allocation_bytes INTEGER NOT NULL,
    allowed_cidrs TEXT NOT NULL,
    audit_labels TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""
