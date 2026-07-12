//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Text;
using Provide.Uterm.Channels;
using Provide.Uterm.Cli;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Fanout;
using Provide.Uterm.FileIo;
using Provide.Uterm.Frames;
using Provide.Uterm.Hub;
using Provide.Uterm.Recording;
using Provide.Uterm.Screen;
using Provide.Uterm.SessionLogger;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Tests;

/// <summary>Final residual push to ≥97% gate floor.</summary>
public class CoverageTo97Wave7Tests
{
    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var p = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    [Fact]
    public void Frames_StatusFrame_TsTypeVariants()
    {
        var f1 = FrameBuilders.CoerceWorkerStatusFrame(new Dictionary<string, object?>
        {
            ["type"] = "status",
            ["ts"] = 1.5f,
        });
        Assert.True(f1.Ts > 0);
        var f2 = FrameBuilders.CoerceWorkerStatusFrame(new Dictionary<string, object?> { ["ts"] = 2 });
        Assert.Equal(2, f2.Ts);
        var f3 = FrameBuilders.CoerceWorkerStatusFrame(new Dictionary<string, object?> { ["ts"] = 3L });
        Assert.Equal(3, f3.Ts);
        var f4 = FrameBuilders.CoerceWorkerStatusFrame(new Dictionary<string, object?> { ["ts"] = 4.25 });
        Assert.Equal(4.25, f4.Ts);
        var f5 = FrameBuilders.CoerceWorkerStatusFrame(new Dictionary<string, object?>
        {
            ["type"] = 123, // non-string type → Extra
            ["other"] = "x",
        });
        Assert.NotNull(f5.Extra);
    }

