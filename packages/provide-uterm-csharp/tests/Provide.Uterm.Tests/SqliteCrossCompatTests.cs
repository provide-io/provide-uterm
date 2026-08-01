//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using Provide.Uterm.ControlPlane;

namespace Provide.Uterm.Tests;

/// <summary>
/// Cross-language database compatibility.
///
/// Schema-text parity (SqliteSchemaParityTests) proves the DDL matches; these
/// prove the artefact actually round-trips. Python -> C# runs against a
/// committed golden database with no Python dependency, so it works in CI;
/// C# -> Python shells out and skips when uv is unavailable.
/// </summary>
public sealed class SqliteCrossCompatTests
{
    private static string TestDataDir =>
        Path.Combine(AppContext.BaseDirectory, "testdata");

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

    /// <summary>
    /// A database written by the Python control plane opens here unchanged: the
    /// migration runner sees the schema is current and applies nothing, and the
    /// row Python wrote reads back through the C# store.
    /// </summary>
    [Fact]
    public async Task PythonCreatedDatabase_ReadsHere()
    {
        var golden = Path.Combine(TestDataDir, "golden_py.db");
        if (!File.Exists(golden))
        {
            // The fixture ships with the repo; a packaged run without it is not
            // a failure.
            return;
        }

        // Copy so the test never mutates the committed fixture.
        var work = Path.Combine(Path.GetTempPath(), $"xcompat-{Guid.NewGuid():N}.db");
        File.Copy(golden, work);

        var engine = new SqliteEngine(work);
        await engine.OpenAsync();
        // Re-migrating a current database must be a no-op rather than an error.
        await engine.MigrateAsync();

        var got = await engine.GraphicalTargets().GetAsync("gt-python");
        Assert.NotNull(got);
        Assert.Equal("from-python", got!.DisplayName);
        Assert.Equal("acme", got.TenantId);
        Assert.Equal("rfb", got.Protocol);
        Assert.Equal(800, got.Width);
        Assert.Equal("vm.local:5900", got.Endpoint);
        Assert.Contains("vm-py", got.Config);

        // And C# can write into Python's database.
        await engine.GraphicalTargets().PutAsync(new GraphicalTargetRecord
        {
            TargetId = "gt-csharp", TenantId = "acme", DisplayName = "from-csharp",
            Protocol = "memory", Width = 640, Height = 480, CreatedAt = 1,
        });
        Assert.Equal(2, (await engine.GraphicalTargets().ListAsync()).Count);

        await engine.CloseAsync();
        SqliteTestDb.Delete(work);
    }

    /// <summary>
    /// A database created here is byte-identical in schema to one Python wrote.
    ///
    /// Compares sqlite_master — the text SQLite itself stored — rather than
    /// shelling out to Python: a subprocess test can silently pass by skipping
    /// when the toolchain is absent, which is worse than not having it. If every
    /// stored CREATE matches, either runtime can open either file.
    /// </summary>
    [Fact]
    public async Task CSharpCreatedDatabase_HasTheSameStoredSchemaAsPython()
    {
        var golden = Path.Combine(TestDataDir, "golden_py.db");
        if (!File.Exists(golden))
        {
            return;
        }

        var mine = Path.Combine(Path.GetTempPath(), $"xcompat-cs-{Guid.NewGuid():N}.db");
        var engine = new SqliteEngine(mine);
        await engine.OpenAsync();
        await engine.MigrateAsync();
        await engine.CloseAsync();

        var pythonSchema = await StoredSchemaAsync(golden);
        var csharpSchema = await StoredSchemaAsync(mine);

        Assert.NotEmpty(pythonSchema);
        Assert.Equal(pythonSchema.Keys.OrderBy(k => k, StringComparer.Ordinal),
                     csharpSchema.Keys.OrderBy(k => k, StringComparer.Ordinal));
        foreach (var (name, sql) in pythonSchema)
        {
            Assert.Equal(sql, csharpSchema[name]);
        }

        SqliteTestDb.Delete(mine);
    }

    /// <summary>Every CREATE statement as SQLite recorded it.</summary>
    private static async Task<Dictionary<string, string>> StoredSchemaAsync(string path)
    {
        var work = Path.Combine(Path.GetTempPath(), $"schema-{Guid.NewGuid():N}.db");
        File.Copy(path, work);
        try
        {
            var stored = new Dictionary<string, string>(StringComparer.Ordinal);

            // Scoped so the connection closes before the `finally` deletes the
            // copy: a method-scoped `await using` disposes after the finally.
            await using (var conn = SqliteTestDb.Connect(work))
            {
                await conn.OpenAsync();
                await using var cmd = conn.CreateCommand();
                cmd.CommandText = "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name";
                await using var r = await cmd.ExecuteReaderAsync();
                while (await r.ReadAsync())
                {
                    stored[r.GetString(0)] = r.GetString(1);
                }
            }

            return stored;
        }
        finally
        {
            SqliteTestDb.Delete(work);
        }
    }
}
