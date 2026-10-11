//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Provide.Uterm.Ansi;
using Provide.Uterm.Auth;
using Provide.Uterm.Bridge;
using Provide.Uterm.Channels;
using Provide.Uterm.Cli;
using Provide.Uterm.Colors;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.CtrlMsg;
using Provide.Uterm.DeckMux;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// Pure-library arms the earlier coverage waves left unexercised: overflowing
/// SGR digits, comment-less authorized_keys lines, protocol ranges carried as
/// doubles, malformed control frames and audit records.
/// </summary>
public sealed class Coverage100LibraryTests
{
    [Fact]
    public void Upgrade_keeps_an_sgr_parameter_too_large_for_int_with_its_leading_zeros_stripped()
    {
        // int.TryParse fails on overflow only (the regex admits digits alone),
        // and the parameter is passed through rather than mapped.
        Assert.Equal("\x1b[99999999999m", Upgrade.UpgradeTo256("\x1b[0099999999999m"));
    }

    [Fact]
    public async Task Authorized_keys_line_without_comment_resolves_to_a_fingerprint_subject()
    {
        var blob = new byte[] { 0, 0, 0, 11, (byte)'s', (byte)'s', (byte)'h', (byte)'-', (byte)'e', (byte)'d', (byte)'2', (byte)'5', (byte)'5', (byte)'1', (byte)'9', 1, 2, 3 };
        var b64 = Convert.ToBase64String(blob);
        var path = Path.Combine(Path.GetTempPath(), "ak-" + Guid.NewGuid().ToString("N"));
        // A single bare token (no whitespace) is skipped as malformed; the
        // comment-less key below it still resolves.
        await File.WriteAllLinesAsync(path, new[] { "garbage-without-space", "ssh-ed25519 " + b64 });
        try
        {
            var fp = SshAuth.FingerprintFromOpenSshBlob(blob);
            var id = await new SshAuth.AuthorizedKeysFileResolver(path).ResolveAsync(fp, blob, "u");
            Assert.NotNull(id);
            Assert.Equal("key:" + fp, id!.Subject);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Protocol_range_accepts_doubles_floors_zero_and_falls_back_on_strings()
    {
        Assert.Equal((1, 1), ProtocolContracts.ParseClientRange(new Dictionary<string, object?> { ["protocol_version"] = 0 }));
        Assert.Equal((2, 2), ProtocolContracts.ParseClientRange(new Dictionary<string, object?> { ["protocol_version"] = 2.9 }));
        var proto = new Dictionary<string, object?> { ["min"] = "x", ["max"] = 3.0 };
        var (min, max) = ProtocolContracts.ParseClientRange(new Dictionary<string, object?> { ["protocol"] = proto });
        Assert.Equal(ProtocolContracts.MinProtocolVersion, min);
        Assert.Equal(3, max);
    }

    [Fact]
    public async Task Watchdog_clamps_a_tiny_interval_and_reports_a_stuck_worker()
    {
        var h = new Hijackable();
        var fired = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        h.StartWatchdog(TimeSpan.Zero, TimeSpan.FromMilliseconds(1), () => fired.TrySetResult());
        try
        {
            await fired.Task.WaitAsync(TimeSpan.FromSeconds(10));
        }
        finally
        {
            h.StopWatchdog();
        }
    }

    [Fact]
    public void Channel_hello_with_an_undecodable_control_frame_is_not_a_hello()
    {
        var good = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?> { ["type"] = "hello", ["x"] = 1 });
        // Same length, invalid JSON: structurally a frame, so it reaches the decoder.
        var bad = good[..^4] + "!!!}";
        Assert.True(ControlChannelCodec.IsControlFrame(bad));
        Assert.Null(Negotiated.ParseChannelHello(bad));
    }

    [Fact]
    public void Audit_verify_accepts_double_seq_and_rejects_an_unencodable_detail()
    {
        var rec = AuditChain.MakeRecord(1, AuditChain.GenesisHash);
        rec["seq"] = 1.0;
        rec["record_hash"] = AuditChain.ComputeRecordHash(AuditChain.CanonicalPayload(rec));
        var ok = AuditChain.VerifyRecords(new List<Dictionary<string, object?>> { rec });
        Assert.True(ok.Ok);
        Assert.Equal(1, ok.HeadSeq);

        var bad = AuditChain.MakeRecord(1, AuditChain.GenesisHash);
        bad["detail"] = new object();
        var res = AuditChain.VerifyRecords(new List<Dictionary<string, object?>> { bad });
        Assert.False(res.Ok);
        Assert.Equal("malformed record", res.Reason);
        Assert.Equal(1, res.FirstBadSeq);
    }

    [Theory]
    [InlineData("38;2;;1;2")]
    [InlineData("38;2;1a;1;2")]
    public void Sgr_truecolor_with_a_non_numeric_component_is_left_untouched(string p)
    {
        Assert.Equal("\x1b[" + p + "m", Sgr.RewriteParams(p, ColorMode.Mode256));
    }

    [Fact]
    public void Sgr_truecolor_component_wider_than_nine_digits_saturates()
    {
        // 1<<30 saturates every channel, i.e. maps exactly like full white.
        var huge = Sgr.RewriteParams("38;2;1234567890;1234567890;1234567890", ColorMode.Mode256);
        Assert.Equal("\x1b[38;5;" + Rgb.RgbTo256(1 << 30, 1 << 30, 1 << 30) + "m", huge);
    }

    [Fact]
    public void Canonical_json_refuses_an_undefined_json_element()
    {
        var ex = Assert.Throws<ArgumentException>(() => CanonicalJson.Serialize(default(JsonElement)));
        Assert.Contains("Undefined", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Generate_color_falls_back_to_the_base_color_when_every_color_is_taken()
    {
        var taken = new HashSet<string>();
        string? first = null;
        for (var i = 0; i < 64; i++)
        {
            var c = IdentityNames.GenerateColor("conn-a", taken);
            first ??= c;
            if (!taken.Add(c))
            {
                // Everything was taken: the deterministic base color comes back.
                Assert.Equal(first, c);
                return;
            }
        }

        Assert.Fail("palette never exhausted");
    }

    [Fact]
    public void Presence_update_coerces_unknown_ints_to_zero_and_copies_read_only_dictionaries()
    {
        var store = new PresenceStore();
        store.Add("u1", "U", "#fff", "viewer", "UU");
        var sel = new SortedDictionary<string, object?> { ["a"] = 1 };
        var (user, ok) = store.Update("u1", new Dictionary<string, object?>
        {
            ["scroll_line"] = "not-a-number",
            ["selection"] = sel,
        });
        Assert.True(ok);
        Assert.Equal(0, user!.ScrollLine);
        Assert.Equal(1, user.Selection!["a"]);
    }
}
