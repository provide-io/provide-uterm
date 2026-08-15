//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.ControlPlane;

public sealed class EngineCapabilities
{
    public bool Durable { get; init; }
    public bool Sqlite { get; init; }
    public bool AuditChain { get; init; }
}

public interface ITx
{
    Task CommitAsync(CancellationToken cancellationToken = default);
    Task RollbackAsync(CancellationToken cancellationToken = default);
}

// The record shapes below mirror the cp_* columns shared with the Python and Go
// control planes, so a database written by any of the three is readable by the
// others. Fields are named for their columns; nullable columns are nullable
// here.

public sealed class SessionRecord
{
    public string SessionId { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string ConnectorType { get; set; } = "";
    public string? Owner { get; set; }

    /// <summary>"public" | "operator" | "private".</summary>
    public string Visibility { get; set; } = "private";

    /// <summary>"waiting" | "running" | "stopped" | "error" | "deleted".</summary>
    public string LifecycleState { get; set; } = "waiting";
    public double CreatedAt { get; set; }
    public double UpdatedAt { get; set; }
    public double? DeletedAt { get; set; }
}

public sealed class SessionTokenRecord
{
    public string SessionId { get; set; } = "";
    public string TokenKind { get; set; } = "";
    public string TokenValue { get; set; } = "";
    public double CreatedAt { get; set; }
    public double? ExpiresAt { get; set; }
    public double? RevokedAt { get; set; }
}

public sealed class ResumeTokenRecord
{
    public string TokenValue { get; set; } = "";
    public string SessionId { get; set; } = "";
    public string Role { get; set; } = "";
    public double CreatedAt { get; set; }
    public double ExpiresAt { get; set; }

    /// <summary>Stored as INTEGER 0/1.</summary>
    public bool WasHijackOwner { get; set; }
    public double? RevokedAt { get; set; }
}

public sealed class ApprovalRecord
{
    public string ApprovalId { get; set; } = "";
    public string SessionId { get; set; } = "";
    public string Command { get; set; } = "";
    public string? RequestedBy { get; set; }

