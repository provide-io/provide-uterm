//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Provide.Uterm.DeckMux;

/// <summary>Opaque identity from a control-channel identity frame.</summary>
public sealed class ResolvedIdentity
{
    public required string Subject { get; init; }
    public Dictionary<string, object?> Claims { get; init; } = new();
    public string Fingerprint { get; init; } = "";
}

/// <summary>Collaborative presence record for a connected user.</summary>
public sealed class UserPresence
{
    public required string UserId { get; init; }
    public required string Name { get; init; }
    public required string Color { get; init; }
    public required string Role { get; init; }
    public required string Initials { get; init; }
}

/// <summary>Principal adapter for DeckMux presence services.</summary>
public sealed class IdentityPrincipal
{
    public string SubjectId { get; }
    public string DisplayName { get; }
    public ResolvedIdentity Identity { get; }

    public IdentityPrincipal(string subjectId, string displayName, ResolvedIdentity identity)
    {
        SubjectId = subjectId;
        DisplayName = displayName;
        Identity = identity;
    }
}

/// <summary>
/// Identity-frame parsing and presence derivation.
/// Port of packages/provide-uterm-go/deckmux/identity.go.
/// </summary>
public static class Identity
{
    private static readonly HashSet<int> SupportedIdentityVersions = [1];

    public static ResolvedIdentity? ParseIdentityFrame(
        IReadOnlyDictionary<string, object?> frame,
        byte[]? expectedSecret = null)
    {
        if (!frame.TryGetValue("type", out var typeObj) || typeObj as string != "identity")
        {
            return null;
        }

        if (!TryIdentityVersion(frame.GetValueOrDefault("version"), out var version))
        {
            return null;
        }

        if (!SupportedIdentityVersions.Contains(version))
        {
            return null;
        }

        if (frame.GetValueOrDefault("subject") is not string subject || subject.Length == 0)
        {
            return null;
        }

        var claims = new Dictionary<string, object?>();
        if (frame.GetValueOrDefault("claims") is IDictionary<string, object?> rawClaims)
        {
            foreach (var (k, v) in rawClaims)
            {
                claims[k] = v;
            }
        }
        else if (frame.GetValueOrDefault("claims") is Dictionary<string, object?> d)
        {
            foreach (var (k, v) in d)
            {
                claims[k] = v;
            }
        }

        var fingerprint = frame.GetValueOrDefault("fingerprint") as string ?? "";

        if (expectedSecret is { Length: > 0 })
        {
            if (frame.GetValueOrDefault("signature") is not string signature || signature.Length == 0)
            {
                return null;
            }

            var transport = frame.GetValueOrDefault("transport") as string ?? "";
            var claimsStr = PythonCompactJson(claims);
            var canonical = version.ToString(CultureInfo.InvariantCulture) + ":" + subject + ":" +
                            fingerprint + ":" + transport + ":" + claimsStr;
            using var hmac = new HMACSHA256(expectedSecret);
            var expected = Convert.ToHexString(hmac.ComputeHash(Encoding.UTF8.GetBytes(canonical)))
                .ToLowerInvariant();
            if (!CryptographicOperations.FixedTimeEquals(
                    Encoding.UTF8.GetBytes(signature),
                    Encoding.UTF8.GetBytes(expected)))
            {
                return null;
            }
        }

        return new ResolvedIdentity { Subject = subject, Claims = claims, Fingerprint = fingerprint };
    }

    public static UserPresence PresenceFromIdentity(
        ResolvedIdentity identity,
        string connectionId,
        ISet<string>? takenColors = null,
        string role = "viewer")
    {
        takenColors ??= new HashSet<string>();
        var claims = identity.Claims;
        var name = FirstNonempty(
            StrOrNone(claims.GetValueOrDefault("display_name")),
            StrOrNone(claims.GetValueOrDefault("display")),
            NameFromSubject(identity.Subject));
        if (name.Length == 0)
        {
            name = IdentityNames.GenerateName(connectionId);
        }

        var color = StrOrNone(claims.GetValueOrDefault("color"));
        if (color.Length == 0)
        {
            color = IdentityNames.GenerateColor(connectionId, takenColors);
        }

        var resolvedRole = StrOrNone(claims.GetValueOrDefault("role"));
        if (resolvedRole.Length == 0)
        {
            resolvedRole = role;
        }

        return new UserPresence
        {
            UserId = identity.Subject,
            Name = name,
            Color = color,
            Role = resolvedRole,
            Initials = IdentityNames.GenerateInitials(name),
        };
    }

