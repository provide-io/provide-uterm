//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using Microsoft.Data.Sqlite;

namespace Provide.Uterm.ControlPlane;

/// <summary>Raised when the SQLite control-plane connection cannot be
/// established. Port of control.plane.sqlite.connection.SqliteConnectionError.</summary>
public sealed class SqliteConnectionException(string message) : Exception(message);

/// <summary>
/// A control-plane transaction over the engine's single connection.
///
/// Microsoft.Data.Sqlite requires every command issued while a transaction is
/// pending to carry that transaction, so the engine hands its current one to
/// each command it builds.
/// </summary>
public sealed class SqliteTx(SqliteEngine engine, SqliteTransaction inner) : ITx
{
    internal SqliteTransaction Inner => inner;

    public async Task CommitAsync(CancellationToken cancellationToken = default)
    {
        if (IsDone)
        {
            return;
        }

        try
        {
            await inner.CommitAsync(cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            Finish();
        }
    }

    public async Task RollbackAsync(CancellationToken cancellationToken = default)
    {
        if (IsDone)
        {
            return;
        }

        try
        {
            await inner.RollbackAsync(cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            Finish();
        }
    }

    /// <summary>True once committed or rolled back; both are idempotent after.</summary>
    public bool IsDone { get; private set; }

    private void Finish()
    {
        IsDone = true;
        engine.ClearTransaction(this);
        inner.Dispose();
    }
}

/// <summary>
/// SQLite-backed control-plane engine. Port of
/// control.plane.sqlite.engine.SqliteControlPlane and the Go sqlite.Engine.
///
/// Holds one connection, mirroring Python's single aiosqlite connection, so a
/// transaction and its commit always run on the same handle.
/// </summary>
public sealed class SqliteEngine : IEngine, IAsyncDisposable
{
    private const string SchemaVersionTable = "cp_schema_version";

    private readonly string _databaseUrl;
    private readonly int _busyTimeoutMs;
    private readonly bool _wal;
    private readonly SemaphoreSlim _txGate = new(1, 1);
    private SqliteConnection? _conn;
    private SqliteTx? _current;

    /// <summary>Wall-clock source for internal columns (injected for tests).</summary>
    public Func<double> Now { get; set; } =
        () => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    public SqliteEngine(string databaseUrl, int busyTimeoutMs = 5000, bool wal = true)
    {
        _databaseUrl = databaseUrl;
        _busyTimeoutMs = busyTimeoutMs;
        _wal = wal;
    }

    public EngineCapabilities Capabilities() =>
        new() { Durable = true, Sqlite = true, AuditChain = true };

    /// <summary>
    /// Normalizes a configured database URL to a path. Port of
    /// control.plane.sqlite.connection.resolve_database_path.
    /// </summary>
    public static string ResolveDatabasePath(string databaseUrl)
    {
        if (databaseUrl is ":memory:" or "file::memory:")
        {
            return ":memory:";
        }

        if (Uri.TryCreate(databaseUrl, UriKind.Absolute, out var parsed) &&
            parsed.Scheme is "sqlite" or "sqlite+aiosqlite")
        {
            var path = Uri.UnescapeDataString(parsed.AbsolutePath);
            return path is "" or "/:memory:" or ":memory:" ? ":memory:" : path;
        }

        return databaseUrl;
    }

    public async Task OpenAsync(CancellationToken ct = default)
    {
        if (_conn is not null)
        {
            return;
        }

        var path = ResolveDatabasePath(_databaseUrl);
        try
        {
            if (path != ":memory:")
            {
                var dir = Path.GetDirectoryName(Path.GetFullPath(path));
                if (!string.IsNullOrEmpty(dir))
                {
                    Directory.CreateDirectory(dir);
                }
            }

            var conn = new SqliteConnection(new SqliteConnectionStringBuilder
            {
                DataSource = path,
                Mode = SqliteOpenMode.ReadWriteCreate,
                // Shared cache keeps an in-memory database alive for the
                // connection's lifetime rather than vanishing between commands.
                Cache = path == ":memory:" ? SqliteCacheMode.Shared : SqliteCacheMode.Default,
                // Pooling off so Close really closes. With pooling on, disposing
                // returns the handle to the pool, SQLite never sees a last-connection
                // close, and the WAL is never checkpointed — leaving the .db file
                // incomplete for anything that copies or reads it afterwards
                // (backups, and the other runtimes' engines).
                Pooling = false,
            }.ToString());
            await conn.OpenAsync(ct).ConfigureAwait(false);

            await using (var pragma = conn.CreateCommand())
            {
                pragma.CommandText = string.Create(
                    CultureInfo.InvariantCulture, $"PRAGMA busy_timeout={_busyTimeoutMs}");
                await pragma.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
            }

            if (_wal && path != ":memory:")
            {
                await using var journal = conn.CreateCommand();
                journal.CommandText = "PRAGMA journal_mode=WAL";
                await journal.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
            }

            _conn = conn;
        }
        catch (Exception ex) when (ex is SqliteException or IOException or UnauthorizedAccessException)
        {
            throw new SqliteConnectionException(
                $"failed to initialize sqlite control-plane connection: {ex.Message}");
        }
    }

    public async Task CloseAsync(CancellationToken ct = default)
    {
        if (_conn is null)
        {
            return;
        }

        await _conn.DisposeAsync().ConfigureAwait(false);
        _conn = null;
        _current = null;
    }

    public Task MigrateAsync(CancellationToken ct = default)
    {
        var conn = Require();
        return Migration.ApplyAsync(conn, SchemaVersionTable, Now(), ct);
    }

    /// <summary>
    /// Begins a transaction. Serialized so a second caller waits rather than
    /// racing on the single connection, matching the Go engine's tx-lock.
    /// </summary>
    public async Task<ITx> BeginAsync(CancellationToken ct = default)
    {
        var conn = Require();
        await _txGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            var inner = (SqliteTransaction)await conn
                .BeginTransactionAsync(ct).ConfigureAwait(false);
            var tx = new SqliteTx(this, inner);
            _current = tx;
            return tx;
        }
        catch
        {
            _txGate.Release();
            throw;
        }
    }

    internal void ClearTransaction(SqliteTx tx)
    {
        if (!ReferenceEquals(_current, tx))
        {
            return;
        }

        _current = null;
        _txGate.Release();
    }

    /// <summary>Builds a command bound to the pending transaction, if any.</summary>
    internal SqliteCommand Command()
    {
        var cmd = Require().CreateCommand();
        if (_current is { IsDone: false })
        {
            cmd.Transaction = _current.Inner;
        }

        return cmd;
    }

    private SqliteConnection Require() =>
        _conn ?? throw new SqliteConnectionException("sqlite control plane is not open");

    /// <summary>Drops rows whose soft-delete stamp is older than now-retentionS.</summary>
    public async Task<int> ReapAsync(double now, int retentionS, CancellationToken ct = default)
    {
        var cutoff = now - retentionS;
        await using var cmd = Command();
        cmd.CommandText = "DELETE FROM cp_sessions WHERE deleted_at IS NOT NULL AND deleted_at < $cutoff";
        cmd.Parameters.AddWithValue("$cutoff", cutoff);
        return await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }

    public async Task<AuditHead?> GetAuditHeadAsync(CancellationToken ct = default)
    {
        await using var cmd = Command();
        cmd.CommandText = "SELECT seq, record_hash FROM cp_audit_head WHERE id = 1";
        await using var reader = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        if (!await reader.ReadAsync(ct).ConfigureAwait(false))
        {
            return null;
        }

        return new AuditHead { Seq = reader.GetInt64(0), RecordHash = reader.GetString(1) };
    }

    /// <summary>Persists the head monotonically: a lower-or-equal seq is a no-op
    /// (anti-rollback guard).</summary>
    public async Task SetAuditHeadAsync(long seq, string recordHash, CancellationToken ct = default)
    {
        await using var cmd = Command();
        cmd.CommandText = """
            INSERT INTO cp_audit_head(id, seq, record_hash, updated_at)
            VALUES(1, $seq, $hash, $now)
            ON CONFLICT(id) DO UPDATE SET
                seq = excluded.seq,
                record_hash = excluded.record_hash,
                updated_at = excluded.updated_at
            WHERE excluded.seq > cp_audit_head.seq
            """;
        cmd.Parameters.AddWithValue("$seq", seq);
        cmd.Parameters.AddWithValue("$hash", recordHash);
        cmd.Parameters.AddWithValue("$now", Now());
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }

    public ISessionStore Sessions() => new SqliteSessionStore(this);
    public ITokenStore Tokens() => new SqliteTokenStore(this);
    public IApprovalStore Approvals() => new SqliteApprovalStore(this);
    public ILeaseStore Leases() => new SqliteLeaseStore(this);
    public IGraphicalTargetStore GraphicalTargets() => new SqliteGraphicalTargetStore(this);

    public async ValueTask DisposeAsync() => await CloseAsync().ConfigureAwait(false);

    // Shared scan helpers: SQLite has no native null-double, so nullable columns
    // come back as DBNull.
    internal static double? NullableDouble(SqliteDataReader r, int i) =>
        r.IsDBNull(i) ? null : r.GetDouble(i);

    internal static string? NullableString(SqliteDataReader r, int i) =>
        r.IsDBNull(i) ? null : r.GetString(i);

    internal static object DbValue(string? v) => (object?)v ?? DBNull.Value;

    internal static object DbValue(double? v) => (object?)v ?? DBNull.Value;
}
