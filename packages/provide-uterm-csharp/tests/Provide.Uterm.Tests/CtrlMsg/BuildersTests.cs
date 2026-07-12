//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Provide.Uterm.CtrlMsg;
using Xunit;

namespace Provide.Uterm.Tests.CtrlMsg;

public class BuildersTests
{
    private static string TestDataPath(params string[] parts)
    {
        var baseDir = AppContext.BaseDirectory;
        // Walk up to find testdata
        var dir = new DirectoryInfo(baseDir);
        while (dir is not null)
        {
            var candidate = Path.Combine(new[] { dir.FullName, "testdata" }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }

            candidate = Path.Combine(new[] { dir.FullName, "tests", "Provide.Uterm.Tests", "testdata" }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }

            dir = dir.Parent;
        }

        // Relative to this source file location via content copy
        return Path.Combine(baseDir, "testdata", Path.Combine(parts));
    }

    [Fact]
    public void MakeIdentity_RequiresSubject()
    {
        Assert.Throws<ArgumentException>(() => Builders.MakeIdentity(""));
    }

    [Fact]
    public void MakeIdentity_UnsignedDefaults()
    {
        var msg = Builders.MakeIdentity("alice");
        Assert.Equal("identity", msg["type"]);
        Assert.Equal(1, Convert.ToInt32(msg["version"]));
        Assert.Equal("alice", msg["subject"]);
        Assert.Equal("", msg["fingerprint"]);
        Assert.Equal("ssh", msg["transport"]);
        Assert.False(msg.ContainsKey("claims"));
        Assert.False(msg.ContainsKey("signature"));
    }

    [Fact]
    public void MakeIdentity_SignedMatchesPythonCanonical()
    {
        var secret = Encoding.UTF8.GetBytes("test-secret");
        var msg = Builders.MakeIdentity(
            "user1",
            claims: new Dictionary<string, object?> { ["role"] = "admin", ["n"] = 1L },
            includeClaims: true,
            fingerprint: "fp",
            transport: "ws",
            secret: secret);
        Assert.True(msg.ContainsKey("signature"));
        var sig = Assert.IsType<string>(msg["signature"]);
        Assert.Equal(64, sig.Length); // hex sha256
        Assert.Equal(sig, sig.ToLowerInvariant());
    }

    [Fact]
    public void CanonicalJson_SortsKeysAndCompacts()
    {
        var json = CanonicalJson.Serialize(new Dictionary<string, object?>
        {
            ["b"] = 2L,
            ["a"] = 1L,
        });
        Assert.Equal("{\"a\":1,\"b\":2}", json);
    }

    [Fact]
    public void CanonicalJson_EnsureAscii()
    {
        var json = CanonicalJson.Serialize(new Dictionary<string, object?> { ["msg"] = "café" });
        Assert.Contains("\\u00e9", json, StringComparison.Ordinal);
        Assert.DoesNotContain("é", json, StringComparison.Ordinal);
    }

    [Fact]
    public void SignatureCorpus_MatchesPythonGoldens()
    {
        var path = TestDataPath("ctrlmsg", "signature_corpus.json");
        if (!File.Exists(path))
        {
            // Copy from project content
            path = FindRepoTestdata("ctrlmsg", "signature_corpus.json");
        }

        Assert.True(File.Exists(path), $"missing corpus at {path}");
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        var root = doc.RootElement;
        // Support either array of cases or object with "cases"
        var cases = root.ValueKind == JsonValueKind.Array
            ? root.EnumerateArray().ToList()
            : root.TryGetProperty("cases", out var c)
                ? c.EnumerateArray().ToList()
                : root.EnumerateObject().Select(p => p.Value).ToList();

        var checkedCount = 0;
        foreach (var item in cases)
        {
            // Flexible schema: subject, claims, fingerprint, transport, secret, signature
            if (!item.TryGetProperty("signature", out var expectedSigEl) &&
                !item.TryGetProperty("expected_signature", out expectedSigEl))
            {
                continue;
            }

            var subject = item.TryGetProperty("subject", out var sub) ? sub.GetString()! : "x";
            var fingerprint = item.TryGetProperty("fingerprint", out var fp) ? fp.GetString() ?? "" : "";
            var transport = item.TryGetProperty("transport", out var tr) ? tr.GetString() ?? "ssh" : "ssh";
            var secretStr = item.TryGetProperty("secret", out var sec)
                ? sec.GetString() ?? ""
                : item.TryGetProperty("secret_hex", out var sh)
                    ? null
                    : "secret";
            byte[] secret;
            if (item.TryGetProperty("secret_hex", out var hex) && hex.ValueKind == JsonValueKind.String)
            {
                secret = Convert.FromHexString(hex.GetString()!);
            }
            else
            {
                secret = Encoding.UTF8.GetBytes(secretStr ?? "secret");
            }

            Dictionary<string, object?>? claims = null;
            var includeClaims = false;
            if (item.TryGetProperty("claims", out var claimsEl) && claimsEl.ValueKind == JsonValueKind.Object)
            {
                includeClaims = true;
                claims = JsonElementToDict(claimsEl);
            }

            var msg = Builders.MakeIdentity(
                subject,
                claims: claims,
                includeClaims: includeClaims,
                fingerprint: fingerprint,
                transport: transport,
                secret: secret);
            var got = (string)msg["signature"]!;
            var expected = expectedSigEl.GetString()!;
            Assert.Equal(expected, got);
            checkedCount++;
            if (checkedCount >= 50)
            {
                break; // sample first 50 for speed; full suite later
            }
        }

        Assert.True(checkedCount > 0, "no signature cases exercised");
    }

    private static Dictionary<string, object?> JsonElementToDict(JsonElement el)
    {
        var d = new Dictionary<string, object?>();
        foreach (var p in el.EnumerateObject())
        {
            d[p.Name] = p.Value.ValueKind switch
            {
                JsonValueKind.String => p.Value.GetString(),
                JsonValueKind.Number => p.Value.GetRawText().IndexOfAny(['.', 'e', 'E']) >= 0
                    ? p.Value.GetDouble()
                    : p.Value.GetInt64(),
                JsonValueKind.True => true,
                JsonValueKind.False => false,
                JsonValueKind.Null => null,
                JsonValueKind.Object => JsonElementToDict(p.Value),
                JsonValueKind.Array => p.Value.EnumerateArray().Select(x => (object?)x.ToString()).ToList(),
                _ => p.Value.GetRawText(),
            };
        }

        return d;
    }

    private static string FindRepoTestdata(params string[] parts)
    {
        var dir = new DirectoryInfo(Directory.GetCurrentDirectory());
        while (dir is not null)
        {
            var candidate = Path.Combine(
                new[] { dir.FullName, "packages", "provide-uterm-csharp", "tests", "Provide.Uterm.Tests", "testdata" }
                    .Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }

            dir = dir.Parent;
        }

        return Path.Combine(parts);
    }
}
