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

    public string Mint(string workerId, string role)
    {
        lock (_gate)
        {
            var now = _clock.Monotonic();
            SweepExpired(now);
            while (_tokens.Count >= _capacity) EvictOldestExpiry();

            string token;
            do token = _tokenGenerator(); while (_tokens.ContainsKey(token));
            _tokens[token] = new ResumeTokenSession(
                token,
                workerId,
                role,
                now,
                now + LifetimeSeconds);
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
            return session;
        }
    }

    public bool MarkHijackOwner(string token, long ownershipVersion)
    {
        lock (_gate)
        {
            if (!_tokens.TryGetValue(token, out var session)) return false;
            session.WasHijackOwner = true;
            session.OwnershipVersion = ownershipVersion;
            return true;
        }
    }

    public bool Revoke(string token)
    {
        lock (_gate) return _tokens.Remove(token);
    }

    private void SweepExpired(double now)
    {
        foreach (var token in _tokens
                     .Where(pair => pair.Value.ExpiresAt <= now)
                     .Select(pair => pair.Key)
                     .ToArray())
        {
            _tokens.Remove(token);
        }
    }

    private void EvictOldestExpiry()
    {
        var oldest = _tokens.Values
            .OrderBy(session => session.ExpiresAt)
            .ThenBy(session => session.CreatedAt)
            .ThenBy(session => session.Token, StringComparer.Ordinal)
            .First();
        _tokens.Remove(oldest.Token);
    }

    private static string GenerateToken() =>
        Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
}
