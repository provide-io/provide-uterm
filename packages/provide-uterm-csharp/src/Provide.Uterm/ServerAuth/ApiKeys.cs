//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text.RegularExpressions;
using System.Text;

namespace Provide.Uterm.ServerAuth;

public sealed class ApiKeyRecord
{
    public required string KeyId { get; set; }
    public required string KeyHash { get; set; }
    public required string TenantId { get; set; }
    public required string Name { get; set; }
    public StringSet Scopes { get; set; } = new();
    public double CreatedAt { get; set; }
    public double? ExpiresAt { get; set; }
    public double? LastUsedAt { get; set; }
    public bool Revoked { get; set; }
}

/// <summary>In-memory API key registry with timing-safe validation.</summary>
public sealed class ApiKeyStore
{
    private static readonly Regex TenantPattern = new(@"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", RegexOptions.Compiled);
    private readonly object _gate = new();
    private readonly Dictionary<string, ApiKeyRecord> _keys = new();
    private Func<double> _now = () => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    public void SetClock(Func<double> now) => _now = now;

    public static string HashKey(string rawKey)
    {
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(rawKey));
        return Convert.ToHexString(digest).ToLowerInvariant();
    }

    public (string RawKey, ApiKeyRecord Record) Create(
        string name,
        StringSet? scopes = null,
        int? expiresInS = null,
        string tenantId = "")
    {
        var tenant = CanonicalTenantId(tenantId);
        if (tenant is null)
        {
            throw new ArgumentException("tenant_id is required and must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$");
        }

        var raw = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32))
            .TrimEnd('=').Replace('+', '-').Replace('/', '_');
        var keyHash = HashKey(raw);
        double? expiresAt = expiresInS is null ? null : _now() + expiresInS.Value;
        var record = new ApiKeyRecord
        {
            KeyId = keyHash[..16],
            KeyHash = keyHash,
            Name = name,
            TenantId = tenant,
            Scopes = scopes ?? new StringSet(),
            CreatedAt = _now(),
            ExpiresAt = expiresAt,
        };
        lock (_gate) _keys[record.KeyId] = record;
        return (raw, record);
    }

    public (string RawKey, ApiKeyRecord Record) CreateForTenant(
        string tenantId,
        string name,
        StringSet? scopes = null,
        int? expiresInS = null
    ) => Create(name, scopes, expiresInS, tenantId);

    public ApiKeyRecord? Validate(string rawKey)
    {
        var keyHash = HashKey(rawKey);
        lock (_gate)
        {
            foreach (var record in _keys.Values)
            {
                if (record.Revoked) continue;
                if (!CryptographicOperations.FixedTimeEquals(
                        Encoding.UTF8.GetBytes(record.KeyHash),
                        Encoding.UTF8.GetBytes(keyHash)))
                {
                    continue;
                }

                if (record.ExpiresAt is { } exp && exp <= _now()) return null;
                record.LastUsedAt = _now();
                return record;
            }
        }

        return null;
    }

    public IReadOnlyList<ApiKeyRecord> ListKeysForTenant(string tenantId)
    {
        var tenant = CanonicalTenantId(tenantId);
        if (tenant is null) return Array.Empty<ApiKeyRecord>();
        lock (_gate)
        {
            var outList = new List<ApiKeyRecord>();
            foreach (var record in _keys.Values)
            {
                if (!record.Revoked && record.TenantId == tenant)
                {
                    outList.Add(record);
                }
            }

            return outList;
        }
    }

    public bool RevokeForTenant(string keyId, string tenantId)
    {
        var tenant = CanonicalTenantId(tenantId);
        if (tenant is null) return false;
        lock (_gate)
        {
            if (!_keys.TryGetValue(keyId, out var rec)) return false;
            if (rec.TenantId != tenant) return false;
            rec.Revoked = true;
            return true;
        }
    }

    public IReadOnlyList<ApiKeyRecord> ListKeys()
    {
        lock (_gate)
        {
            return _keys.Values.ToList();
        }
    }

    public bool Revoke(string keyId)
    {
        lock (_gate)
        {
            if (!_keys.TryGetValue(keyId, out var rec)) return false;
            rec.Revoked = true;
            return true;
        }
    }

    private static string? CanonicalTenantId(string tenantId)
    {
        tenantId = tenantId?.Trim();
        if (string.IsNullOrEmpty(tenantId))
        {
            return null;
        }

        return TenantPattern.IsMatch(tenantId) ? tenantId : null;
    }
}
