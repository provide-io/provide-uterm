//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using Provide.Uterm.Hub;

namespace Provide.Uterm.Server;

/// <summary>State carried by one single-use browser resume token.</summary>
internal sealed record ResumeTokenSession(
    string Token,
    string WorkerId,
    string Role,
    double CreatedAt,
    double ExpiresAt)
{
    public bool WasDisconnected { get; set; }
    public bool WasHijackOwner { get; set; }
    public long? OwnershipVersion { get; set; }
}

/// <summary>Bounded, expiring, single-use in-process resume-token storage.</summary>
internal sealed class ResumeTokenStore
{
    internal const double LifetimeSeconds = 300;
    internal const int DefaultCapacity = 10_000;

    private readonly object _gate = new();
    private readonly Dictionary<string, ResumeTokenSession> _tokens = new(StringComparer.Ordinal);
    private readonly SortedSet<ResumeTokenSession> _expiryIndex = new(ExpiryComparer.Instance);
    private readonly IClock _clock;
    private readonly int _capacity;
    private readonly Func<string> _tokenGenerator;

    public ResumeTokenStore(
        IClock clock,
        int capacity = DefaultCapacity,
        Func<string>? tokenGenerator = null)
    {
        _clock = clock ?? throw new ArgumentNullException(nameof(clock));
        _capacity = Math.Max(1, capacity);
        _tokenGenerator = tokenGenerator ?? GenerateToken;
    }

    public int Count
    {
        get { lock (_gate) return _tokens.Count; }
    }

    internal int ExpiryIndexCount
    {
        get { lock (_gate) return _expiryIndex.Count; }
    }

    public string Mint(string workerId, string role)
    {
        lock (_gate)
        {
            var now = _clock.Monotonic();
            SweepExpired(now);
            while (_tokens.Count >= _capacity) EvictOldestExpiry();

            string token;
            do token = _tokenGenerator(); while (_tokens.ContainsKey(token));
            var session = new ResumeTokenSession(
                token,
                workerId,
                role,
                now,
                now + LifetimeSeconds);
            _tokens[token] = session;
            _expiryIndex.Add(session);
            return token;
        }
    }

    public ResumeTokenSession? Consume(string token)
    {
        lock (_gate)
        {
            var now = _clock.Monotonic();
            SweepExpired(now);
            if (!_tokens.Remove(token, out var session)) return null;
            _expiryIndex.Remove(session);
            return session;
        }
    }

    public bool MarkDisconnected(string token, long? ownershipVersion = null)
    {
        lock (_gate)
        {
            if (!_tokens.TryGetValue(token, out var session)) return false;
            session.WasDisconnected = true;
            if (ownershipVersion is { } version)
            {
                session.WasHijackOwner = true;
                session.OwnershipVersion = version;
            }
            return true;
        }
    }

    public bool MarkHijackOwner(string token, long ownershipVersion) =>
        MarkDisconnected(token, ownershipVersion);

    public bool Revoke(string token)
    {
        lock (_gate)
        {
            if (!_tokens.Remove(token, out var session)) return false;
            _expiryIndex.Remove(session);
            return true;
        }
    }

    private void SweepExpired(double now)
    {
        while (_expiryIndex.Min is { } oldest && oldest.ExpiresAt <= now)
        {
            _expiryIndex.Remove(oldest);
            _tokens.Remove(oldest.Token);
        }
    }

    private void EvictOldestExpiry()
    {
        var oldest = _expiryIndex.Min!;
        _expiryIndex.Remove(oldest);
        _tokens.Remove(oldest.Token);
    }

    private sealed class ExpiryComparer : IComparer<ResumeTokenSession>
    {
        public static ExpiryComparer Instance { get; } = new();

        public int Compare(ResumeTokenSession? left, ResumeTokenSession? right)
        {
            if (ReferenceEquals(left, right)) return 0;
            if (left is null) return -1;
            if (right is null) return 1;
            var byExpiry = left.ExpiresAt.CompareTo(right.ExpiresAt);
            if (byExpiry != 0) return byExpiry;
            var byCreation = left.CreatedAt.CompareTo(right.CreatedAt);
            return byCreation != 0
                ? byCreation
                : StringComparer.Ordinal.Compare(left.Token, right.Token);
        }
    }

    private static string GenerateToken() =>
        Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
}
