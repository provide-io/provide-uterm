//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;

namespace Provide.Uterm.Tests.ControlChannel;

/// <summary>
/// Loader + structural helpers for the cross-language differential fuzz corpus
/// at <c>conformance/fuzz/control_channel_fuzz.json</c>.
///
/// The corpus lives at the repository root, outside this package, and is
/// deliberately NOT copied into the test output directory: a copy-to-output item
/// is not re-run under <c>dotnet test --no-build</c> (which the package Makefile
/// uses), so a regenerated corpus could silently be replayed from a stale copy.
/// Instead the file is located by walking up from <see cref="AppContext.BaseDirectory"/>
/// until a directory containing <c>conformance/fuzz/control_channel_fuzz.json</c>
/// is found, so the live file is always read regardless of the working directory
/// <c>dotnet test</c> was invoked from.
/// </summary>
internal static class FuzzCorpus
{
    /// <summary>The only schema version this replay understands.</summary>
    public const string Schema = "provide-uterm/control-channel-fuzz/1";

    private const string RelativePath = "conformance/fuzz/control_channel_fuzz.json";

    private static readonly Lazy<(JsonDocument Doc, string Path)> Loaded = new(Load, isThreadSafe: true);

    public static JsonElement Root => Loaded.Value.Doc.RootElement;

    public static string SourcePath => Loaded.Value.Path;

    /// <summary>Locate the corpus by ascending from the test binary's directory.</summary>
    public static string Locate()
    {
        var overridePath = Environment.GetEnvironmentVariable("UTERM_FUZZ_CORPUS");
        if (!string.IsNullOrEmpty(overridePath))
        {
            if (!File.Exists(overridePath))
            {
                throw new FileNotFoundException($"UTERM_FUZZ_CORPUS points at a missing file: {overridePath}");
            }

            return Path.GetFullPath(overridePath);
        }

        var parts = RelativePath.Split('/');
        var probed = new List<string>();
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(new[] { dir.FullName }.Concat(parts).ToArray());
            probed.Add(candidate);
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        throw new FileNotFoundException(
            "differential fuzz corpus not found; walked up from " + AppContext.BaseDirectory +
            " looking for " + RelativePath + ". Probed:\n  " + string.Join("\n  ", probed));
    }

    private static (JsonDocument, string) Load()
    {
        var path = Locate();
        var doc = JsonDocument.Parse(File.ReadAllBytes(path));
        var schema = doc.RootElement.GetProperty("schema").GetString();
        if (schema != Schema)
        {
            doc.Dispose();
            throw new InvalidOperationException(
                $"refusing to replay unknown fuzz corpus schema '{schema}' (this port understands '{Schema}') " +
                $"from {path}");
        }

        return (doc, path);
    }

    /// <summary>The <c>counts</c> entry the corpus declares for a family.</summary>
    public static int DeclaredCount(string family) => Root.GetProperty("counts").GetProperty(family).GetInt32();

    /// <summary>
    /// The cases of a family, after cross-checking the array length against the
    /// corpus's own <c>counts</c> block and the count this port expects. A
    /// truncated corpus therefore fails loudly instead of asserting nothing.
    /// </summary>
    public static JsonElement[] Family(string family, int expected)
    {
        var cases = Root.GetProperty(family).EnumerateArray().ToArray();
        Assert.Equal(expected, DeclaredCount(family));
        Assert.Equal(expected, cases.Length);
        return cases;
    }

    public static string CaseId(JsonElement c) => c.GetProperty("id").GetString() ?? "<no id>";

    public static byte[] B64(JsonElement el) => Convert.FromBase64String(el.GetString() ?? "");

    public static byte[] B64(JsonElement c, string field) => B64(c.GetProperty(field));

    /// <summary>Base64 of UTF-8 bytes -> .NET string (corpus encoding rule 1).</summary>
    public static string B64Str(JsonElement el) => Encoding.UTF8.GetString(B64(el));

    public static string B64Str(JsonElement c, string field) => B64Str(c.GetProperty(field));

    /// <summary>Readable rendering of arbitrary bytes for a failure message.</summary>
    public static string Show(byte[] bytes)
    {
        var sb = new StringBuilder();
        foreach (var b in bytes)
        {
            if (b is >= 0x20 and < 0x7F && b != (byte)'\\')
            {
                sb.Append((char)b);
            }
            else
            {
                sb.Append("\\x").Append(b.ToString("x2", System.Globalization.CultureInfo.InvariantCulture));
            }
        }

        return sb.ToString();
    }

    /// <summary>
    /// Structural (deep) comparison of a recorded JSON value against the CLR
    /// value the C# decoder produced. Rule 3 of the corpus (no floats in the
    /// asserted families) is what makes this safe without a tolerance.
    /// </summary>
    public static bool Matches(JsonElement expected, object? actual)
    {
        switch (expected.ValueKind)
        {
            case JsonValueKind.Object:
                if (actual is not IReadOnlyDictionary<string, object?> map)
                {
                    return false;
                }

                var props = expected.EnumerateObject().ToArray();
                if (props.Length != map.Count)
                {
                    return false;
                }

                foreach (var p in props)
                {
                    if (!map.TryGetValue(p.Name, out var child) || !Matches(p.Value, child))
                    {
                        return false;
                    }
                }

                return true;

            case JsonValueKind.Array:
                if (actual is not System.Collections.IList list)
                {
                    return false;
                }

                var items = expected.EnumerateArray().ToArray();
                if (items.Length != list.Count)
                {
                    return false;
                }

                for (var i = 0; i < items.Length; i++)
                {
                    if (!Matches(items[i], list[i]))
                    {
                        return false;
                    }
                }

                return true;

            case JsonValueKind.String:
                return actual is string s && string.Equals(s, expected.GetString(), StringComparison.Ordinal);

            case JsonValueKind.Number:
                return MatchesNumber(expected, actual);

            case JsonValueKind.True:
                return actual is true;

            case JsonValueKind.False:
                return actual is false;

            case JsonValueKind.Null:
                return actual is null;

            default:
                return false;
        }
    }

    private static bool MatchesNumber(JsonElement expected, object? actual)
    {
        if (!expected.TryGetInt64(out var want))
        {
            // Rule 3: the asserted families carry no floats. Anything else is a
            // corpus violation, so refuse rather than compare loosely.
            return false;
        }

        return actual switch
        {
            long l => l == want,
            int i => i == want,
            double d => d == want,
            _ => false,
        };
    }

    /// <summary>Render a decoded CLR payload for a failure message.</summary>
    public static string Render(object? value)
    {
        switch (value)
        {
            case null:
                return "null";
            case string s:
                return JsonSerializer.Serialize(s);
            case bool b:
                return b ? "true" : "false";
            case IReadOnlyDictionary<string, object?> map:
                return "{" + string.Join(",", map.Select(kv => JsonSerializer.Serialize(kv.Key) + ":" + Render(kv.Value))) + "}";
            case System.Collections.IList list:
                return "[" + string.Join(",", list.Cast<object?>().Select(Render)) + "]";
            default:
                return Convert.ToString(value, System.Globalization.CultureInfo.InvariantCulture) ?? "?";
        }
    }
}
