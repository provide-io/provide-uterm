//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.ControlPlane;

/// <summary>
/// Control-plane schema DDL.
///
/// The constants below are copied VERBATIM from the Python source
/// (control.plane.sqlite.schema.v0001_initial / v0002_audit_head /
/// v0003_graphical_targets and control.plane.sqlite.migration), exactly as the
/// Go port does. SQLite records each CREATE statement's literal text in
/// sqlite_master.sql, so keeping the text byte-identical is what makes a
/// database created here indistinguishable from one created by Python or Go —
/// the hard cross-compatibility requirement. Verified by
/// SqliteSchemaParityTests.
/// </summary>
internal static class Schema
{
    /// <summary>
    /// The cp_schema_version DDL emitted by apply_migrations BEFORE the
    /// versioned migrations run. Because it runs first (and every CREATE is
    /// IF NOT EXISTS), this single-line form is what lands in sqlite_master, so
    /// it must match the Python string exactly.
    /// </summary>
    internal const string MigrationTableCreate =
        "CREATE TABLE IF NOT EXISTS {0} (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)";

    /// <summary>control.plane.sqlite.schema.v0001_initial.SQL (verbatim).</summary>
    internal const string V0001 = @"
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
";

    /// <summary>control.plane.sqlite.schema.v0002_audit_head.SQL (verbatim).</summary>
    internal const string V0002 = @"
CREATE TABLE IF NOT EXISTS cp_audit_head (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seq INTEGER NOT NULL,
    record_hash TEXT NOT NULL,
    updated_at REAL NOT NULL
);
";

    /// <summary>control.plane.sqlite.schema.v0003_graphical_targets.SQL (verbatim).</summary>
    internal const string V0003 = @"
CREATE TABLE IF NOT EXISTS cp_graphical_targets (
    target_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    protocol TEXT NOT NULL,
    endpoint TEXT,
    secret TEXT,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    is_system INTEGER NOT NULL DEFAULT 0,
    is_static INTEGER NOT NULL DEFAULT 0,
    ca_secret_ref TEXT,
    client_cert_secret_ref TEXT,
    client_key_secret_ref TEXT,
    config TEXT NOT NULL DEFAULT '{}',
    created_by TEXT,
    created_at REAL NOT NULL,
    updated_by TEXT,
    updated_at REAL
);

CREATE INDEX IF NOT EXISTS ix_cp_graphical_targets_tenant
    ON cp_graphical_targets(tenant_id);
";

    /// <summary>
    /// Ordered migration list. Port of control.plane.sqlite.migration.MIGRATIONS:
    /// ((1, V0001_SQL), (2, V0002_SQL), (3, V0003_SQL)).
    /// </summary>
    internal static readonly (int Version, string Sql)[] Migrations =
    [
        (1, Lf(V0001)),
        (2, Lf(V0002)),
        (3, Lf(V0003)),
    ];

    /// <summary>
    /// Force LF endings, whatever the checkout did to this file.
    ///
    /// The constants above are verbatim string literals, so they carry this
    /// source file's own line endings. A CRLF checkout — the Windows default —
    /// therefore puts CRLF into sqlite_master.sql and quietly makes a database
    /// created here differ from one created by Python or Go, which is exactly
    /// the byte-identical contract this class exists to keep. Normalising at
    /// the single point the SQL is consumed makes that independent of how the
    /// repository was cloned.
    /// </summary>
    private static string Lf(string sql) => sql.Replace("\r\n", "\n", StringComparison.Ordinal);
}
