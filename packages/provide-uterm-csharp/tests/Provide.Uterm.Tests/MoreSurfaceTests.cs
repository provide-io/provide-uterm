//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Annotation;
using Provide.Uterm.Ansi;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Emulator;
using Provide.Uterm.Gui;
using Provide.Uterm.Manager;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Session;
using Provide.Uterm.Transports;
using Provide.Uterm.Vnc;
using FileIoHelper = Provide.Uterm.FileIo.FileIo;

namespace Provide.Uterm.Tests;

public class MoreSurfaceTests
{
    [Fact]
    public void ColorDialectRegistry_NormalizesDialects()
    {
        var names = ColorDialectRegistry.RegisteredDialects();
        Assert.Contains("brace_tokens", names);
        Assert.Contains("extended_tokens", names);
        Assert.Contains("tilde_codes", names);
        Assert.Contains("pipe_codes", names);

        var brace = ColorDialectRegistry.NormalizeColors("{+r}RED{-x}");
        Assert.Contains("\x1b[", brace, StringComparison.Ordinal);
        Assert.Contains("RED", brace, StringComparison.Ordinal);

        var ext = ColorDialectRegistry.NormalizeColors("{F196}{B21}{P1}{T2}X");
        Assert.Contains("38;5;196", ext, StringComparison.Ordinal);

        var tilde = ColorDialectRegistry.NormalizeColors("~1green~0");
        Assert.Contains("\x1b[", tilde, StringComparison.Ordinal);

        var pipe = ColorDialectRegistry.NormalizeColors("|12Hello|07");
        Assert.Contains("Hello", pipe, StringComparison.Ordinal);

        Assert.Equal(brace, ColorDialectRegistry.PreviewAnsi("{+r}RED{-x}"));
        Assert.NotNull(ColorDialectRegistry.RegisterColorDialect("brace_tokens", s => s));
        var name = "custom_ms_" + Guid.NewGuid().ToString("N")[..8];
        Assert.Null(ColorDialectRegistry.RegisterColorDialect(name, s => s + "!"));
        Assert.Null(ColorDialectRegistry.UnregisterColorDialect(name));
        Assert.NotNull(ColorDialectRegistry.UnregisterColorDialect("nope"));
    }

    [Fact]
    public void Annotation_PatternDetector()
    {
        var det = new PatternDetector();
        var hits = det.Detect("write", "password=hunter2 and sudo rm -rf /");
        Assert.NotEmpty(hits);
        Assert.NotEmpty(hits[0].ToDict());
        Assert.Empty(det.Detect("read", "hello world"));
        var stream = new StreamingDetector(det);
        Assert.NotEmpty(stream.Feed("write", "password=x"));
        stream.Reset();
    }

    [Fact]
    public void FileIo_SecureOpen_And_LoadAns()
    {
        var dir = Path.Combine(Path.GetTempPath(), "fio-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var path = Path.Combine(dir, "rec.jsonl");
            using (var fs = FileIoHelper.SecureOpenAppend(path))
            {
                fs.Write(Encoding.UTF8.GetBytes("line\n"));
            }

            Assert.True(File.Exists(path));
            var ans = Path.Combine(dir, "screen.ans");
            File.WriteAllBytes(ans, Encoding.GetEncoding("ISO-8859-1").GetBytes("\x1b[31mHi\x1b[0m"));
            Assert.Contains("Hi", FileIoHelper.LoadAns(ans), StringComparison.Ordinal);
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }

    [Fact]
    public void Vnc_And_Gui()
    {
        var t = new FramebufferTracker(4, 4);
        var pixels = new byte[2 * 2 * 4];
        for (var i = 0; i < pixels.Length; i++) pixels[i] = (byte)(i + 1);
        t.ApplyRawUpdate(1, 1, 2, 2, pixels);
        Assert.Equal(4, t.GetImage().Width);
        Assert.Throws<ArgumentException>(() => t.ApplyRawUpdate(0, 0, -1, 1, pixels));

        var g = new MemoryGraphicalSession(8, 8);
        g.InjectPointer(2, 2, 1);
        g.InjectKey(65, true);
        Assert.Equal(8, g.Screenshot().Width);
    }

    [Fact]
    public async Task LocalIdentity_HeaderMode()
    {
        var cfg = new AuthConfig
        {
            Mode = "header",
            HeaderModeAcknowledged = true,
            TrustedProxyIps = new List<string> { "127.0.0.1" },
            PrincipalHeader = "x-uterm-principal",
            RoleHeader = "x-uterm-role",
        };
        var idp = new LocalIdentityProvider(cfg);
        var p = await idp.AuthenticateAsync(new AuthRequest
        {
            SourceIp = "127.0.0.1",
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["x-uterm-principal"] = "alice",
                ["x-uterm-role"] = "admin",
            },
        });
        Assert.Equal("alice", p.SubjectId);
        var denied = await idp.AuthenticateAsync(new AuthRequest { SourceIp = "10.0.0.1" });
        Assert.Equal("anonymous", denied.SubjectId);
    }

    [Fact]
    public async Task Expect_SendAndExpect()
    {
        var session = new FakeExpectSession("ready>");
        var result = await Expect.SendAndExpectAsync(session, "cmd\n", new ExpectOptions
        {
            ExpectText = "ready",
            Timeout = TimeSpan.FromMilliseconds(200),
        });
        Assert.True(result.Matched);
        var no = await Expect.SendAndExpectAsync(session, "x", new ExpectOptions
        {
            ExpectText = "never-match-zzz",
            Timeout = TimeSpan.FromMilliseconds(50),
        });
        Assert.False(no.Matched);
    }

    [Fact]
    public void ControlChannel_And_Emulator()
    {
        var dec = new ControlFrameDecoder(new DecoderOptions { OnError = _ => { } });
        _ = dec.Feed("he").ToList();
        var more = dec.Feed("lo").Concat(dec.Finish()).ToList();
        Assert.Contains(more, c => c is DataChunk);
        Assert.False(ControlChannelCodec.IsControlFrame("plain"));

        var emu = new TerminalEmulator(40, 8);
        emu.Process(Encoding.ASCII.GetBytes("1\r\n2\r\n\x1b[1;1H\x1b[2Lfinal"));
        Assert.Contains("final", emu.GetSnapshot().Screen, StringComparison.Ordinal);
        Assert.NotNull(TransportErrors.NotConnected);
        Assert.True(new ConnectOptions().WithDefaults().Cols > 0);
    }

    [Fact]
    public async Task ManagerProgram_Help()
    {
        Assert.Equal(0, await ManagerProgram.RunAsync(["--help"]));
        Assert.Equal(0, ManagerHost.Run(["--help"]));
    }

    private sealed class FakeExpectSession : IExpectSession
    {
        private readonly string _screen;
        private int _seq;
        public FakeExpectSession(string screen) => _screen = screen;
        public Task SendAsync(string data, CancellationToken ct = default)
        {
            _seq++;
            return Task.CompletedTask;
        }

        public Snapshot Snapshot() => new() { Screen = _screen, Cols = 80, Rows = 25 };
        public int ScreenChangeSeq() => _seq;
        public Task<bool> WaitForScreenChangeAsync(TimeSpan timeout, int since, CancellationToken cancellationToken = default)
        {
            _seq = Math.Max(_seq, since + 1);
            return Task.FromResult(true);
        }
    }
}
