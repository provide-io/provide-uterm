//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;
using Org.BouncyCastle.Crypto.Digests;

namespace Provide.Uterm.Tunnel;

/// <summary>Role a tunnel invite grants when consumed.</summary>
public enum TunnelRole
{
    Viewer,
    Operator,
}

/// <summary>
/// Tunnel token / invite types and helpers.
/// Port of packages/provide-uterm-go/tunnel.
/// </summary>
public static class TunnelConstants
{
    public const double InviteTtlS = 300.0;
}

public sealed class TokenRecord
{
    public string WorkerTokenHash { get; set; } = "";
    public string ShareTokenHash { get; set; } = "";
    public string ControlTokenHash { get; set; } = "";
    public double CreatedAt { get; set; }
    public double ExpiresAt { get; set; }
    public string? IssuedIp { get; set; }
    public string TunnelType { get; set; } = "";
    public string SharePage { get; set; } = "";
}

public sealed class Invite
{
    public string SessionId { get; set; } = "";
    public TunnelRole Role { get; set; }
    public string TunnelToken { get; set; } = "";
    public double ExpiresAt { get; set; }
    public string? IssuedIp { get; set; }
}

/// <summary>
/// Token hashing / verification. BLAKE2b-256 hex digests, byte-for-byte with
/// Python hashlib.blake2b(..., digest_size=32).hexdigest() and Go blake2b.Sum256.
/// </summary>
public static class TunnelTokens
{
    /// <summary>
    /// BLAKE2b-256 hex digest of <paramref name="token"/> UTF-8 bytes.
    /// Empty plain returns empty string (matches Python/Go).
    /// </summary>
    public static string HashToken(string token)
    {
        if (string.IsNullOrEmpty(token))
        {
            return "";
        }

        var input = Encoding.UTF8.GetBytes(token);
        // Blake2bDigest ctor takes bit length of output (256 → 32 bytes).
        var digest = new Blake2bDigest(256);
        digest.BlockUpdate(input, 0, input.Length);
        var output = new byte[digest.GetDigestSize()];
        digest.DoFinal(output, 0);
        return Convert.ToHexString(output).ToLowerInvariant();
    }

    /// <summary>
    /// Constant-time compare of HashToken(plain) against stored hash.
    /// Empty plain or empty stored hash never authenticates (Python/Go parity).
    /// </summary>
    public static bool VerifyToken(string token, string tokenHash)
    {
        if (string.IsNullOrEmpty(token) || string.IsNullOrEmpty(tokenHash))
        {
            return false;
        }

        var computed = HashToken(token);
        return CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(computed),
            Encoding.UTF8.GetBytes(tokenHash));
    }

    public static bool InviteMatchesTokenHash(Invite? invite, string tokenHash) =>
        invite is not null && VerifyToken(invite.TunnelToken, tokenHash);

    /// <summary>
    /// URL-safe base64 (no padding) bearer from 32 bytes of crypto RNG.
    /// Shape matches Python secrets.token_urlsafe(32) / Go GenerateToken.
    /// </summary>
    public static string GenerateToken()
    {
        var bytes = new byte[32];
        RandomNumberGenerator.Fill(bytes);
        return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }
}

/// <summary>In-memory tunnel token store (Go MemStore parity).</summary>
public sealed class MemoryTunnelStore
{
    private readonly object _lock = new();
    private readonly Dictionary<string, TokenRecord> _tokens = new();
    private readonly Dictionary<string, Invite> _invites = new();

    public void PutToken(string tunnelId, TokenRecord record)
    {
        lock (_lock)
        {
            _tokens[tunnelId] = record;
        }
    }

    public TokenRecord? GetToken(string tunnelId)
    {
        lock (_lock)
        {
            return _tokens.TryGetValue(tunnelId, out var r) ? r : null;
        }
    }

