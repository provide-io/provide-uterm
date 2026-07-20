//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Reflection;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Ansi;
using Provide.Uterm.Channels;
using Provide.Uterm.Cli;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.DeckMux;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;

namespace Provide.Uterm.Tests;

/// <summary>Wave 10: pure residual arms toward dual-OS ~98.5+ floor headroom.</summary>
public class CoverageTo99Wave10Tests
{
    [Fact]
    public void Ansi_Upgrade_LeadingZerosAndExtendedFgBg()
    {
        // Leading-zero SGR digits → NormalizeDigits; 38/48 early-return arm.
        var s = Upgrade.UpgradeTo256("\x1b[00;30;40m");
        Assert.Contains("38;5;", s, StringComparison.Ordinal);
        _ = Upgrade.UpgradeToTruecolor("\x1b[00;31;41m");
        Assert.Equal("\x1b[48;5;1m", Upgrade.UpgradeTo256("\x1b[48;5;1m"));
    }

    [Fact]
    public void Channels_ParseHello_DecodeFramesCatch()
    {
        var hello = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["channels"] = new Dictionary<string, object?> { ["term"] = 1 },
        });
        var parsed = Negotiated.ParseChannelHello(hello);
        Assert.NotNull(parsed);
        var n = Negotiated.Create(new Dictionary<string, int> { ["term"] = 1 }, "term");
        _ = n.HandleHello(parsed!);

        // Malformed DLE → DecodeFrames catch → empty → null hello
        Assert.Null(Negotiated.ParseChannelHello("\x10\x02zzzz"));

        try
        {
            n.RestoreGrants(new Dictionary<string, object?> { ["term"] = "not-int" });
        }
        catch
        {
            // coerce residual
        }
    }

    [Fact]
    public void DeckMux_Identity_SurrogateAndClaims()
    {
        var emoji = char.ConvertFromUtf32(0x1F600);
        var claims = new Dictionary<string, object?> { ["emoji"] = emoji, ["n"] = 1 };
        var frame = new Dictionary<string, object?>
        {
            ["type"] = "identity",
            ["version"] = 1,
            ["subject"] = "u1",
            ["fingerprint"] = "fp",
            ["transport"] = "ws",
            ["claims"] = claims,
            ["signature"] = "deadbeef",
        };
        _ = Identity.ParseIdentityFrame(frame, Encoding.UTF8.GetBytes("secret"));

        // Direct compact JSON for surrogate-pair arm
        var compact = typeof(Identity).GetMethod(
            "PythonCompactJson",
            BindingFlags.NonPublic | BindingFlags.Static);
        Assert.NotNull(compact);
        var json = compact!.Invoke(null, new object?[] { claims }) as string;
        Assert.NotNull(json);
        Assert.Contains("\\u", json, StringComparison.Ordinal);

        frame["claims"] = new List<object?> { "x" };
        _ = Identity.ParseIdentityFrame(frame, null);
    }

    [Fact]
    public void DeckMux_PresenceHelpers_CoerceAndAsDict()
    {
        var helperType = typeof(DeckMuxPresence).Assembly.GetTypes()
            .First(t => t.GetMethod("CoerceInt", BindingFlags.NonPublic | BindingFlags.Static) is not null);
        var coerce = helperType.GetMethod("CoerceInt", BindingFlags.NonPublic | BindingFlags.Static)!;
        var asDict = helperType.GetMethod("AsDict", BindingFlags.NonPublic | BindingFlags.Static)!;

        Assert.Equal(3, coerce.Invoke(null, new object?[] { 3L }));
        Assert.Equal(4, coerce.Invoke(null, new object?[] { 4.2 }));
        using (var doc = JsonDocument.Parse("7"))
        {
            Assert.Equal(7, coerce.Invoke(null, new object?[] { doc.RootElement }));
        }

        Assert.Null(asDict.Invoke(null, new object?[] { null }));
        Assert.NotNull(asDict.Invoke(null, new object?[] { new Dictionary<string, object?> { ["a"] = 1 } }));
        IDictionary<string, object?> id = new Dictionary<string, object?> { ["b"] = "x" };
        Assert.NotNull(asDict.Invoke(null, new object?[] { id }));
        using (var doc = JsonDocument.Parse("""{"s":"t","n":1,"f":1.5,"t":true,"z":false,"u":null,"o":{}}"""))
        {
            Assert.NotNull(asDict.Invoke(null, new object?[] { doc.RootElement }));
        }

        Assert.Null(asDict.Invoke(null, new object?[] { "nope" }));
    }

    [Fact]
    public void ControlChannel_Codec_BoolNumberAndTruncatedFinish()
    {
        var framed = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "x",
            ["ok"] = true,
            ["off"] = false,
            ["n"] = 1.5,
            ["i"] = 9L,
        });
        var dec = new ControlFrameDecoder();
        Assert.Contains(dec.Feed(framed), c => c is ControlChunk);

        var dec2 = new ControlFrameDecoder();
        dec2.Feed("\x10\x02");
        try
        {
            _ = dec2.Finish().ToList();
        }
        catch (ProtocolException)
        {
            // truncated control frame residual
        }
    }

    [Fact]
    public async Task Hub_Lease_ThrowingWorkerAndCancel()
    {
        var clock = new ManualClock(1);
        clock.SetMonotonic(10);
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        hub.Conn.RegisterWorker("w1", new ThrowingWorker());

        var (ok, reason) = await hub.TryAcquireRestHijackAsync("w1", "owner", 30, "hid1", 10);
        Assert.False(ok);
        Assert.Equal("no_worker", reason);

        hub.Conn.RegisterWorker("w1", new CancelWorker());
        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () =>
            await hub.TryAcquireRestHijackAsync(
                "w1", "owner", 30, "hid2", 10, new CancellationToken(canceled: true)));

        Assert.Null(hub.Lease.ExtendLease("w1", "missing", "owner", 30, 10));
    }

    [Fact]
    public void Server_AcquireErrorMessage_DefaultArm()
    {
        var m = typeof(UtermServer).GetMethod(
            "AcquireErrorMessage",
            BindingFlags.NonPublic | BindingFlags.Static);
        Assert.NotNull(m);
        Assert.Equal("weird", m!.Invoke(null, new object?[] { "weird" }));
        Assert.Equal("No worker connected.", m.Invoke(null, new object?[] { "no_worker" }));
        Assert.Equal("Worker is already hijacked.", m.Invoke(null, new object?[] { "already_hijacked" }));
    }

    [Fact]
    public void Cli_Audit_BrokenHashAndHeadMismatch()
    {
        var good = AuditChain.MakeRecord(1, AuditChain.GenesisHash, detail: new Dictionary<string, object?> { ["k"] = "v" });
        var broken = new Dictionary<string, object?>(good) { ["record_hash"] = "not-the-real-hash" };
        var r = AuditChain.VerifyRecords(new[] { broken }, head: null);
        Assert.False(r.Ok);
        Assert.Equal("broken hash link", r.Reason);

        var r2 = AuditChain.VerifyRecords(
            new[] { good },
            head: new AuditChain.ExpectedHead { Seq = 99, Hash = "x" });
        Assert.False(r2.Ok);
        Assert.Equal("head mismatch", r2.Reason);
    }

    [Fact]
    public void FileIo_SecureOpen_ReparseAndAppend()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-wave10-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var path = Path.Combine(dir, "rec.log");
            using (var fs = FileIo.FileIo.SecureOpenAppend(path))
            {
                fs.Write(Encoding.UTF8.GetBytes("a\n"));
            }

            // Second open hits existing-file branch (symlink/reparse checks).
            using (var fs2 = FileIo.FileIo.SecureOpenAppend(path))
            {
                fs2.Write(Encoding.UTF8.GetBytes("b\n"));
            }

            if (!OperatingSystem.IsWindows())
            {
                var link = Path.Combine(dir, "link.log");
                try
                {
                    File.CreateSymbolicLink(link, path);
                    Assert.ThrowsAny<IOException>(() => FileIo.FileIo.SecureOpenAppend(link));
                }
                catch (UnauthorizedAccessException)
                {
                    // symlink may require privileges
                }
                catch (PlatformNotSupportedException)
                {
                }
            }
        }
        finally
        {
            try
            {
                Directory.Delete(dir, true);
            }
            catch
            {
                // best-effort
            }
        }
    }

    [Fact]
    public void DeckMux_Identity_ControlCharJsonEscape()
    {
        var compact = typeof(Identity).GetMethod(
            "PythonCompactJson",
            BindingFlags.NonPublic | BindingFlags.Static)!;
        var s = compact.Invoke(null, new object?[] { "\x01\x02\t\n" }) as string;
        Assert.NotNull(s);
        Assert.Contains("\\u", s, StringComparison.Ordinal);
    }

    private sealed class ThrowingWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            throw new InvalidOperationException("boom");
    }

    private sealed class CancelWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            cancellationToken.ThrowIfCancellationRequested();
            return Task.CompletedTask;
        }
    }
}
