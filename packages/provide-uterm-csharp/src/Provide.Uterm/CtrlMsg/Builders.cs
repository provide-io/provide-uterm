//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;

namespace Provide.Uterm.CtrlMsg;

/// <summary>
/// Control-message builders mirroring provide.uterm.control_channel_builders
/// and packages/provide-uterm-go/ctrlmsg.
/// </summary>
public static class Builders
{
    private const int IdentityVersion = 1;

    private static readonly string[] ValidLinkActions = ["cmd", "focus", "key", "url"];

    private static readonly HashSet<string> LinkPatternFields =
    [
        "pattern", "action", "id", "flags", "group", "payload", "hover", "line_contains", "class",
    ];

    /// <summary>
    /// Build an "identity" control message. When <paramref name="secret"/> is
    /// non-empty, adds a lowercase hex HMAC-SHA256 signature over the canonical
    /// payload "{version}:{subject}:{fingerprint}:{transport}:{claims_json}".
    /// </summary>
    public static Dictionary<string, object?> MakeIdentity(
        string subject,
        IReadOnlyDictionary<string, object?>? claims = null,
        bool includeClaims = false,
        string fingerprint = "",
        string transport = "ssh",
        byte[]? secret = null)
    {
        if (string.IsNullOrEmpty(subject))
        {
            throw new ArgumentException("make_identity: 'subject' must be a non-empty string", nameof(subject));
        }

        var msg = new Dictionary<string, object?>
        {
            ["type"] = "identity",
            ["version"] = IdentityVersion,
            ["subject"] = subject,
            ["fingerprint"] = fingerprint,
            ["transport"] = transport,
        };

        if (includeClaims)
        {
            msg["claims"] = claims is null
                ? new Dictionary<string, object?>()
                : new Dictionary<string, object?>(claims);
        }

        if (secret is { Length: > 0 })
        {
            var claimsForSig = claims is null
                ? new Dictionary<string, object?>()
                : new Dictionary<string, object?>(claims);
            var claimsJson = CanonicalJson.Serialize(claimsForSig);
            var payload = $"{IdentityVersion}:{subject}:{fingerprint}:{transport}:{claimsJson}";
            var hash = HMACSHA256.HashData(secret, Encoding.UTF8.GetBytes(payload));
            msg["signature"] = Convert.ToHexString(hash).ToLowerInvariant();
        }

        return msg;
    }

    public static Dictionary<string, object?> MakeSessionToken(string token, int? playerId = null)
    {
        if (string.IsNullOrEmpty(token))
        {
            throw new ArgumentException("make_session_token: 'token' must be a non-empty string", nameof(token));
        }

        var msg = new Dictionary<string, object?> { ["type"] = "session_token", ["token"] = token };
        if (playerId is not null)
        {
            msg["player_id"] = playerId.Value;
        }

        return msg;
    }

    public static Dictionary<string, object?> MakeResume(string token, int? playerId = null)
    {
        if (string.IsNullOrEmpty(token))
        {
            throw new ArgumentException("make_resume: 'token' must be a non-empty string", nameof(token));
        }

        var msg = new Dictionary<string, object?> { ["type"] = "resume", ["token"] = token };
        if (playerId is not null)
        {
            msg["player_id"] = playerId.Value;
        }

        return msg;
    }

    public static Dictionary<string, object?> MakeResumeOk() =>
        new() { ["type"] = "resume_ok" };

    public static Dictionary<string, object?> MakeResumeFailed(string? reason = null, bool includeReason = false)
    {
        var msg = new Dictionary<string, object?> { ["type"] = "resume_failed" };
        if (includeReason)
        {
            msg["reason"] = reason ?? "";
        }

        return msg;
    }

    public static Dictionary<string, object?> MakeLinkPatterns(IReadOnlyList<IReadOnlyDictionary<string, object?>> patterns)
    {
        var entries = new List<object?>();
        for (var i = 0; i < patterns.Count; i++)
        {
            try
            {
                entries.Add(ValidateLinkPatternEntry(patterns[i]));
            }
            catch (ArgumentException ex)
            {
                throw new ArgumentException($"make_link_patterns: entry[{i}] is invalid: {ex.Message}", ex);
            }
        }

        return new Dictionary<string, object?> { ["type"] = "link_patterns", ["patterns"] = entries };
    }

    private static Dictionary<string, object?> ValidateLinkPatternEntry(IReadOnlyDictionary<string, object?> entry)
    {
        var keys = entry.Keys.OrderBy(k => k, StringComparer.Ordinal).ToList();
        foreach (var k in keys)
        {
            if (!LinkPatternFields.Contains(k))
            {
                throw new ArgumentException($"unknown field \"{k}\"");
            }
        }

        if (!entry.ContainsKey("pattern"))
        {
            throw new ArgumentException("field \"pattern\" is required");
        }

        if (!entry.ContainsKey("action"))
        {
            throw new ArgumentException("field \"action\" is required");
        }

        var outMap = new Dictionary<string, object?>();
        foreach (var k in keys)
        {
            var v = entry[k];
            var err = ValidateField(k, v);
            if (err is not null)
            {
                throw new ArgumentException($"field \"{k}\" {err}");
            }

            outMap[k] = v;
        }

        return outMap;
    }

    private static string? ValidateField(string key, object? v) =>
        key switch
        {
            "pattern" or "id" or "flags" or "hover" or "line_contains" or "class" =>
                v is string ? null : "must be a string",
            "action" => v is string s
                ? ValidLinkActions.Contains(s)
                    ? null
                    : $"is invalid (\"{s}\"); must be one of [{string.Join(' ', ValidLinkActions)}]"
                : "must be a string",
            "group" => v is int or long or string ? null : "must be an int or string",
            "payload" => null,
            _ => "unknown",
        };

    public static Dictionary<string, object?> MakePresenceUpdate(string userId, IReadOnlyDictionary<string, object?>? fields = null)
    {
        var msg = new Dictionary<string, object?> { ["type"] = "presence_update", ["user_id"] = userId };
        if (fields is null)
        {
            return msg;
        }

        foreach (var (k, v) in fields)
        {
            if (v is null)
            {
                continue;
            }

            msg[k] = v;
        }

        return msg;
    }
}
