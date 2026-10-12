//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections;
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Provide.Uterm.Redaction;

/// <summary>
/// Structure-aware redaction for recorder payloads.
/// Port of provide.uterm.redaction.redact_structure / Go redaction.RedactStructure.
/// </summary>
public static class RedactionStructure
{
    /// <summary>
    /// Container levels walked. Real frames are two or three levels deep; the cap keeps a
    /// hostile frame nested thousands deep from overflowing the stack of every recorder that
    /// redacts it.
    /// </summary>
    public const int RedactMaxDepth = 32;

    /// <summary>
    /// Replaces a container at or past the depth cap. The subtree is replaced whole, never
    /// returned unwalked: an unwalked subtree is an unredacted one.
    /// </summary>
    public const string DepthLimitMarker = "[NESTED_REDACTED]";

    /// <summary>Replaces the value of a sensitive key under key-aware redaction.</summary>
    public const string SensitiveKeyMarker = "[SECRET_REDACTED]";

    /// <summary>
    /// Key-aware rule, ported from provide.uterm.redaction. A key is split into lower-case
    /// segments (camelCase split: accessToken -> access, token; '-', '.' and ' ' act like '_').
    /// It is sensitive when any segment is in <see cref="SensitiveKeySegments"/> or two adjacent
    /// segments form a pair in <see cref="SensitiveKeyPairs"/> -- unless its last segment is in
    /// <see cref="NonSecretKeySuffixes"/> (token_count, token_type describe a credential rather
    /// than hold one).
    /// </summary>
    public static readonly IReadOnlySet<string> SensitiveKeySegments = new HashSet<string>(StringComparer.Ordinal)
    {
        "token", "secret", "password", "passwd", "passphrase",
        "credential", "credentials", "cookie", "authorization", "apikey",
    };

    /// <summary>Adjacent segment pairs that name a credential (api_key, privateKey, access_key).</summary>
    public static readonly IReadOnlyList<(string First, string Second)> SensitiveKeyPairs =
        [("api", "key"), ("private", "key"), ("access", "key")];

    /// <summary>A key whose last segment is one of these describes a credential and stays visible.</summary>
    public static readonly IReadOnlySet<string> NonSecretKeySuffixes = new HashSet<string>(StringComparer.Ordinal)
    {
        "count", "len", "length", "size", "type", "kind",
        "ttl", "expiry", "expires", "hash", "required", "enabled",
    };

    private static bool IsAsciiUpper(char c) => c is >= 'A' and <= 'Z';

    private static bool IsAsciiLowerOrDigit(char c) => c is (>= 'a' and <= 'z') or (>= '0' and <= '9');

    /// <summary>Split <paramref name="key"/> into lower-case segments, exactly as the reference key_segments does.</summary>
    public static IReadOnlyList<string> KeySegments(string key)
    {
        var sb = new System.Text.StringBuilder(key.Length + 4);
        var last = key.Length - 1;
        for (var i = 0; i < key.Length; i++)
        {
            var c = key[i];
            if (i > 0 && IsAsciiUpper(c))
            {
                var prev = key[i - 1];
                var endsAcronym = IsAsciiUpper(prev) && i < last && key[i + 1] is >= 'a' and <= 'z';
                if (IsAsciiLowerOrDigit(prev) || endsAcronym)
                {
                    sb.Append('_');
                }
            }

            sb.Append(c);
        }

        var folded = sb.ToString().ToLowerInvariant().Replace('-', '_').Replace('.', '_').Replace(' ', '_');
        return folded.Split('_', StringSplitOptions.RemoveEmptyEntries);
    }

    /// <summary>Whether <paramref name="key"/> names a credential field (see <see cref="SensitiveKeySegments"/>).</summary>
    public static bool IsSensitiveKey(string key)
    {
        var segments = KeySegments(key);
        if (segments.Count == 0 || NonSecretKeySuffixes.Contains(segments[^1]))
        {
            return false;
        }

        for (var i = 0; i < segments.Count; i++)
        {
            if (SensitiveKeySegments.Contains(segments[i]))
            {
                return true;
            }

            if (i > 0 && SensitiveKeyPairs.Contains((segments[i - 1], segments[i])))
            {
                return true;
            }
        }

        return false;
    }

    /// <summary>
    /// Return a redacted copy of a JSON-like value: strings go through <paramref name="redactor"/>
    /// at any depth; dictionaries (non-generic <c>IDictionary</c>, generic
    /// <c>IDictionary&lt;,&gt;</c> / <c>IReadOnlyDictionary&lt;,&gt;</c>) and sequences are
    /// rebuilt as <c>Dictionary&lt;string, object?&gt;</c> / <c>List&lt;object?&gt;</c> (keys
    /// kept, input never mutated; <c>byte[]</c> is a scalar, as System.Text.Json treats it);
    /// <see cref="JsonNode"/> trees come back as JsonNode trees and a <see cref="JsonElement"/>
    /// as plain containers whose non-string scalars stay JsonElements, so either serialises to
    /// the same shape and types; a container <paramref name="maxDepth"/> levels below the root
    /// becomes <see cref="DepthLimitMarker"/>; with <paramref name="keyAware"/>, a non-null value
    /// under a sensitive key becomes <see cref="SensitiveKeyMarker"/>; every other value is
    /// returned unchanged.
    /// </summary>
    public static object? Redact(object? value, Redactor redactor, bool keyAware = false, int maxDepth = RedactMaxDepth) =>
        new Walker(redactor, keyAware, maxDepth).Walk(value, 0);

