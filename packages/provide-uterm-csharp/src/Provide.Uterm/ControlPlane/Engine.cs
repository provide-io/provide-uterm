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

public sealed class SessionRecord
{
    public string SessionId { get; set; } = "";
    public string State { get; set; } = "active";
    public double CreatedAt { get; set; }
    public double? DeletedAt { get; set; }
    public Dictionary<string, object?> Metadata { get; set; } = new();
}

public sealed class SessionTokenRecord
{
    public string SessionId { get; set; } = "";
    public string TokenKind { get; set; } = "";
    public string TokenHash { get; set; } = "";
    public double ExpiresAt { get; set; }
}

public sealed class ResumeTokenRecord
{
    public string TokenValue { get; set; } = "";
    public string SessionId { get; set; } = "";
    public double ExpiresAt { get; set; }
    public double? RevokedAt { get; set; }
}

public sealed class ApprovalRecord
{
    public string ApprovalId { get; set; } = "";
    public string SessionId { get; set; } = "";
    public string Command { get; set; } = "";
    public string Status { get; set; } = "pending";
    public double CreatedAt { get; set; }
}

public sealed class LeaseRecord
{
    public string SessionId { get; set; } = "";
    public string Principal { get; set; } = "";
    public string HijackId { get; set; } = "";
    public double ExpiresAt { get; set; }
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
    private AuditHead? _auditHead;
    private bool _open;

    public EngineCapabilities Capabilities() => new() { Durable = false, Sqlite = false, AuditChain = true };

    public Task OpenAsync(CancellationToken ct = default)
    {
        _open = true;
        return Task.CompletedTask;
    }

    public Task CloseAsync(CancellationToken ct = default)
    {
        _open = false;
        return Task.CompletedTask;
    }

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
                    r.State = "deleted";
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
                    .Where(a => a.Status == "pending")
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
