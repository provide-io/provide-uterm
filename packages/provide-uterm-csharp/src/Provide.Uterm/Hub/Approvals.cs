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
}

/// <summary>In-memory approval request store.</summary>
public sealed class InMemoryApprovalStore
{
    private const double PruneTtl = 3600.0;
    private readonly object _gate = new();
    private readonly Dictionary<string, ApprovalRequest> _requests = new();
    private readonly IClock _clock;

    public Action<string>? OnExpired { get; set; }

    public InMemoryApprovalStore(IClock? clock = null)
    {
        _clock = ClockUtil.OrDefault(clock);
    }

    public void Add(ApprovalRequest req)
    {
        lock (_gate) _requests[req.Id] = req;
    }

    public ApprovalRequest? Get(string requestId)
    {
        lock (_gate) return _requests.TryGetValue(requestId, out var r) ? r : null;
    }

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

            pending = _requests.Values.Where(r => r.Status == ApprovalStatus.Pending).ToList();
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
