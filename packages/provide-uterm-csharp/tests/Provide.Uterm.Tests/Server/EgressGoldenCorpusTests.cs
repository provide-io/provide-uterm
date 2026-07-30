//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Server;

namespace Provide.Uterm.Tests.Server;

/// <summary>
/// Hold this port's egress classifier against the recorded reference corpus.
///
/// Every other check on this guard is hand-written here, which means a range the
/// reference blocks and this port does not can only be caught by somebody
/// noticing. That is not hypothetical: RFC 6598 CGNAT was refused on the
/// connector path and permitted on the webhook path for as long as it took to
/// re-read the comment explaining why, and the Go corpus separately recorded
/// <c>100.64.0.1</c> as <em>permitted</em> — so the Go suite faithfully enforced
/// a hole the reference had. See <c>conformance/EGRESS_GUARD.md</c> §1.
///
/// The corpus is generated from the CPython reference and regenerated on every CI
/// run by <c>.ci/check_goldens.sh</c>, so it cannot quietly rot: a reference
/// change rewrites the file and the diff fails the gate. Binding C# to it makes
/// this port fail in the same breath rather than staying silently behind.
///
/// Go's copy is read rather than TypeScript's on purpose. TypeScript's records
/// the reference's <em>error message text</em> (<c>"connector peer '…' is a
/// blocked internal address"</c>), which is Python prose no other language can
/// reproduce without copying English into its assertions. Go's records
/// decisions — a boolean per policy mode and a reason word — which is what a
/// cross-language corpus has to be. Reading another package's testdata is
/// already the idiom here; <c>ServerConfigNestedKeyTests</c> ascends to the
/// TypeScript corpus the same way.
/// </summary>
public sealed class EgressGoldenCorpusTests
{
    private sealed record AddressCase(
        string Ip,
        bool BlockedDefault,
        string BlockedDefaultReason,
        bool BlockedPrivate,
        string BlockedPrivateReason);

    /// <summary>The recorded corpus, found by ascending from the test binary.</summary>
    /// <remarks>
    /// Read live rather than copied into the output directory: the package
    /// Makefile runs <c>dotnet test --no-build</c>, so a copy step would not
    /// re-run and a regenerated corpus could be compared against a stale copy
    /// with nothing looking wrong.
    /// </remarks>
    private static string LocateCorpus()
    {
        var parts = new[] { "packages", "provide-uterm-go", "server", "testdata", "egress_golden.json" };
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(new[] { dir.FullName }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        throw new FileNotFoundException("egress_golden.json not found above " + AppContext.BaseDirectory);
    }

    private static List<AddressCase> AddressCases()
    {
        using var document = JsonDocument.Parse(File.ReadAllText(LocateCorpus()));
        return document.RootElement.GetProperty("ips").EnumerateArray().Select(row => new AddressCase(
            row.GetProperty("ip").GetString()!,
            row.GetProperty("blocked_default").GetBoolean(),
            row.GetProperty("blocked_default_reason").GetString() ?? "",
            row.GetProperty("blocked_private").GetBoolean(),
            row.GetProperty("blocked_private_reason").GetString() ?? "")).ToList();
    }

    /// <summary>Run the guard and report why it refused, or null if it did not.</summary>
    /// <remarks>
    /// Every corpus row is an IP literal, so this takes the
    /// <c>IPAddress.TryParse</c> branch and never touches DNS — the test is
    /// hermetic without needing a resolver seam.
    /// </remarks>
    private static async Task<string?> RefusalReasonAsync(string ip, bool blockPrivate)
    {
        try
        {
            await EgressGuard.AssertConnectorTargetAllowedAsync(ip, blockPrivate).ConfigureAwait(false);
            return null;
        }
        catch (InvalidOperationException error)
        {
            // The corpus records a reason *word*; this port spells its messages
            // its own way, and should keep doing so. Only the classification is
            // the contract, so map the message back to the vocabulary rather
            // than asserting on prose.
            if (error.Message.Contains("metadata", StringComparison.Ordinal))
            {
                return "metadata";
            }

            return "private";
        }
    }

    [Fact]
    public async Task EveryRecordedAddressIsClassifiedTheSameWay()
    {
        var mismatches = new List<string>();

        foreach (var recorded in AddressCases())
        {
            foreach (var (blockPrivate, expectedBlocked, expectedReason) in new[]
                     {
                         (false, recorded.BlockedDefault, recorded.BlockedDefaultReason),
                         (true, recorded.BlockedPrivate, recorded.BlockedPrivateReason),
                     })
            {
                var reason = await RefusalReasonAsync(recorded.Ip, blockPrivate);
                var blocked = reason is not null;

                if (blocked != expectedBlocked)
                {
                    mismatches.Add(
                        $"{recorded.Ip} blockPrivate={blockPrivate}: reference says blocked={expectedBlocked}, "
                            + $"this port says blocked={blocked}");
                }
                else if (blocked && reason != expectedReason)
                {
                    mismatches.Add(
                        $"{recorded.Ip} blockPrivate={blockPrivate}: reference says '{expectedReason}', "
                            + $"this port says '{reason}'");
                }
            }
        }

        Assert.True(
            mismatches.Count == 0,
            $"{mismatches.Count} address(es) classified differently from the reference:\n  "
                + string.Join("\n  ", mismatches));
    }

    [Fact]
    public void TheCorpusIsBigEnoughToBeWorthReading()
    {
        // A corpus that failed to load, or loaded as an empty array, would make
        // the test above pass while asserting nothing at all — the failure mode
        // this whole exercise exists to catch, reproduced inside its own guard.
        var cases = AddressCases();

        Assert.True(cases.Count >= 40, $"only {cases.Count} recorded addresses — corpus did not load properly");
        Assert.Contains(cases, row => row.Ip == "169.254.169.254" && row.BlockedDefault);
        Assert.Contains(cases, row => row.Ip == "100.64.0.1" && row.BlockedPrivate);
    }
}