    public static IdentityPrincipal IdentityAsPrincipal(ResolvedIdentity identity)
    {
        var claims = identity.Claims;
        var display = FirstNonempty(
            StrOrNone(claims.GetValueOrDefault("display_name")),
            StrOrNone(claims.GetValueOrDefault("display")),
            NameFromSubject(identity.Subject));
        if (display.Length == 0)
        {
            display = identity.Subject;
        }

        return new IdentityPrincipal(identity.Subject, display, identity);
    }

    private static bool TryIdentityVersion(object? v, out int version)
    {
        version = 0;
        switch (v)
        {
            case int i:
                version = i;
                return true;
            case long l:
                version = (int)l;
                return true;
            case double d when d == Math.Truncate(d):
                version = (int)d;
                return true;
            case JsonElement je when je.ValueKind == JsonValueKind.Number && je.TryGetInt32(out var n):
                version = n;
                return true;
            default:
                return false;
        }
    }

    private static string FirstNonempty(params string[] values)
    {
        foreach (var v in values)
        {
            if (v.Length > 0)
            {
                return v;
            }
        }

        return "";
    }

    private static string StrOrNone(object? value) =>
        value is string s ? s.Trim() : "";

    private static string NameFromSubject(string subject)
    {
        var idx = subject.IndexOf(':');
        if (idx < 0)
        {
            return subject.Trim();
        }

        return subject[(idx + 1)..].Trim();
    }

    internal static string PythonCompactJson(object? v)
    {
        var sb = new StringBuilder();
        EncodePyJson(sb, v);
        return sb.ToString();
    }

    private static void EncodePyJson(StringBuilder b, object? v)
    {
        switch (v)
        {
            case null:
                b.Append("null");
                break;
            case bool bl:
                b.Append(bl ? "true" : "false");
                break;
            case string s:
                EncodePyString(b, s);
                break;
            case int i:
                b.Append(i.ToString(CultureInfo.InvariantCulture));
                break;
            case long l:
                b.Append(l.ToString(CultureInfo.InvariantCulture));
                break;
            case double d when d == Math.Truncate(d):
                b.Append(((long)d).ToString(CultureInfo.InvariantCulture));
                break;
            case double d:
                b.Append(d.ToString("G", CultureInfo.InvariantCulture));
                break;
            case IList<object?> list:
                b.Append('[');
                for (var i = 0; i < list.Count; i++)
                {
                    if (i > 0)
                    {
                        b.Append(',');
                    }

                    EncodePyJson(b, list[i]);
                }

                b.Append(']');
                break;
            case IDictionary<string, object?> map:
            {
                var keys = map.Keys.OrderBy(k => k, StringComparer.Ordinal).ToList();
                b.Append('{');
                for (var i = 0; i < keys.Count; i++)
                {
                    if (i > 0)
                    {
                        b.Append(',');
                    }

                    EncodePyString(b, keys[i]);
                    b.Append(':');
                    EncodePyJson(b, map[keys[i]]);
                }

                b.Append('}');
                break;
            }
            default:
                b.Append("null");
                break;
        }
    }

    private static void EncodePyString(StringBuilder b, string s)
    {
        b.Append('"');
        foreach (var r in s)
        {
            switch (r)
            {
                case '"':
                    b.Append("\\\"");
                    break;
                case '\\':
                    b.Append("\\\\");
                    break;
                case '\n':
                    b.Append("\\n");
                    break;
                case '\r':
                    b.Append("\\r");
                    break;
                case '\t':
                    b.Append("\\t");
                    break;
                case '\b':
                    b.Append("\\b");
                    break;
                case '\f':
                    b.Append("\\f");
                    break;
                default:
                    if (r < 0x20)
                    {
                        WriteUnicodeEscape(b, r);
                    }
                    else if (r < 0x7f)
                    {
                        b.Append(r);
                    }
                    else if (r > 0xffff)
                    {
                        var v = r - 0x10000;
                        WriteUnicodeEscape(b, (char)(0xd800 + (v >> 10)));
                        WriteUnicodeEscape(b, (char)(0xdc00 + (v & 0x3ff)));
                    }
                    else
                    {
                        WriteUnicodeEscape(b, r);
                    }

                    break;
            }
        }

        b.Append('"');
    }

    private static void WriteUnicodeEscape(StringBuilder b, int r)
    {
        const string hex = "0123456789abcdef";
        b.Append("\\u");
        b.Append(hex[(r >> 12) & 0xf]);
        b.Append(hex[(r >> 8) & 0xf]);
        b.Append(hex[(r >> 4) & 0xf]);
        b.Append(hex[r & 0xf]);
    }
}
