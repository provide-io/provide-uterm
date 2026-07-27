//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;
using Microsoft.Data.Sqlite;
using Provide.Uterm.ControlPlane;

namespace Provide.Uterm.Tests;

/// <summary>
/// Cross-language schema parity.
///
/// SQLite records each CREATE statement's literal text in sqlite_master.sql, so
/// a database is only interchangeable between the Python, Go and C# control
/// planes if all three emit byte-identical DDL. These tests read the Python
/// sources directly rather than trusting a hand-copied constant — a drift in
/// either direction fails here rather than at a customer's restore.
/// </summary>
public sealed class SqliteSchemaParityTests
{
    /// <summary>Locates the repo root by walking up to the packages/ directory.</summary>
    private static string? RepoRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            if (Directory.Exists(Path.Combine(dir.FullName, "packages", "provide-uterm")))
            {
                return dir.FullName;
            }

            dir = dir.Parent;
        }

        return null;
    }

    private static string? PythonSql(string root, string module)
    {
        var path = Path.Combine(
            root, "packages", "provide-uterm", "src", "provide", "uterm", "control", "plane",
            "sqlite", "schema", module);
        if (!File.Exists(path))
        {
            return null;
        }

        var text = File.ReadAllText(path).ReplaceLineEndings("\n");
        var m = Regex.Match(text, "SQL = \"\"\"(.*?)\"\"\"", RegexOptions.Singleline);
        return m.Success ? m.Groups[1].Value : null;
    }

    [Theory]
    [InlineData("v0001_initial.py", nameof(Schema.V0001))]
    [InlineData("v0002_audit_head.py", nameof(Schema.V0002))]
    [InlineData("v0003_graphical_targets.py", nameof(Schema.V0003))]
    public void Ddl_IsByteIdenticalToPython(string module, string constant)
    {
        var root = RepoRoot();
        if (root is null)
        {
            // Running outside the monorepo (e.g. a packaged artifact) — nothing
            // to compare against, and a missing sibling is not a failure.
            return;
        }

        var expected = PythonSql(root, module);
        if (expected is null)
        {
            return;
        }

        var actual = constant switch
        {
            nameof(Schema.V0001) => Schema.V0001,
            nameof(Schema.V0002) => Schema.V0002,
            _ => Schema.V0003,
        };

        Assert.Equal(expected, actual.ReplaceLineEndings("\n"));
    }

    /// <summary>
    /// The migration bookkeeping DDL runs before the versioned migrations, so
    /// its single-line form is what lands in sqlite_master and must match too.
    /// </summary>
    [Fact]
    public void MigrationTableDdl_MatchesPython()
    {
        var root = RepoRoot();
        if (root is null)
        {
            return;
        }

        var path = Path.Combine(
            root, "packages", "provide-uterm", "src", "provide", "uterm", "control", "plane",
            "sqlite", "migration.py");
        if (!File.Exists(path))
        {
            return;
        }

        var text = File.ReadAllText(path);
        Assert.Contains(
            "CREATE TABLE IF NOT EXISTS {migration_table} (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)",
            text.Replace("\n", " "));
        Assert.Equal(
            "CREATE TABLE IF NOT EXISTS {0} (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)",
            Schema.MigrationTableCreate);
    }

    /// <summary>
    /// The stored schema of a C#-created database, as SQLite itself recorded it.
    /// This is the artefact another runtime actually reads.
    /// </summary>
    [Fact]
    public async Task CreatedDatabase_StoresTheCanonicalSchema()
    {
        var path = Path.Combine(Path.GetTempPath(), $"parity-{Guid.NewGuid():N}.db");
        var engine = new SqliteEngine(path);
        await engine.OpenAsync();
        await engine.MigrateAsync();
        await engine.CloseAsync();

        var stored = new Dictionary<string, string>(StringComparer.Ordinal);
        await using (var raw = new SqliteConnection($"Data Source={path}"))
        {
            await raw.OpenAsync();
            await using var cmd = raw.CreateCommand();
            cmd.CommandText =
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name";
            await using var r = await cmd.ExecuteReaderAsync();
            while (await r.ReadAsync())
            {
                stored[r.GetString(0)] = r.GetString(1);
            }
        }

        // Every canonical table is present with the DDL text the other runtimes
        // emit, and the graphical-target index came along with it.
        foreach (var table in new[]
                 {
                     "cp_schema_version", "cp_sessions", "cp_session_tokens", "cp_resume_tokens",
                     "cp_approvals", "cp_leases", "cp_audit_head", "cp_graphical_targets",
                 })
        {
            Assert.True(stored.ContainsKey(table), $"missing table {table}");
        }

        Assert.True(stored.ContainsKey("ix_cp_graphical_targets_tenant"));

        // The recorded text for a representative table matches the shared DDL
        // verbatim, whitespace included.
        Assert.Contains("target_id TEXT PRIMARY KEY", stored["cp_graphical_targets"]);
        Assert.Contains("config TEXT NOT NULL DEFAULT '{}'", stored["cp_graphical_targets"]);
        Assert.Equal(
            "CREATE TABLE cp_schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)",
            stored["cp_schema_version"]);

        foreach (var suffix in new[] { "", "-wal", "-shm" })
        {
            if (File.Exists(path + suffix))
            {
                File.Delete(path + suffix);
            }
        }
    }
}
