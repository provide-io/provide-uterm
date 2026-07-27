//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.Data.Sqlite;

namespace Provide.Uterm.ControlPlane;

/// <summary>SQLite ISessionStore. Port of SqliteSessionStore.</summary>
internal sealed class SqliteSessionStore(SqliteEngine engine) : ISessionStore
{
    private const string Columns =
        "session_id, display_name, connector_type, owner, visibility, " +
        "lifecycle_state, created_at, updated_at, deleted_at";

    public async Task UpsertAsync(SessionRecord rec, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = """
            INSERT INTO cp_sessions(session_id, display_name, connector_type, owner,
                visibility, lifecycle_state, created_at, updated_at, deleted_at)
            VALUES($id, $name, $connector, $owner, $vis, $state, $created, $updated, $deleted)
            ON CONFLICT(session_id) DO UPDATE SET
                display_name = excluded.display_name,
                connector_type = excluded.connector_type,
                owner = excluded.owner,
                visibility = excluded.visibility,
                lifecycle_state = excluded.lifecycle_state,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                deleted_at = excluded.deleted_at
            """;
        cmd.Parameters.AddWithValue("$id", rec.SessionId);
        cmd.Parameters.AddWithValue("$name", rec.DisplayName);
        cmd.Parameters.AddWithValue("$connector", rec.ConnectorType);
        cmd.Parameters.AddWithValue("$owner", SqliteEngine.DbValue(rec.Owner));
        cmd.Parameters.AddWithValue("$vis", rec.Visibility);
        cmd.Parameters.AddWithValue("$state", rec.LifecycleState);
        cmd.Parameters.AddWithValue("$created", rec.CreatedAt);
        cmd.Parameters.AddWithValue("$updated", rec.UpdatedAt);
        cmd.Parameters.AddWithValue("$deleted", SqliteEngine.DbValue(rec.DeletedAt));
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }

    public async Task<SessionRecord?> GetAsync(string sessionId, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = $"SELECT {Columns} FROM cp_sessions WHERE session_id = $id";
        cmd.Parameters.AddWithValue("$id", sessionId);
        await using var r = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        if (!await r.ReadAsync(ct).ConfigureAwait(false))
        {
            return null;
        }

        return new SessionRecord
        {
            SessionId = r.GetString(0),
            DisplayName = r.GetString(1),
            ConnectorType = r.GetString(2),
            Owner = SqliteEngine.NullableString(r, 3),
            Visibility = r.GetString(4),
            LifecycleState = r.GetString(5),
            CreatedAt = r.GetDouble(6),
            UpdatedAt = r.GetDouble(7),
            DeletedAt = SqliteEngine.NullableDouble(r, 8),
        };
    }

    public async Task MarkDeletedAsync(string sessionId, double deletedAt, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = """
            UPDATE cp_sessions
            SET deleted_at = $deleted, lifecycle_state = 'deleted', updated_at = $deleted
            WHERE session_id = $id
            """;
        cmd.Parameters.AddWithValue("$deleted", deletedAt);
        cmd.Parameters.AddWithValue("$id", sessionId);
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }
}

/// <summary>SQLite ITokenStore. Port of SqliteTokenStore.</summary>
internal sealed class SqliteTokenStore(SqliteEngine engine) : ITokenStore
{
    private const string ResumeColumns =
        "token_value, session_id, role, created_at, expires_at, was_hijack_owner, revoked_at";

    public async Task PutSessionTokenAsync(SessionTokenRecord rec, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = """
            INSERT INTO cp_session_tokens(session_id, token_kind, token_value, created_at,
                expires_at, revoked_at)
            VALUES($id, $kind, $value, $created, $expires, $revoked)
            ON CONFLICT(session_id, token_kind) DO UPDATE SET
                token_value = excluded.token_value,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                revoked_at = excluded.revoked_at
            """;
        cmd.Parameters.AddWithValue("$id", rec.SessionId);
        cmd.Parameters.AddWithValue("$kind", rec.TokenKind);
        cmd.Parameters.AddWithValue("$value", rec.TokenValue);
        cmd.Parameters.AddWithValue("$created", rec.CreatedAt);
        cmd.Parameters.AddWithValue("$expires", SqliteEngine.DbValue(rec.ExpiresAt));
        cmd.Parameters.AddWithValue("$revoked", SqliteEngine.DbValue(rec.RevokedAt));
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }

    public async Task<SessionTokenRecord?> GetSessionTokenAsync(
        string sessionId, string tokenKind, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = """
            SELECT session_id, token_kind, token_value, created_at, expires_at, revoked_at
            FROM cp_session_tokens WHERE session_id = $id AND token_kind = $kind
            """;
        cmd.Parameters.AddWithValue("$id", sessionId);
        cmd.Parameters.AddWithValue("$kind", tokenKind);
        await using var r = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        if (!await r.ReadAsync(ct).ConfigureAwait(false))
        {
            return null;
        }

        return new SessionTokenRecord
        {
            SessionId = r.GetString(0),
            TokenKind = r.GetString(1),
            TokenValue = r.GetString(2),
            CreatedAt = r.GetDouble(3),
            ExpiresAt = SqliteEngine.NullableDouble(r, 4),
            RevokedAt = SqliteEngine.NullableDouble(r, 5),
        };
    }

