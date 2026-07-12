//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Provide.Uterm.CtrlMsg;

namespace Provide.Uterm.Tests.CtrlMsg;

/// <summary>
/// Differential HMAC identity-signature corpus against Python goldens.
/// </summary>
public class SignatureCorpusTests
{
    [Fact]
    public void FullSignatureCorpus_MatchesPython()
    {
        var path = TestData.PathTo("ctrlmsg", "signature_corpus.json");
        Assert.True(File.Exists(path), path);
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        var cases = doc.RootElement.EnumerateArray().ToList();
        Assert.True(cases.Count >= 500, $"expected ~544 cases, got {cases.Count}");

        var failures = new List<string>();
        var checkedCount = 0;
        foreach (var item in cases)
        {
            var subject = item.GetProperty("subject").GetString()!;
            var fingerprint = item.GetProperty("fingerprint").GetString() ?? "";
            var transport = item.GetProperty("transport").GetString() ?? "ssh";
            var secret = Encoding.UTF8.GetBytes(item.GetProperty("secret").GetString()!);
            var expected = item.GetProperty("signature").GetString()!;
            var hasClaims = item.GetProperty("has_claims").GetBoolean();

            Dictionary<string, object?>? claims = null;
            if (hasClaims)
            {
                if (item.TryGetProperty("claims_json", out var cj) &&
                    cj.ValueKind == JsonValueKind.String &&
                    cj.GetString() is { Length: > 0 } claimsJson)
                {
                    using var claimsDoc = JsonDocument.Parse(claimsJson);
                    claims = global::Provide.Uterm.ControlChannel.ControlChannelCodec
                        .JsonElementToDictionary(claimsDoc.RootElement);
                }
                else
                {
                    claims = new Dictionary<string, object?>();
                }
            }

            var msg = Builders.MakeIdentity(
                subject,
                claims: claims,
                includeClaims: hasClaims,
                fingerprint: fingerprint,
                transport: transport,
                secret: secret);
            var got = (string)msg["signature"]!;
            if (!string.Equals(expected, got, StringComparison.Ordinal))
            {
                failures.Add($"subject={subject} claims={item.GetProperty("claims_json")} expected={expected} got={got}");
            }

            checkedCount++;
        }

        Assert.True(checkedCount >= 500);
        Assert.True(failures.Count == 0, string.Join("\n", failures.Take(10)));
    }
}