    /// <summary>"pending" | "approved" | "rejected".</summary>
    public string State { get; set; } = "pending";
    public double CreatedAt { get; set; }
    public double? ResolvedAt { get; set; }
    public string? ResolvedBy { get; set; }
}

public sealed class LeaseRecord
{
    public string SessionId { get; set; } = "";
    public string HijackId { get; set; } = "";
    public string Owner { get; set; } = "";
    public double LeaseExpiresAt { get; set; }
    public double CreatedAt { get; set; }
    public double? DeletedAt { get; set; }
}

/// <summary>
/// A persisted graphical-target definition — the storage shape of
/// <see cref="Server.GraphicalTargetDefinition"/>.
///
/// Config holds the protocol-specific parameter object as JSON text rather than
/// a column per protocol, so adding a protocol needs no migration. It is not a
/// secret and survives the redacted copy that crosses REST.
/// </summary>
public sealed class GraphicalTargetRecord
{
    public string TargetId { get; set; } = "";
    public string TenantId { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string Protocol { get; set; } = "";
    public string? Endpoint { get; set; }
    public string? Secret { get; set; }
    public long Width { get; set; }
    public long Height { get; set; }

    /// <summary>Stored as INTEGER 0/1.</summary>
    public bool IsSystem { get; set; }

    /// <summary>Stored as INTEGER 0/1.</summary>
    public bool IsStatic { get; set; }
    public string? CaSecretRef { get; set; }
    public string? ClientCertSecretRef { get; set; }
    public string? ClientKeySecretRef { get; set; }
    public string Config { get; set; } = "{}";
    public string? CreatedBy { get; set; }
    public double CreatedAt { get; set; }
    public string? UpdatedBy { get; set; }
    public double? UpdatedAt { get; set; }
}

public sealed class AuditHead
{
    public long Seq { get; set; }
    public string RecordHash { get; set; } = "";
}

public interface ISessionStore
{
    Task UpsertAsync(SessionRecord rec, CancellationToken ct = default);
    Task<SessionRecord?> GetAsync(string sessionId, CancellationToken ct = default);
    Task MarkDeletedAsync(string sessionId, double deletedAt, CancellationToken ct = default);
}

public interface ITokenStore
{
    Task PutSessionTokenAsync(SessionTokenRecord rec, CancellationToken ct = default);
    Task<SessionTokenRecord?> GetSessionTokenAsync(string sessionId, string tokenKind, CancellationToken ct = default);
    Task CreateResumeTokenAsync(ResumeTokenRecord rec, CancellationToken ct = default);
    Task<ResumeTokenRecord?> GetResumeTokenAsync(string tokenValue, CancellationToken ct = default);
    Task RevokeResumeTokenAsync(string tokenValue, double revokedAt, CancellationToken ct = default);
    Task<ResumeTokenRecord?> ConsumeResumeTokenAsync(string tokenValue, double revokedAt, CancellationToken ct = default);
}

public interface IApprovalStore
{
    Task PutApprovalAsync(ApprovalRecord rec, CancellationToken ct = default);
    Task<ApprovalRecord?> GetApprovalAsync(string approvalId, CancellationToken ct = default);
    Task<IReadOnlyList<ApprovalRecord>> ListPendingAsync(CancellationToken ct = default);
}

public interface ILeaseStore
{
    Task PutLeaseAsync(LeaseRecord rec, CancellationToken ct = default);
    Task<LeaseRecord?> GetLeaseAsync(string sessionId, CancellationToken ct = default);
    Task ClearLeaseAsync(string sessionId, CancellationToken ct = default);
}

/// <summary>
/// Persistence for graphical-target definitions.
///
/// Tenant isolation is NOT enforced here: this is a row layer, and the caller
/// already holds a scope derived from the authenticated principal. Gating here
/// too would double-check reads and hide scope bugs from the registry's tests.
/// </summary>
public interface IGraphicalTargetStore
{
    Task PutAsync(GraphicalTargetRecord rec, CancellationToken ct = default);
    Task<GraphicalTargetRecord?> GetAsync(string targetId, CancellationToken ct = default);

    /// <summary>Every row, ordered by target_id.</summary>
    Task<IReadOnlyList<GraphicalTargetRecord>> ListAsync(CancellationToken ct = default);

    /// <summary>True when a row was actually removed.</summary>
    Task<bool> DeleteAsync(string targetId, CancellationToken ct = default);
}

public interface IEngine
{
    EngineCapabilities Capabilities();
    Task OpenAsync(CancellationToken ct = default);
    Task CloseAsync(CancellationToken ct = default);
    Task MigrateAsync(CancellationToken ct = default);
    Task<ITx> BeginAsync(CancellationToken ct = default);
    Task<int> ReapAsync(double now, int retentionS, CancellationToken ct = default);
    Task<AuditHead?> GetAuditHeadAsync(CancellationToken ct = default);
    Task SetAuditHeadAsync(long seq, string recordHash, CancellationToken ct = default);
    ISessionStore Sessions();
    ITokenStore Tokens();
    IApprovalStore Approvals();
    ILeaseStore Leases();
    IGraphicalTargetStore GraphicalTargets();
}

public sealed class MemoryTx : ITx
{
    private bool _done;

    public Task CommitAsync(CancellationToken cancellationToken = default)
    {
        _done = true;
        return Task.CompletedTask;
    }

    public Task RollbackAsync(CancellationToken cancellationToken = default)
    {
        _done = true;
        return Task.CompletedTask;
    }

    public bool IsDone => _done;
}

/// <summary>In-memory control-plane engine.</summary>
public sealed class MemoryEngine : IEngine
{
    private readonly object _lock = new();
    private readonly Dictionary<string, SessionRecord> _sessions = new();
    private readonly Dictionary<string, SessionTokenRecord> _sessionTokens = new();
    private readonly Dictionary<string, ResumeTokenRecord> _resumeTokens = new();
    private readonly Dictionary<string, ApprovalRecord> _approvals = new();
    private readonly Dictionary<string, LeaseRecord> _leases = new();
    private readonly Dictionary<string, GraphicalTargetRecord> _graphicalTargets = new();
    private AuditHead? _auditHead;