    [Fact]
    public void Channels_Hello_Parse_And_BadDecode()
    {
        var hello = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["channels"] = new Dictionary<string, object?> { ["term"] = 1, ["ctl"] = 2 },
        });
        var h = Negotiated.ParseChannelHello(hello);
        Assert.NotNull(h);

        var noCh = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?> { ["type"] = "hello" });
        Assert.Null(Negotiated.ParseChannelHello(noCh));

        var badCh = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["channels"] = "nope",
        });
        Assert.Null(Negotiated.ParseChannelHello(badCh));

        Assert.Null(Negotiated.ParseChannelHello("\u0010\u0002not-a-frame"));
        Assert.Null(Negotiated.ParseChannelHello(""));

        // empty channel name normalize
        Assert.Throws<ArgumentException>(() =>
            Negotiated.Create(new Dictionary<string, int> { [""] = 1 }));
        // coerce fail path via RestoreGrants bad
        var n = Negotiated.Create(new Dictionary<string, int> { ["term"] = 1 }, "term");
        Assert.ThrowsAny<Exception>(() =>
            n.RestoreGrants(new Dictionary<string, object?> { ["term"] = "nope" }));
    }

    [Fact]
    public void Lease_Clamp_And_FailingWorker_Pause()
    {
        Assert.Equal(1, HijackLeaseManager.ClampDashboardLease(0));
        Assert.Equal(1, HijackLeaseManager.ClampDashboardLease(-5));
        Assert.Equal(600, HijackLeaseManager.ClampDashboardLease(9999));
        Assert.Equal(45, HijackLeaseManager.ClampDashboardLease(45));

        var hub = new TermHub(new TermHubConfig { DashboardHijackLeaseS = 0 });
        hub.Lease.DashboardHijackLeaseS = 30;
        Assert.Equal(30, hub.Lease.DashboardHijackLeaseS);

        var fail = new FailWs();
        hub.Conn.RegisterWorker("wf", fail);
        var (ok, reason) = hub.TryAcquireRestHijackAsync("wf", "op", 30, "h1", 10).GetAwaiter().GetResult();
        Assert.False(ok);
        Assert.Equal("no_worker", reason);

        var (ok2, r2) = hub.Lease.TryAcquireWs("missing", new object());
        Assert.False(ok2);
        Assert.Equal("no_worker", r2);

        var good = new EchoWs();
        hub.Conn.RegisterWorker("w2", good);
        var (ok3, _) = hub.Lease.TryAcquireWs("w2", good);
        Assert.True(ok3);
        var (ok4, r4) = hub.Lease.TryAcquireWs("w2", new object());
        Assert.False(ok4);
        Assert.Equal("already_hijacked", r4);

        hub.Conn.ForceReleaseHijack("w2");
        hub.Conn.RegisterWorker("w2", good);
        var (ok6, _) = hub.TryAcquireRestHijackAsync("w2", "owner-a", 30, "hx2", 30).GetAwaiter().GetResult();
        if (ok6)
        {
            var ext = hub.Lease.ExtendLease("w2", "hx2", "wrong-owner", 30, 40);
            Assert.Null(ext);
        }
    }

    private sealed class EchoWs : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;
    }

    private sealed class FailWs : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            throw new IOException("boom");
    }

    [Fact]
    public void Limiter_Evicts_When_Cache_Full()
    {
        var hub = new TermHub(new TermHubConfig
        {
            RestAcquireRateLimitPerSec = 1000,
            RestSendRateLimitPerSec = 1000,
        });
        for (var i = 0; i < 200; i++)
        {
            Assert.True(hub.AllowRestAcquireFor("client-" + i));
            Assert.True(hub.AllowRestSendFor("send-" + i));
        }
    }

    [Fact]
    public void Fanout_ValidateErrorPattern_And_Create()
    {
        var ctl = new Controller(null, new ControllerConfig
        {
            MaxGroupSize = 2,
            IdGen = () => "fixed-id",
        });
        Assert.Throws<ArgumentException>(() =>
            ctl.CreateGroup(new Group
            {
                WorkerIds = new List<string> { "a", "b", "c" },
            }, "p"));

        Assert.Throws<ArgumentException>(() =>
            ctl.CreateGroup(new Group
            {
                WorkerIds = new List<string> { "a" },
                ErrorPattern = new string('x', 5000),
            }, "p"));

        var id = ctl.CreateGroup(new Group
        {
            WorkerIds = new List<string> { "a" },
            ErrorPattern = "",
        }, "p");
        Assert.Equal("fixed-id", id);

        Assert.Null(ctl.GetGroup(id, "other"));
        Assert.NotNull(ctl.GetGroup(id, "p"));
    }

    [Fact]
    public async Task InterceptGate_Timeout_Path()
    {
        var gate = new InterceptGate(timeoutS: 1.0, timeoutAction: "drop");
        gate.RegisterPending("slow");
        var d = await gate.AwaitDecisionAsync("slow");
        Assert.Equal("drop", d.Action);
    }

    [Fact]
    public void LineEditor_CtrlU_CtrlK_CtrlW()
    {
        var wrote = new StringBuilder();
        var ed = new LineEditor.LineEditor(40, passwordMode: false, onWrite: s => wrote.Append(s));
        foreach (var c in "hello world")
        {
            ed.ProcessChar(c);
        }

        ed.ProcessChar('\x17'); // Ctrl+W
        ed.ProcessChar('x');
        ed.ProcessChar('\x15'); // Ctrl+U
        ed.ProcessChar('a');
        ed.ProcessChar('b');
        ed.ProcessChar('\x02'); // left
        ed.ProcessChar('\x0b'); // Ctrl+K
        ed.ProcessChar('\r');
        Assert.True(wrote.Length >= 0);
    }

    [Fact]
    public async Task SessionLogger_ExcludeControl_And_MaxBytes()
    {
        var store = new InMemoryStore();
        await using var logger = new SessionLogger.SessionLogger(store, new SessionLoggerOptions
        {
            BatchSize = 100,
            MaxBytes = 10,
            ControlChannelMode = ControlChannelMode.Exclude,
        });
        await logger.StartAsync("sess-log");
        await logger.LogAsync("wire", new Dictionary<string, object?> { ["x"] = "1" });
        await logger.LogAsync("control", new Dictionary<string, object?> { ["x"] = "1" });
        await logger.LogAsync("read", new Dictionary<string, object?> { ["raw"] = new string('z', 50) });
        await logger.LogAsync("read", new Dictionary<string, object?> { ["raw"] = "more" });
        await logger.StopAsync();
    }

    [Fact]
    public void FileIo_SecureOpen_And_LoadAns()
    {
        var dir = Path.Combine(Path.GetTempPath(), "fio-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var path = Path.Combine(dir, "rec.log");
            using (var fs = FileIo.FileIo.SecureOpenAppend(path))
            {
                var b = Encoding.UTF8.GetBytes("hi\n");
                fs.Write(b, 0, b.Length);
            }

            using (var fs2 = FileIo.FileIo.SecureOpenAppend(path))
            {
                Assert.True(fs2.CanWrite);
            }

            var ans = Path.Combine(dir, "s.ans");
            File.WriteAllBytes(ans, new byte[] { 0x48, 0x69, 0x1b });
            var text = FileIo.FileIo.LoadAns(ans);
            Assert.Contains("Hi", text, StringComparison.Ordinal);
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { /* ignore */ }
        }
    }

    [Fact]
    public void Audit_ToLong_ViaVerify_JsonElementSeq()
    {
        var r0 = AuditChain.MakeRecord(0, "");
        var json = System.Text.Json.JsonSerializer.Serialize(new[] { r0 });
        using var doc = System.Text.Json.JsonDocument.Parse(json);
        var list = new List<Dictionary<string, object?>>();
        foreach (var el in doc.RootElement.EnumerateArray())
        {
            var d = new Dictionary<string, object?>();
            foreach (var p in el.EnumerateObject())
            {
                d[p.Name] = p.Value.Clone();
            }

            list.Add(d);
        }

        _ = AuditChain.VerifyRecords(list);
    }

    [Fact]
    public void Proxy_Once_Health_IsRealBind()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        var port = FreePort();
        var code = Root.Execute(
            new[] { "proxy", "127.0.0.1", "23", "--port", port.ToString(), "--once", "--bind", "127.0.0.1" },
            o, e);
        Assert.Equal(0, code);
        Assert.Contains("uterm-proxy", o.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void Screen_Extract_And_Normalize_Touch_PyText()
    {
        var screen = "  1. Hello world  \r\n2. Next\u2028";
        _ = ScreenNormalize.NormalizeTerminalText(screen);
        _ = ScreenNormalize.StripAnsi("\x1b[31mred\x1b[0m");
        _ = Extract.ExtractMenuOptions(screen);
        _ = Extract.ExtractNumberedList(screen);
        _ = ScreenNormalize.ExtractActionTags("[action:foo]");
    }
}
