//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

public enum ApprovalStatus
{
    Pending,
    Approved,
    Rejected,
    Timeout,

    /// <summary>
    /// Approved by an admin, but the submitting browser's capability fence had
    /// already lapsed when the held command was injected, so nothing reached the
    /// worker. Distinct from <see cref="Rejected"/>: nobody refused the command,
    /// the authority to run it expired. Port of Go <c>ApprovalRefused</c>.
    /// </summary>
    Refused,
}

public sealed class ApprovalRequest
{
    public required string Id { get; set; }
    public required string WorkerId { get; set; }
    public required string SubmitterId { get; set; }
    public required string Command { get; set; }
    public ApprovalStatus Status { get; set; } = ApprovalStatus.Pending;
    public double CreatedAt { get; set; }
    public double ExpiresAt { get; set; }
    public string? GroupId { get; set; }
    public bool IsFanout { get; set; }

    /// <summary>
    /// Opaque store-assigned identity, distinguishing records when a pruned
    /// caller-controlled id is later reused. Assigned by
    /// <see cref="InMemoryApprovalStore.Add"/>; a caller-supplied value is
    /// ignored. Port of Go <c>ApprovalRequest.Revision</c>.
    /// </summary>
    public long Revision { get; set; }

    /// <summary>
    /// The browser whose keystroke was held, and the dashboard-ownership version
    /// it held at. Internal capability-fence data — deliberately absent from the
    /// approval routes' serialization. Port of Go <c>OriginBrowser</c> /
    /// <c>OriginGeneration</c>.
    /// </summary>
    public object? OriginBrowser { get; set; }

    /// <inheritdoc cref="OriginBrowser"/>
    public long OriginGeneration { get; set; }
}

/// <summary>In-memory approval request store.</summary>
public sealed class InMemoryApprovalStore
{
    private const double PruneTtl = 3600.0;
    private readonly object _gate = new();
    private readonly Dictionary<string, ApprovalRequest> _requests = new();
    private readonly IClock _clock;
    private long _nextRevision;

    public Action<string>? OnExpired { get; set; }

    public InMemoryApprovalStore(IClock? clock = null)
    {
        _clock = ClockUtil.OrDefault(clock);
    }

    /// <summary>
    /// Insert a copied request, refusing a duplicate id so a stale resolver can
    /// never claim a different request through identifier reuse. Port of Go
    /// <c>InMemoryApprovalStore.Add</c>.
    /// </summary>
    public bool Add(ApprovalRequest req)
    {
        lock (_gate)
        {
            if (_requests.ContainsKey(req.Id)) return false;
            var stored = Clone(req);
            stored.Revision = ++_nextRevision;
            _requests[req.Id] = stored;
            return true;
        }
    }

    public ApprovalRequest? Get(string requestId)
    {
        lock (_gate) return _requests.TryGetValue(requestId, out var r) ? Clone(r) : null;
    }

    /// <summary>
    /// Copy a stored record so a caller cannot mutate the store through the
    /// reference it was handed. Port of Go <c>cloneApprovalRequest</c>.
    /// </summary>
    private static ApprovalRequest Clone(ApprovalRequest req) =>
        new()
        {
            Id = req.Id,
            WorkerId = req.WorkerId,
            SubmitterId = req.SubmitterId,
            Command = req.Command,
            Status = req.Status,
            CreatedAt = req.CreatedAt,
            ExpiresAt = req.ExpiresAt,
            GroupId = req.GroupId,
            IsFanout = req.IsFanout,
            Revision = req.Revision,
            OriginBrowser = req.OriginBrowser,
            OriginGeneration = req.OriginGeneration,
        };

    /// <summary>
    /// Move one overdue request to <see cref="ApprovalStatus.Timeout"/>, and say
    /// whether it did. Caller must hold <see cref="_gate"/>, and must raise
    /// <see cref="OnExpired"/> outside it.
    ///
    /// Every read and write path runs this first, because the port has no sweep:
    /// <see cref="CleanupExpired"/> is the only thing that can retire a deadline
    /// and nothing in production calls it (Go ticks it from StartSweeps, Python
    /// from sweep_expired_approvals). Without the check here a request stayed
    /// Pending forever, so `POST /api/approvals/{id}/approve` granted a command
    /// whose deadline had passed arbitrarily long ago and answered 200. The
    /// reference makes the same check inline for the same reason
    /// (bridge/hub/approvals.py:109), and a sweep alone would still leave a
    /// window one tick wide.
    /// </summary>
    private static bool ExpireIfOverdueLocked(ApprovalRequest req, double now)
    {
        if (req.Status != ApprovalStatus.Pending || req.ExpiresAt >= now)
        {
            return false;
        }

        req.Status = ApprovalStatus.Timeout;
        return true;
    }

