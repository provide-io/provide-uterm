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

    public void Resolve(string requestId, ApprovalStatus status)
    {
        lock (_gate)
        {
            if (_requests.TryGetValue(requestId, out var req) && req.Status == ApprovalStatus.Pending)
            {
                req.Status = status;
            }
        }
    }

    public bool Claim(string requestId, ApprovalStatus status)
    {
        lock (_gate)
        {
            if (!_requests.TryGetValue(requestId, out var req) || req.Status != ApprovalStatus.Pending)
            {
                return false;
            }

            req.Status = status;
            return true;
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
                if (req.Status == ApprovalStatus.Pending && req.ExpiresAt < now)
                {
                    req.Status = ApprovalStatus.Timeout;
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

        if (OnExpired is not null)
        {
            foreach (var id in expiredIds)
            {
                OnExpired(id);
            }
        }
    }
}