    public async Task CreateResumeTokenAsync(ResumeTokenRecord rec, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = """
            INSERT INTO cp_resume_tokens(token_value, session_id, role, created_at,
                expires_at, was_hijack_owner, revoked_at)
            VALUES($value, $id, $role, $created, $expires, $owner, $revoked)
            """;
        cmd.Parameters.AddWithValue("$value", rec.TokenValue);
        cmd.Parameters.AddWithValue("$id", rec.SessionId);
        cmd.Parameters.AddWithValue("$role", rec.Role);
        cmd.Parameters.AddWithValue("$created", rec.CreatedAt);
        cmd.Parameters.AddWithValue("$expires", rec.ExpiresAt);
        cmd.Parameters.AddWithValue("$owner", rec.WasHijackOwner ? 1 : 0);
        cmd.Parameters.AddWithValue("$revoked", SqliteEngine.DbValue(rec.RevokedAt));
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }

    private static ResumeTokenRecord ReadResume(SqliteDataReader r) => new()
    {
        TokenValue = r.GetString(0),
        SessionId = r.GetString(1),
        Role = r.GetString(2),
        CreatedAt = r.GetDouble(3),
        ExpiresAt = r.GetDouble(4),
        WasHijackOwner = r.GetInt64(5) != 0,
        RevokedAt = SqliteEngine.NullableDouble(r, 6),
    };

    /// <summary>Returns null for an absent OR revoked token.</summary>
    public async Task<ResumeTokenRecord?> GetResumeTokenAsync(
        string tokenValue, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = $"SELECT {ResumeColumns} FROM cp_resume_tokens WHERE token_value = $v";
        cmd.Parameters.AddWithValue("$v", tokenValue);
        await using var r = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        if (!await r.ReadAsync(ct).ConfigureAwait(false))
        {
            return null;
        }

        var rec = ReadResume(r);
        return rec.RevokedAt is null ? rec : null;
    }

    public async Task RevokeResumeTokenAsync(
        string tokenValue, double revokedAt, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText =
            "UPDATE cp_resume_tokens SET revoked_at = $at WHERE token_value = $v AND revoked_at IS NULL";
        cmd.Parameters.AddWithValue("$at", revokedAt);
        cmd.Parameters.AddWithValue("$v", tokenValue);
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }

    /// <summary>
    /// Atomically revokes and returns the token on the first call, null on any
    /// later one. The UPDATE ... WHERE revoked_at IS NULL is the single-use
    /// gate: only one caller can affect a row, so a racing second caller sees
    /// zero rows changed and gets null.
    /// </summary>
    public async Task<ResumeTokenRecord?> ConsumeResumeTokenAsync(
        string tokenValue, double revokedAt, CancellationToken ct = default)
    {
        int affected;
        await using (var update = engine.Command())
        {
            update.CommandText =
                "UPDATE cp_resume_tokens SET revoked_at = $at WHERE token_value = $v AND revoked_at IS NULL";
            update.Parameters.AddWithValue("$at", revokedAt);
            update.Parameters.AddWithValue("$v", tokenValue);
            affected = await update.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
        }

        if (affected == 0)
        {
            return null;
        }

        await using var cmd = engine.Command();
        cmd.CommandText = $"SELECT {ResumeColumns} FROM cp_resume_tokens WHERE token_value = $v";
        cmd.Parameters.AddWithValue("$v", tokenValue);
        await using var r = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        return await r.ReadAsync(ct).ConfigureAwait(false) ? ReadResume(r) : null;
    }
}

/// <summary>SQLite IApprovalStore. Port of SqliteApprovalStore.</summary>
internal sealed class SqliteApprovalStore(SqliteEngine engine) : IApprovalStore
{
    private const string Columns =
        "approval_id, session_id, command, requested_by, state, created_at, resolved_at, resolved_by";

    private static ApprovalRecord Read(SqliteDataReader r) => new()
    {
        ApprovalId = r.GetString(0),
        SessionId = r.GetString(1),
        Command = r.GetString(2),
        RequestedBy = SqliteEngine.NullableString(r, 3),
        State = r.GetString(4),
        CreatedAt = r.GetDouble(5),
        ResolvedAt = SqliteEngine.NullableDouble(r, 6),
        ResolvedBy = SqliteEngine.NullableString(r, 7),
    };

