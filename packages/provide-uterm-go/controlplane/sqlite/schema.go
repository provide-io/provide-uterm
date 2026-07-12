//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite

// The schema constants below are copied VERBATIM from the Python source
// (control.plane.sqlite.schema.v0001_initial / v0002_audit_head and
// control.plane.sqlite.migration). SQLite records each CREATE statement's exact
// text in sqlite_master.sql, so keeping the text byte-identical means a database
// created by Go is indistinguishable from one created by Python — the hard
// cross-compatibility requirement.

// migrationTableCreate is the cp_schema_version DDL emitted by apply_migrations
// BEFORE the versioned migrations run. Because it runs first (and every CREATE
// is IF NOT EXISTS), this single-line form is what lands in sqlite_master, so it
// must match the Python string exactly.
const migrationTableCreate = "CREATE TABLE IF NOT EXISTS %s (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"

// v0001SQL is control.plane.sqlite.schema.v0001_initial.SQL (verbatim).
const v0001SQL = `
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
`

// v0002SQL is control.plane.sqlite.schema.v0002_audit_head.SQL (verbatim).
const v0002SQL = `
CREATE TABLE IF NOT EXISTS cp_audit_head (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seq INTEGER NOT NULL,
    record_hash TEXT NOT NULL,
    updated_at REAL NOT NULL
);
`

const v0003SQL = `
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
`

// migration is one ordered schema step.
type migration struct {
	version int
	sql     string
}

// migrations is the ordered migration list. Port of control.plane.sqlite.
// migration.MIGRATIONS: ((1, V0001_SQL), (2, V0002_SQL)).
var migrations = []migration{
	{version: 1, sql: v0001SQL},
	{version: 2, sql: v0002SQL},
	{version: 3, sql: v0003SQL},
}