    public EngineCapabilities Capabilities() => new() { Durable = false, Sqlite = false, AuditChain = true };

    // Open/Close are no-ops, as they are on the reference's MemoryEngine
    // (provide/uterm/control/plane/memory/engine.py): an in-memory engine has
    // no connection to establish and no handle to release. The port used to
    // keep an `_open` flag here that nothing ever read, which advertised a
    // closed-state guard that did not exist.
    public Task OpenAsync(CancellationToken ct = default) => Task.CompletedTask;

    public Task CloseAsync(CancellationToken ct = default) => Task.CompletedTask;

    public Task MigrateAsync(CancellationToken ct = default) => Task.CompletedTask;

    public Task<ITx> BeginAsync(CancellationToken ct = default) => Task.FromResult<ITx>(new MemoryTx());

    public Task<int> ReapAsync(double now, int retentionS, CancellationToken ct = default)
    {
        var cutoff = now - retentionS;
        var removed = 0;
        lock (_lock)
        {
            foreach (var id in _sessions.Keys.ToList())
            {
                var s = _sessions[id];
                if (s.DeletedAt is double d && d < cutoff)
                {
                    _sessions.Remove(id);
                    removed++;
                }
            }
        }

        return Task.FromResult(removed);
    }

    public Task<AuditHead?> GetAuditHeadAsync(CancellationToken ct = default)
    {
        lock (_lock)
        {
            return Task.FromResult(_auditHead);
        }
    }

    public Task SetAuditHeadAsync(long seq, string recordHash, CancellationToken ct = default)
    {
        lock (_lock)
        {
            if (_auditHead is not null && seq <= _auditHead.Seq)
            {
                return Task.CompletedTask;
            }

            _auditHead = new AuditHead { Seq = seq, RecordHash = recordHash };
        }

        return Task.CompletedTask;
    }

    public ISessionStore Sessions() => new SessionStoreAdapter(this);
    public ITokenStore Tokens() => new TokenStoreAdapter(this);
    public IApprovalStore Approvals() => new ApprovalStoreAdapter(this);
    public ILeaseStore Leases() => new LeaseStoreAdapter(this);
    public IGraphicalTargetStore GraphicalTargets() => new GraphicalTargetStoreAdapter(this);

    private sealed class GraphicalTargetStoreAdapter(MemoryEngine eng) : IGraphicalTargetStore
    {
        public Task PutAsync(GraphicalTargetRecord rec, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                eng._graphicalTargets[rec.TargetId] = rec;
            }

            return Task.CompletedTask;
        }

        public Task<GraphicalTargetRecord?> GetAsync(string targetId, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                return Task.FromResult(eng._graphicalTargets.TryGetValue(targetId, out var r) ? r : null);
            }
        }

        // Ordered by target_id so this backend agrees with the SQLite one, which
        // gets its order from ORDER BY.
        public Task<IReadOnlyList<GraphicalTargetRecord>> ListAsync(CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                var list = eng._graphicalTargets.Values
                    .OrderBy(t => t.TargetId, StringComparer.Ordinal)
                    .ToList();
                return Task.FromResult<IReadOnlyList<GraphicalTargetRecord>>(list);
            }
        }