    public async Task PutApprovalAsync(ApprovalRecord rec, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = """
            INSERT INTO cp_approvals(approval_id, session_id, command, requested_by,
                state, created_at, resolved_at, resolved_by)
            VALUES($id, $session, $command, $by, $state, $created, $resolvedAt, $resolvedBy)
            ON CONFLICT(approval_id) DO UPDATE SET
                session_id = excluded.session_id,
                command = excluded.command,
                requested_by = excluded.requested_by,
                state = excluded.state,
                created_at = excluded.created_at,
                resolved_at = excluded.resolved_at,
                resolved_by = excluded.resolved_by
            """;
        cmd.Parameters.AddWithValue("$id", rec.ApprovalId);
        cmd.Parameters.AddWithValue("$session", rec.SessionId);
        cmd.Parameters.AddWithValue("$command", rec.Command);
        cmd.Parameters.AddWithValue("$by", SqliteEngine.DbValue(rec.RequestedBy));
        cmd.Parameters.AddWithValue("$state", rec.State);
        cmd.Parameters.AddWithValue("$created", rec.CreatedAt);
        cmd.Parameters.AddWithValue("$resolvedAt", SqliteEngine.DbValue(rec.ResolvedAt));
        cmd.Parameters.AddWithValue("$resolvedBy", SqliteEngine.DbValue(rec.ResolvedBy));
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }

    public async Task<ApprovalRecord?> GetApprovalAsync(string approvalId, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = $"SELECT {Columns} FROM cp_approvals WHERE approval_id = $id";
        cmd.Parameters.AddWithValue("$id", approvalId);
        await using var r = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        return await r.ReadAsync(ct).ConfigureAwait(false) ? Read(r) : null;
    }

    /// <summary>Pending approvals ordered by (created_at, approval_id).</summary>
    public async Task<IReadOnlyList<ApprovalRecord>> ListPendingAsync(CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText =
            $"SELECT {Columns} FROM cp_approvals WHERE state = 'pending' ORDER BY created_at, approval_id";
        await using var r = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        var list = new List<ApprovalRecord>();
        while (await r.ReadAsync(ct).ConfigureAwait(false))
        {
            list.Add(Read(r));
        }

        return list;
    }
}

/// <summary>SQLite ILeaseStore. Port of SqliteLeaseStore.</summary>
internal sealed class SqliteLeaseStore(SqliteEngine engine) : ILeaseStore
{
    public async Task PutLeaseAsync(LeaseRecord rec, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = """
            INSERT INTO cp_leases(session_id, hijack_id, owner, lease_expires_at, created_at, deleted_at)
            VALUES($id, $hijack, $owner, $expires, $created, $deleted)
            ON CONFLICT(session_id) DO UPDATE SET
                hijack_id = excluded.hijack_id,
                owner = excluded.owner,
                lease_expires_at = excluded.lease_expires_at,
                created_at = excluded.created_at,
                deleted_at = excluded.deleted_at
            """;
        cmd.Parameters.AddWithValue("$id", rec.SessionId);
        cmd.Parameters.AddWithValue("$hijack", rec.HijackId);
        cmd.Parameters.AddWithValue("$owner", rec.Owner);
        cmd.Parameters.AddWithValue("$expires", rec.LeaseExpiresAt);
        cmd.Parameters.AddWithValue("$created", rec.CreatedAt);
        cmd.Parameters.AddWithValue("$deleted", SqliteEngine.DbValue(rec.DeletedAt));
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }

    /// <summary>Returns null when absent OR soft-deleted.</summary>
    public async Task<LeaseRecord?> GetLeaseAsync(string sessionId, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = """
            SELECT session_id, hijack_id, owner, lease_expires_at, created_at, deleted_at
            FROM cp_leases WHERE session_id = $id
            """;
        cmd.Parameters.AddWithValue("$id", sessionId);
        await using var r = await cmd.ExecuteReaderAsync(ct).ConfigureAwait(false);
        if (!await r.ReadAsync(ct).ConfigureAwait(false))
        {
            return null;
        }

        var deletedAt = SqliteEngine.NullableDouble(r, 5);
        if (deletedAt is not null)
        {
            return null;
        }

        return new LeaseRecord
        {
            SessionId = r.GetString(0),
            HijackId = r.GetString(1),
            Owner = r.GetString(2),
            LeaseExpiresAt = r.GetDouble(3),
            CreatedAt = r.GetDouble(4),
            DeletedAt = null,
        };
    }

    /// <summary>Soft-deletes by stamping deleted_at with the engine clock.</summary>
    public async Task ClearLeaseAsync(string sessionId, CancellationToken ct = default)
    {
        await using var cmd = engine.Command();
        cmd.CommandText = "UPDATE cp_leases SET deleted_at = $at WHERE session_id = $id";
        cmd.Parameters.AddWithValue("$at", engine.Now());
        cmd.Parameters.AddWithValue("$id", sessionId);
        await cmd.ExecuteNonQueryAsync(ct).ConfigureAwait(false);
    }
}
