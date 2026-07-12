//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;

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

/// <summary>Token hashing / verification (BLAKE2b-256 hex digests when available).</summary>
public static class TunnelTokens
{
    public static string HashToken(string token)
    {
        // Use SHA-256 hex as a portable stand-in; Go uses BLAKE2b for at-rest digests.
        // The verify helper is consistent within this port.
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(token));
        return Convert.ToHexString(digest).ToLowerInvariant();
    }

    public static bool VerifyToken(string token, string tokenHash)
    {
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
            if (!_invites.Remove(inviteHash, out var inv))
            {
                return null;
            }

            return inv;
        }
    }
}
