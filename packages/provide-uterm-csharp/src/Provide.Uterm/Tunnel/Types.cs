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
}

/// <summary>In-memory tunnel token store.</summary>
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

    public void PutInvite(string inviteHash, Invite invite)
    {
        lock (_lock)
        {
            _invites[inviteHash] = invite;
        }
    }

    public Invite? ConsumeInvite(string inviteHash)
    {
        lock (_lock)
        {
            return _invites.Remove(inviteHash, out var inv) ? inv : null;
        }
    }
}
