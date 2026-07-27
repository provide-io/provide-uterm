//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using Microsoft.Data.Sqlite;
using Provide.Uterm.Cli;

namespace Provide.Uterm.Tests;

/// <summary>
/// The CLI must actually select the durable control plane.
///
/// The async factory existed and was tested before the CLI used it, so
/// control_plane.backend=sqlite had no effect in production: a capability that
/// nothing invokes is indistinguishable from one that was never written. These
/// drive the real `server` command.
/// </summary>
public sealed class CliServerControlPlaneTests
{
    private static int FreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static string WriteConfig(string dir, string backend, string? dbPath)
    {
        var path = Path.Combine(dir, "server.toml");
        var database = dbPath is null ? "" : $"database_url = \"{dbPath.Replace("\\", "/")}\"\n";
        File.WriteAllText(path, $"""
            [control_plane]
            backend = "{backend}"
            {database}
            """);
        return path;
    }

    /// <summary>
    /// Running the server with a sqlite control plane leaves a migrated database
    /// behind — proof the CLI opened AND migrated an engine rather than falling
    /// back to the in-memory registry.
    /// </summary>
    [Fact]
    public void ServerCommand_SqliteBackend_CreatesAMigratedDatabase()
    {
        var dir = Path.Combine(Path.GetTempPath(), $"cli-cp-{Guid.NewGuid():N}");
        Directory.CreateDirectory(dir);
        var db = Path.Combine(dir, "cp.db");
        var cfg = WriteConfig(dir, "sqlite", db);

        using var o = new StringWriter();
        using var e = new StringWriter();
        var code = Root.Execute(
            ["server", "--once", "--config", cfg, "--host", "127.0.0.1", "--port", FreePort().ToString()],
            o, e);

        Assert.Equal(0, code);
        Assert.True(File.Exists(db), "the sqlite backend should have created a database");

        // The engine was closed properly, so the schema is in the file itself
        // rather than stranded in an uncheckpointed WAL.
        var tables = new List<string>();
        using (var conn = new SqliteConnection($"Data Source={db};Mode=ReadOnly"))
        {
            conn.Open();
            using var cmd = conn.CreateCommand();
            cmd.CommandText = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name";
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                tables.Add(r.GetString(0));
            }
        }

        Assert.Contains("cp_graphical_targets", tables);
        Assert.Contains("cp_schema_version", tables);

        Directory.Delete(dir, recursive: true);
    }

    /// <summary>A memory backend still starts cleanly and writes no database.</summary>
    [Fact]
    public void ServerCommand_MemoryBackend_WritesNothing()
    {
        var dir = Path.Combine(Path.GetTempPath(), $"cli-cp-{Guid.NewGuid():N}");
        Directory.CreateDirectory(dir);
        var cfg = WriteConfig(dir, "memory", null);

        using var o = new StringWriter();
        using var e = new StringWriter();
        var code = Root.Execute(
            ["server", "--once", "--config", cfg, "--host", "127.0.0.1", "--port", FreePort().ToString()],
            o, e);

        Assert.Equal(0, code);
        Assert.Empty(Directory.GetFiles(dir, "*.db"));

        Directory.Delete(dir, recursive: true);
    }

    /// <summary>
    /// A database written by one run is still readable by the next: the point of
    /// wiring the CLI at all.
    /// </summary>
    [Fact]
    public void ServerCommand_ReusesAnExistingDatabaseAcrossRuns()
    {
        var dir = Path.Combine(Path.GetTempPath(), $"cli-cp-{Guid.NewGuid():N}");
        Directory.CreateDirectory(dir);
        var db = Path.Combine(dir, "cp.db");
        var cfg = WriteConfig(dir, "sqlite", db);

        using var o = new StringWriter();
        using var e = new StringWriter();
        for (var run = 0; run < 2; run++)
        {
            Assert.Equal(0, Root.Execute(
                ["server", "--once", "--config", cfg, "--host", "127.0.0.1", "--port", FreePort().ToString()],
                o, e));
        }

        // Re-migrating an existing database is a no-op, so the version list is
        // unchanged rather than duplicated.
        var versions = new List<long>();
        using (var conn = new SqliteConnection($"Data Source={db};Mode=ReadOnly"))
        {
            conn.Open();
            using var cmd = conn.CreateCommand();
            cmd.CommandText = "SELECT version FROM cp_schema_version ORDER BY version";
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                versions.Add(r.GetInt64(0));
            }
        }

        Assert.Equal([1L, 2L, 3L], versions);

        Directory.Delete(dir, recursive: true);
    }
}