    /// <summary>
    /// Does <paramref name="sessionId"/> hold a live tunnel share at
    /// <paramref name="now"/>?
    /// </summary>
    /// <remarks>
    /// <para>
    /// Asked by the webhook egress guard (<c>conformance/EGRESS_GUARD.md</c> §4):
    /// tunnel sharing exposes a loopback-bound server through a relay, so a
    /// shared session must not also be allowed to drive the server at loopback
    /// destinations.
    /// </para>
    /// <para>
    /// In this port a tunnel <em>is</em> a session — <c>POST /api/tunnels</c>
    /// mints <c>tunnel-&lt;id&gt;</c>, upserts a session definition under that
    /// same id, and stores one <see cref="TokenRecord"/> against it holding the
    /// share and control token hashes — so a token record for a session id is
    /// exactly "a share exists for this session", and this store is the only
    /// place the port tracks that fact.
    /// </para>
    /// <para>
    /// Expiry is read from the record, never assumed: <c>ExpiresAt</c> comes from
    /// the configured TTL at create, is refreshed on rotate, and the record is
    /// removed outright on revoke. A record outlives its expiry until something
    /// sweeps it, so "the key is present" is not the question being asked, and an
    /// expired share must not keep the webhook guard closed.
    /// </para>
    /// <para>
    /// The boundary is <c>now &lt; ExpiresAt</c>: at the exact instant of expiry
    /// the share is <em>not</em> live. That is deliberately the opposite of
    /// <see cref="ConsumeInviteValue"/>'s local convention (expired when
    /// <c>now &gt; ExpiresAt</c>), and it is not a free choice —
    /// <c>conformance/EGRESS_GUARD.md</c> §4 fixes the instant across all four
    /// ports so a share's last moment cannot be live in one language and dead in
    /// another (the reference writes <c>now &lt; expires_at</c>, Go writes
    /// <c>rec.ExpiresAt &gt; now</c>). Following the invite convention here, as
    /// this port originally did, made the share live one instant longer than
    /// everywhere else. The instant is unobservable against a real float clock;
    /// the agreement is the point.
    /// </para>
    /// </remarks>
    public bool HasLiveShare(string sessionId, double now)
    {
        lock (_lock)
        {
            return _tokens.TryGetValue(sessionId, out var record) && now < record.ExpiresAt;
        }
    }

    public void DeleteToken(string tunnelId)
    {
        lock (_lock)
        {
            _tokens.Remove(tunnelId);
        }
    }

    public IReadOnlyDictionary<string, TokenRecord> ListTokens()
    {
        lock (_lock)
        {
            return new Dictionary<string, TokenRecord>(_tokens);
        }
    }

    public void PutInvite(string inviteHash, Invite invite)
    {
        lock (_lock)
        {
            _invites[inviteHash] = invite;
        }
    }

    /// <summary>Pop invite by storage key (tests / low-level). Single-use.</summary>
    public Invite? ConsumeInvite(string inviteHash)
    {
        lock (_lock)
        {
            return _invites.Remove(inviteHash, out var inv) ? inv : null;
        }
    }

    /// <summary>
    /// Mint single-use viewer + operator invites. Returns plain invite values
    /// (stored under HashToken). Port of Go MemStore.IssueInvites.
    /// </summary>
    public (string ShareInvite, string ControlInvite) IssueInvites(
        string sessionId,
        string shareToken,
        string controlToken,
        double tunnelExpiresAt,
        double now,
        string? issuedIp)
    {
        var inviteExpiresAt = now + TunnelConstants.InviteTtlS;
        if (tunnelExpiresAt < inviteExpiresAt)
        {
            inviteExpiresAt = tunnelExpiresAt;
        }

        var shareInvite = TunnelTokens.GenerateToken();
        var controlInvite = TunnelTokens.GenerateToken();
        lock (_lock)
        {
            _invites[TunnelTokens.HashToken(shareInvite)] = new Invite
            {
                SessionId = sessionId,
                Role = TunnelRole.Viewer,
                TunnelToken = shareToken,
                ExpiresAt = inviteExpiresAt,
                IssuedIp = issuedIp,
            };
            _invites[TunnelTokens.HashToken(controlInvite)] = new Invite
            {
                SessionId = sessionId,
                Role = TunnelRole.Operator,
                TunnelToken = controlToken,
                ExpiresAt = inviteExpiresAt,
                IssuedIp = issuedIp,
            };
        }

        return (shareInvite, controlInvite);
    }

    /// <summary>
    /// Consume a one-time invite plain value for <paramref name="sessionId"/>.
    /// Port of Go MemStore.ConsumeInvite (burns invite even on validation fail).
    /// </summary>
    public Invite? ConsumeInviteValue(string invite, string sessionId, double now)
    {
        var inviteValue = (invite ?? "").Trim();
        if (inviteValue.Length == 0)
        {
            return null;
        }

        var hash = TunnelTokens.HashToken(inviteValue);
        Invite? rec;
        lock (_lock)
        {
            if (!_invites.Remove(hash, out rec))
            {
                return null;
            }
        }

        if (now > rec.ExpiresAt)
        {
            return null;
        }

        if (!string.Equals(rec.SessionId, sessionId, StringComparison.Ordinal))
        {
            return null;
        }

        if (rec.Role is not (TunnelRole.Viewer or TunnelRole.Operator))
        {
            return null;
        }

        var token = (rec.TunnelToken ?? "").Trim();
        if (token.Length == 0)
        {
            return null;
        }

        return new Invite
        {
            SessionId = sessionId,
            Role = rec.Role,
            TunnelToken = token,
            ExpiresAt = rec.ExpiresAt,
            IssuedIp = rec.IssuedIp,
        };
    }

    public void DiscardInvitesForSession(string sessionId)
    {
        lock (_lock)
        {
            var keys = _invites.Where(kv => kv.Value.SessionId == sessionId).Select(kv => kv.Key).ToList();
            foreach (var k in keys)
            {
                _invites.Remove(k);
            }
        }
    }
}
