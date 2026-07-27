//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using Microsoft.Data.Sqlite;

namespace Provide.Uterm.ControlPlane;

/// <summary>Raised when the SQLite control-plane schema cannot be migrated.
/// Port of control.plane.sqlite.migration.SqliteMigrationError.</summary>
public sealed class SqliteMigrationException(string message) : Exception(message);

/// <summary>
/// Applies the inert control-plane schema migrations in order. Port of
/// control.plane.sqlite.migration.apply_migrations.
/// </summary>
internal static class Migration
{
    /// <summary>
    /// Whether <paramref name="value"/> is a valid identifier, matching the
    /// guard str.isidentifier() applies to the migration table name in Python.
    /// ASCII-only is sufficient for the fixed "cp_schema_version" default and
    /// the test inputs.
    /// </summary>
    internal static bool IsIdentifier(string value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return false;
        }

        for (var i = 0; i < value.Length; i++)
        {
            var c = value[i];
            var isLetter = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_';
            var isDigit = c >= '0' && c <= '9';
            if (i == 0 && !isLetter)
            {
                return false;
            }

            if (i > 0 && !isLetter && !isDigit)
            {
                return false;
            }
        }

        return true;
    }

    /// <summary>
    /// Applies every migration newer than the recorded version. <paramref name="now"/>
    /// supplies the applied_at timestamp (injected for deterministic tests).
    /// </summary>
    internal static async Task ApplyAsync(
        SqliteConnection conn,
        string migrationTable,
        double now,
        CancellationToken ct = default)
    {
        if (!IsIdentifier(migrationTable))
        {
            throw new SqliteMigrationException($"invalid migration table name: \"{migrationTable}\"");
        }

        try
        {
            await ApplyInnerAsync(conn, migrationTable, now, ct).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is not SqliteMigrationException)
        {
            // Best-effort rollback of any autocommit-uncommitted work, then wrap —
            // matching the Python try/except that converts every exception to
            // SqliteMigrationError.
            try
            {
                await using var rollback = conn.CreateCommand();
                rollback.CommandText = "ROLLBACK";
                await rollback.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
            }
            catch (SqliteException)
            {
                // No transaction was open; nothing to undo.
            }

            throw new SqliteMigrationException($"failed to apply control-plane migration: {ex.Message}");
        }
    }

    private static async Task ApplyInnerAsync(
        SqliteConnection conn,
        string migrationTable,
        double now,
        CancellationToken ct)
    {
        await using (var create = conn.CreateCommand())
        {
            // migrationTable is validated by IsIdentifier above.
            create.CommandText = string.Format(
                CultureInfo.InvariantCulture, Schema.MigrationTableCreate, migrationTable);
            await create.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
        }

        long current;
        await using (var read = conn.CreateCommand())
        {
            read.CommandText = $"SELECT COALESCE(MAX(version), 0) FROM {migrationTable}";
            var scalar = await read.ExecuteScalarAsync(ct).ConfigureAwait(false);
            current = scalar is null or DBNull ? 0 : Convert.ToInt64(scalar, CultureInfo.InvariantCulture);
        }

        foreach (var (version, sql) in Schema.Migrations)
        {
            if (version <= current)
            {
                continue;
            }

            await using (var script = conn.CreateCommand())
            {
                // Microsoft.Data.Sqlite executes every statement in one command,
                // matching aiosqlite's executescript.
                script.CommandText = sql;
                await script.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
            }

            await using var record = conn.CreateCommand();
            record.CommandText = $"INSERT INTO {migrationTable}(version, applied_at) VALUES($v, $t)";
            record.Parameters.AddWithValue("$v", version);
            record.Parameters.AddWithValue("$t", now);
            await record.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
        }
    }
}