        public Task<bool> DeleteAsync(string targetId, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                return Task.FromResult(eng._graphicalTargets.Remove(targetId));
            }
        }
    }

    private sealed class SessionStoreAdapter(MemoryEngine eng) : ISessionStore
    {
        public Task UpsertAsync(SessionRecord rec, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                eng._sessions[rec.SessionId] = rec;
            }

            return Task.CompletedTask;
        }

        public Task<SessionRecord?> GetAsync(string sessionId, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                return Task.FromResult(eng._sessions.TryGetValue(sessionId, out var r) ? r : null);
            }
        }

        public Task MarkDeletedAsync(string sessionId, double deletedAt, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                if (eng._sessions.TryGetValue(sessionId, out var r))
                {
                    r.DeletedAt = deletedAt;
                    r.LifecycleState = "deleted";
                }
            }

            return Task.CompletedTask;
        }
    }

    private sealed class TokenStoreAdapter(MemoryEngine eng) : ITokenStore
    {
        private static string Key(string sessionId, string kind) => sessionId + "\0" + kind;

        public Task PutSessionTokenAsync(SessionTokenRecord rec, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                eng._sessionTokens[Key(rec.SessionId, rec.TokenKind)] = rec;
            }

            return Task.CompletedTask;
        }

        public Task<SessionTokenRecord?> GetSessionTokenAsync(string sessionId, string tokenKind, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                return Task.FromResult(eng._sessionTokens.TryGetValue(Key(sessionId, tokenKind), out var r) ? r : null);
            }
        }

        public Task CreateResumeTokenAsync(ResumeTokenRecord rec, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                eng._resumeTokens[rec.TokenValue] = rec;
            }

            return Task.CompletedTask;
        }

        public Task<ResumeTokenRecord?> GetResumeTokenAsync(string tokenValue, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                if (!eng._resumeTokens.TryGetValue(tokenValue, out var r) || r.RevokedAt is not null)
                {
                    return Task.FromResult<ResumeTokenRecord?>(null);
                }

                return Task.FromResult<ResumeTokenRecord?>(r);
            }
        }

        public Task RevokeResumeTokenAsync(string tokenValue, double revokedAt, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                if (eng._resumeTokens.TryGetValue(tokenValue, out var r))
                {
                    r.RevokedAt = revokedAt;
                }
            }

            return Task.CompletedTask;
        }

        public Task<ResumeTokenRecord?> ConsumeResumeTokenAsync(string tokenValue, double revokedAt, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                if (!eng._resumeTokens.TryGetValue(tokenValue, out var r) || r.RevokedAt is not null)
                {
                    return Task.FromResult<ResumeTokenRecord?>(null);
                }

                r.RevokedAt = revokedAt;
                return Task.FromResult<ResumeTokenRecord?>(r);
            }
        }
    }

    private sealed class ApprovalStoreAdapter(MemoryEngine eng) : IApprovalStore
    {
        public Task PutApprovalAsync(ApprovalRecord rec, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                eng._approvals[rec.ApprovalId] = rec;
            }

            return Task.CompletedTask;
        }

        public Task<ApprovalRecord?> GetApprovalAsync(string approvalId, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                return Task.FromResult(eng._approvals.TryGetValue(approvalId, out var r) ? r : null);
            }
        }

        public Task<IReadOnlyList<ApprovalRecord>> ListPendingAsync(CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                var list = eng._approvals.Values
                    .Where(a => a.State == "pending")
                    .OrderBy(a => a.CreatedAt)
                    .ThenBy(a => a.ApprovalId, StringComparer.Ordinal)
                    .ToList();
                return Task.FromResult<IReadOnlyList<ApprovalRecord>>(list);
            }
        }
    }

    private sealed class LeaseStoreAdapter(MemoryEngine eng) : ILeaseStore
    {
        public Task PutLeaseAsync(LeaseRecord rec, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                eng._leases[rec.SessionId] = rec;
            }

            return Task.CompletedTask;
        }

        public Task<LeaseRecord?> GetLeaseAsync(string sessionId, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                return Task.FromResult(eng._leases.TryGetValue(sessionId, out var r) ? r : null);
            }
        }

        public Task ClearLeaseAsync(string sessionId, CancellationToken ct = default)
        {
            lock (eng._lock)
            {
                eng._leases.Remove(sessionId);
            }

            return Task.CompletedTask;
        }
    }
}
