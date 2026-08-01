//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.Data.Sqlite;

namespace Provide.Uterm.Tests;

/// <summary>
/// Raw-connection and temp-file helpers shared by the SQLite control-plane tests.
///
/// Microsoft.Data.Sqlite pools connections by default, so disposing a connection
/// hands the native handle back to the pool rather than closing it: the database
/// file stays open with no managed object left to point at. POSIX unlinks a file
/// that still has open handles without complaint, so on macOS and Linux the leak
/// is invisible; Windows refuses, and teardown fails with "the process cannot
/// access the file ... because it is being used by another process".
///
/// Tests therefore open raw connections with pooling off — the same choice, for
/// the same reason, that <see cref="ControlPlane.SqliteEngine"/> already makes
/// for the production connection.
/// </summary>
internal static class SqliteTestDb
{
    /// <summary>
    /// An unpooled connection to <paramref name="path"/>, so disposing it really
    /// releases the file handle instead of parking it in the pool.
    /// </summary>
    public static SqliteConnection Connect(
        string path, SqliteOpenMode mode = SqliteOpenMode.ReadWriteCreate) =>
        new(new SqliteConnectionStringBuilder
        {
            DataSource = path,
            Mode = mode,
            Pooling = false,
        }.ToString());

    /// <summary>Removes a database file together with its WAL sidecars.</summary>
    public static void Delete(string path)
    {
        foreach (var suffix in new[] { "", "-wal", "-shm" })
        {
            if (File.Exists(path + suffix))
            {
                File.Delete(path + suffix);
            }
        }
    }
}