    /// <summary>Pending approvals (admin list). Port of Go Approvals.PendingApprovals.</summary>
    public IReadOnlyList<ApprovalRequest> PendingApprovals()
    {
        var expiredIds = new List<string>();
        List<ApprovalRequest> pending;
        lock (_gate)
        {
            var now = _clock.Wall();
            foreach (var req in _requests.Values)
            {
                if (ExpireIfOverdueLocked(req, now))
                {
                    expiredIds.Add(req.Id);
                }
            }

            pending = _requests.Values
                .Where(r => r.Status == ApprovalStatus.Pending)
                .Select(Clone)
                .ToList();
        }

        NotifyExpired(expiredIds);
        return pending;
    }

    public void Resolve(string requestId, ApprovalStatus status)
    {
        string? expiredId = null;
        lock (_gate)
        {
            if (_requests.TryGetValue(requestId, out var req))
            {
                if (ExpireIfOverdueLocked(req, _clock.Wall()))
                {
                    expiredId = req.Id;
                }
                else if (req.Status == ApprovalStatus.Pending)
                {
                    req.Status = status;
                }
            }
        }

        NotifyExpired(expiredId);
    }

    public bool Claim(string requestId, ApprovalStatus status)
    {
        string? expiredId = null;
        var claimed = false;
        lock (_gate)
        {
            if (_requests.TryGetValue(requestId, out var req))
            {
                if (ExpireIfOverdueLocked(req, _clock.Wall()))
                {
                    expiredId = req.Id;
                }
                else if (req.Status == ApprovalStatus.Pending)
                {
                    req.Status = status;
                    claimed = true;
                }
            }
        }

        NotifyExpired(expiredId);
        return claimed;
    }

    /// <summary>
    /// Atomically move a PENDING request of exactly <paramref name="revision"/>
    /// to <paramref name="status"/>, returning true only for the caller that
    /// performs the transition — so a held command is injected exactly once
    /// under concurrent approve/reject. Port of Go <c>ClaimRevision</c>.
    /// </summary>
    public bool ClaimRevision(string requestId, long revision, ApprovalStatus status)
    {
        string? expiredId = null;
        var claimed = false;
        lock (_gate)
        {
            if (_requests.TryGetValue(requestId, out var req) && req.Revision == revision)
            {
                if (ExpireIfOverdueLocked(req, _clock.Wall()))
                {
                    expiredId = req.Id;
                }
                else if (req.Status == ApprovalStatus.Pending)
                {
                    req.Status = status;
                    claimed = true;
                }
            }
        }

        NotifyExpired(expiredId);
        return claimed;
    }

    /// <summary>
    /// Record the terminal outcome after the one-shot claim has won. False means
    /// the record is gone or its identity changed (a pruned id was reused), in
    /// which case the caller must not publish an outcome for it. Port of Go
    /// <c>SetStatusRevision</c>.
    /// </summary>
    public bool SetStatusRevision(string requestId, long revision, ApprovalStatus status)
    {
        lock (_gate)
        {
            if (!_requests.TryGetValue(requestId, out var req) || req.Revision != revision) return false;
            req.Status = status;
            return true;
        }
    }

    private void NotifyExpired(string? expiredId)
    {
        if (expiredId is not null)
        {
            OnExpired?.Invoke(expiredId);
        }
    }

    private void NotifyExpired(IReadOnlyList<string> expiredIds)
    {
        foreach (var id in expiredIds)
        {
            OnExpired?.Invoke(id);
        }
    }

    public void CleanupExpired()
    {
        var now = _clock.Wall();
        var expiredIds = new List<string>();
        lock (_gate)
        {
            var toDelete = new List<string>();
            foreach (var (reqId, req) in _requests)
            {
                if (ExpireIfOverdueLocked(req, now))
                {
                    expiredIds.Add(req.Id);
                }
                else if (req.Status != ApprovalStatus.Pending && req.ExpiresAt + PruneTtl < now)
                {
                    toDelete.Add(reqId);
                }
            }

            foreach (var id in toDelete)
            {
                _requests.Remove(id);
            }
        }

        NotifyExpired(expiredIds);
    }
}