    private sealed class Walker(Redactor redactor, bool keyAware, int maxDepth)
    {
        public object? Walk(object? value, int depth)
        {
            switch (value)
            {
                case string s:
                    return redactor(s);
                case null:
                case byte[]:
                    return value;
                case JsonNode node:
                    return WalkNode(node, depth);
                case JsonElement element:
                    return WalkElement(element, depth);
                case IDictionary dict:
                    return WalkPairs(EnumerateDictionary(dict), depth);
            }

            if (TryGenericPairs(value) is { } pairs)
            {
                return WalkPairs(pairs, depth);
            }

            if (value is IEnumerable sequence)
            {
                if (depth >= maxDepth)
                {
                    return DepthLimitMarker;
                }

                var list = new List<object?>();
                foreach (var item in sequence)
                {
                    list.Add(Walk(item, depth + 1));
                }

                return list;
            }

            return value;
        }

        private object WalkPairs(IEnumerable<(object Key, object? Value)> pairs, int depth)
        {
            if (depth >= maxDepth)
            {
                return DepthLimitMarker;
            }

            var map = new Dictionary<string, object?>(StringComparer.Ordinal);
            foreach (var (rawKey, item) in pairs)
            {
                var key = Convert.ToString(rawKey, CultureInfo.InvariantCulture) ?? "";
                map[key] = Masks(key, IsNullish(item)) ? SensitiveKeyMarker : Walk(item, depth + 1);
            }

            return map;
        }

        private bool Masks(string key, bool isNull) => keyAware && !isNull && IsSensitiveKey(key);

        private JsonNode? WalkNode(JsonNode? node, int depth)
        {
            switch (node)
            {
                case null:
                    return null;
                case JsonObject obj:
                    if (depth >= maxDepth)
                    {
                        return JsonValue.Create(DepthLimitMarker);
                    }

                    var outObj = new JsonObject();
                    foreach (var (key, child) in obj)
                    {
                        outObj[key] = Masks(key, child is null || child.GetValueKind() == JsonValueKind.Null)
                            ? JsonValue.Create(SensitiveKeyMarker)
                            : WalkNode(child, depth + 1);
                    }

                    return outObj;
                case JsonArray array:
                    if (depth >= maxDepth)
                    {
                        return JsonValue.Create(DepthLimitMarker);
                    }

                    var outArray = new JsonArray();
                    foreach (var child in array)
                    {
                        outArray.Add(WalkNode(child, depth + 1));
                    }

                    return outArray;
                default:
                    return node.GetValueKind() == JsonValueKind.String
                        ? JsonValue.Create(redactor(node.GetValue<string>()))
                        : node.DeepClone();
            }
        }

        private object? WalkElement(JsonElement element, int depth)
        {
            switch (element.ValueKind)
            {
                case JsonValueKind.String:
                    return redactor(element.GetString()!);
                case JsonValueKind.Object:
                    return WalkPairs(element.EnumerateObject().Select(p => ((object)p.Name, (object?)p.Value)), depth);
                case JsonValueKind.Array:
                    if (depth >= maxDepth)
                    {
                        return DepthLimitMarker;
                    }

                    return element.EnumerateArray().Select(item => WalkElement(item, depth + 1)).ToList();
                default:
                    return element.Clone();
            }
        }
    }

    private static bool IsNullish(object? value) =>
        value is null || (value is JsonElement e && e.ValueKind == JsonValueKind.Null);

    private static IEnumerable<(object Key, object? Value)> EnumerateDictionary(IDictionary dict)
    {
        foreach (DictionaryEntry entry in dict)
        {
            yield return (entry.Key, entry.Value);
        }
    }

    /// <summary>Key/value pairs of a generic dictionary that is not a non-generic IDictionary.</summary>
    private static IEnumerable<(object Key, object? Value)>? TryGenericPairs(object value)
    {
        var isDictionary = value.GetType().GetInterfaces().Any(i =>
            i.IsGenericType &&
            (i.GetGenericTypeDefinition() == typeof(IReadOnlyDictionary<,>) ||
             i.GetGenericTypeDefinition() == typeof(IDictionary<,>)));
        return isDictionary ? EnumerateGenericPairs((IEnumerable)value) : null;
    }

    private static IEnumerable<(object Key, object? Value)> EnumerateGenericPairs(IEnumerable pairs)
    {
        foreach (var pair in pairs)
        {
            var type = pair!.GetType();
            yield return (type.GetProperty("Key")!.GetValue(pair)!, type.GetProperty("Value")!.GetValue(pair));
        }
    }
}
