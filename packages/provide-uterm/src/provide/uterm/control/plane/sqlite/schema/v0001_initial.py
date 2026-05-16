#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

SQL = """
CREATE TABLE IF NOT EXISTS cp_schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cp_sessions (
    session_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    connector_type TEXT NOT NULL,
    owner TEXT,
    visibility TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deleted_at REAL
);

CREATE TABLE IF NOT EXISTS cp_session_tokens (
    session_id TEXT NOT NULL,
    token_kind TEXT NOT NULL,
    token_value TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked_at REAL,
    PRIMARY KEY (session_id, token_kind)
);

CREATE TABLE IF NOT EXISTS cp_resume_tokens (
    token_value TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    was_hijack_owner INTEGER NOT NULL DEFAULT 0,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS cp_approvals (
    approval_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    command TEXT NOT NULL,
    requested_by TEXT,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS cp_leases (
    session_id TEXT PRIMARY KEY,
    hijack_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    lease_expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    deleted_at REAL
);
"""
