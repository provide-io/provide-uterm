//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Text;
using Provide.Uterm.Bridge;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.ControlPlane;
using Provide.Uterm.CtrlMsg;
using Provide.Uterm.Detection;
using Provide.Uterm.Emulator;
using Provide.Uterm.Fanout;
using Provide.Uterm.Gateway;
using Provide.Uterm.Render;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.Shell;
using Provide.Uterm.Session;
using RBuf = Provide.Uterm.Render.RenderBuffer;

namespace Provide.Uterm.Tests;

public class ModuleSurfaceTests
{
    [Fact]
    public async Task Hijackable_Gate_Step_Watchdog()
    {
        var h = new Hijackable();
        Assert.False(h.IsHijacked());
        await h.AwaitIfHijacked(); // no-op when free

        h.SetHijacked(true);
        Assert.True(h.IsHijacked());
        h.RequestStep(2);
        await h.AwaitIfHijacked(); // consumes one token
        await h.AwaitIfHijacked(); // consumes second

        var stuck = 0;
        h.MarkProgress();
        h.StartWatchdog(TimeSpan.FromMilliseconds(50), TimeSpan.FromMilliseconds(500), () => Interlocked.Increment(ref stuck));
        await Task.Delay(20);
        h.StopWatchdog();

        h.SetHijacked(false);
        Assert.False(h.IsHijacked());
        h.RequestStep(1); // ignored when not hijacked
        h.SetHijacked(true);
        h.SetHijacked(true); // no-op same state
        h.SetHijacked(false);
    }

    [Fact]
    public void ProtocolMismatch_Properties()
    {
        var ex = new ProtocolMismatchException(2, 3, 1, 1);
        Assert.Equal(2, ex.ClientMin);
        Assert.Equal(3, ex.ClientMax);
        Assert.Equal(1, ex.ServerMin);
        Assert.Equal(1, ex.ServerMax);
        Assert.Contains("protocol_mismatch", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task MemoryEngine_FullLifecycle()
    {
        var eng = new MemoryEngine();
        Assert.False(eng.Capabilities().Durable);
        await eng.OpenAsync();
        await eng.MigrateAsync();
        var tx = await eng.BeginAsync();
        await tx.CommitAsync();
        Assert.True(((MemoryTx)tx).IsDone);

        await eng.Sessions().UpsertAsync(new SessionRecord
        {
            SessionId = "s1",
            State = "active",
            CreatedAt = 1,
            Metadata = new Dictionary<string, object?> { ["k"] = "v" },
        });
        var got = await eng.Sessions().GetAsync("s1");
        Assert.Equal("s1", got!.SessionId);
        await eng.Sessions().MarkDeletedAsync("s1", 10);
        Assert.Equal("deleted", (await eng.Sessions().GetAsync("s1"))!.State);
        Assert.Equal(1, await eng.ReapAsync(now: 1000, retentionS: 10));

        await eng.Tokens().PutSessionTokenAsync(new SessionTokenRecord
        {
            SessionId = "s1", TokenKind = "player", TokenHash = "hh", ExpiresAt = 99,
        });
        Assert.Equal("hh", (await eng.Tokens().GetSessionTokenAsync("s1", "player"))!.TokenHash);

        await eng.Tokens().CreateResumeTokenAsync(new ResumeTokenRecord
        {
            TokenValue = "rt", SessionId = "s1", ExpiresAt = 50,
        });
        Assert.NotNull(await eng.Tokens().GetResumeTokenAsync("rt"));
        var consumed = await eng.Tokens().ConsumeResumeTokenAsync("rt", 51);
        Assert.NotNull(consumed);
        Assert.Null(await eng.Tokens().GetResumeTokenAsync("rt")); // revoked via consume
        await eng.Tokens().CreateResumeTokenAsync(new ResumeTokenRecord
        {
            TokenValue = "rt2", SessionId = "s1", ExpiresAt = 50,
        });
        await eng.Tokens().RevokeResumeTokenAsync("rt2", 52);
        Assert.Null(await eng.Tokens().GetResumeTokenAsync("rt2"));

        await eng.Approvals().PutApprovalAsync(new ApprovalRecord
        {
            ApprovalId = "a1", SessionId = "s1", Command = "ls", Status = "pending", CreatedAt = 1,
        });
        Assert.Equal("ls", (await eng.Approvals().GetApprovalAsync("a1"))!.Command);
        Assert.NotEmpty(await eng.Approvals().ListPendingAsync());

        await eng.Leases().PutLeaseAsync(new LeaseRecord
        {
            SessionId = "s1", Principal = "p", HijackId = "h", ExpiresAt = 9,
        });
        Assert.Equal("h", (await eng.Leases().GetLeaseAsync("s1"))!.HijackId);
        await eng.Leases().ClearLeaseAsync("s1");
        Assert.Null(await eng.Leases().GetLeaseAsync("s1"));

        await eng.SetAuditHeadAsync(1, "hash1");
        await eng.SetAuditHeadAsync(1, "ignored"); // seq not greater
        await eng.SetAuditHeadAsync(2, "hash2");
        Assert.Equal(2, (await eng.GetAuditHeadAsync())!.Seq);

        await eng.CloseAsync();
    }

    [Fact]
    public void Fanout_Controller_And_Divergence()
    {
        var ctrl = new Controller(null, new ControllerConfig
        {
            MaxGroupSize = 10,
            IdGen = () => "gid-1",
        });
        var id = ctrl.CreateGroup(new Group
        {
            Name = "g",
            WorkerIds = ["w1", "w2", "w3"],
            ErrorPattern = "error",
            DivergenceThreshold = 0.5,
        }, "alice");
        Assert.Equal("gid-1", id);
        Assert.NotNull(ctrl.GetGroup(id, "alice"));
        Assert.Single(ctrl.ListGroups("alice"));

        ctrl.GrantAccess(id, "bob", "alice");
        Assert.NotNull(ctrl.GetGroup(id, "bob"));

        var result = new Result
        {
            GroupId = id,
            Results =
            [
                new SessionResult { WorkerId = "w1", Ok = true, OutputDelta = "same" },
                new SessionResult { WorkerId = "w2", Ok = true, OutputDelta = "same" },
                new SessionResult { WorkerId = "w3", Ok = true, OutputDelta = "DIFFERENT" },
            ],
        };
        result = ctrl.FlagDivergence(result, ctrl.GetGroup(id, "alice")!);
        Assert.Contains("w3", result.DivergentSessions);
        Assert.NotEmpty(result.ResultMaps());
        Assert.Equal("w1", result.Results[0].ToMap()["worker_id"]?.ToString());

        ctrl.DeleteGroup(id, "alice");
        Assert.Null(ctrl.GetGroup(id, "alice"));

        Assert.Throws<ArgumentException>(() => ctrl.CreateGroup(new Group
        {
            WorkerIds = Enumerable.Range(0, 20).Select(i => "w" + i).ToList(),
        }, "x"));
    }

    [Fact]
    public void Detection_PromptPatterns()
    {
        var d = new Detector();
        d.AddPattern(new Dictionary<string, object?>
        {
            ["id"] = "shell",
            ["regex"] = @"\$\s*$",
            ["input_type"] = "command",
            ["negative_match"] = "Password:",
            ["expect_cursor_at_end"] = true,
        });
        d.AddPattern(new Dictionary<string, object?>
        {
            ["id"] = "pass",
            ["regex"] = @"Password:",
            ["negative_regex"] = @"logged in",
        });

        Assert.Null(d.Detect(new Dictionary<string, object?>
        {
            ["screen"] = "user$ ",
            ["cursor_at_end"] = false,
        }));
        var hit = d.Detect(new Dictionary<string, object?>
        {
            ["screen"] = "user$ ",
            ["cursor_at_end"] = true,
        });
        Assert.Equal("shell", hit!.PromptId);

        Assert.Null(d.Detect(new Dictionary<string, object?>
        {
            ["screen"] = "Password: logged in",
            ["cursor_at_end"] = true,
        }));
        Assert.Equal("pass", d.Detect(new Dictionary<string, object?>
        {
            ["screen"] = "Password:",
        })!.PromptId);

        Assert.Throws<ArgumentException>(() => d.AddPattern(new Dictionary<string, object?> { ["id"] = "" }));
    }

    [Fact]
    public void Shell_LineBuffer_And_Ansi()
    {
        var lb = new LineBuffer { MaxLength = 32 };
        Assert.Null(lb.Feed("he"));
        Assert.Equal("he", lb.Text);
        Assert.Equal("hello", lb.Feed("llo\r\n"));
        Assert.Equal("", lb.Text);
        Assert.Null(lb.Feed("ab\x7f"));
        Assert.Equal("a", lb.Text);
        Assert.Equal("\x03", lb.Feed("\x03"));
        Assert.Equal("", lb.Feed("\x04"));
        Assert.Null(lb.Feed("\x1b[A")); // arrow escape consumed
        Assert.Null(lb.Feed("x\x1bOP"));
        Assert.Equal("x", lb.Feed("\n"));
        lb.Clear();

        // MaxLength truncate path
        var full = new LineBuffer { MaxLength = 3 };
        full.Feed("abcd");
        Assert.True(full.Text.Length <= 3);

        Assert.Contains("error", ShellOutput.ErrorMsg("x"), StringComparison.Ordinal);
        Assert.Contains("ok", ShellOutput.SuccessMsg("ok"), StringComparison.Ordinal);
        Assert.Contains("info", ShellOutput.InfoMsg("info"), StringComparison.Ordinal);
        Assert.Contains("H", ShellOutput.Heading("H"), StringComparison.Ordinal);
        Assert.Contains("a=b", ShellOutput.FmtKv(new Dictionary<string, string> { ["a"] = "b" }), StringComparison.Ordinal);

        var disp = new CommandDispatcher();
        disp.Register("echo", args => new CommandResult { Output = string.Join(" ", args) });
        Assert.Contains("help", disp.Dispatch("help").Output, StringComparison.OrdinalIgnoreCase);
        Assert.Equal("hi", disp.Dispatch("echo hi").Output);
        Assert.False(disp.Dispatch("nope").Ok);
        Assert.Contains("2J", disp.Dispatch("clear").Output, StringComparison.Ordinal);
    }

    [Fact]
    public void Render_Sgr_And_CellGrid_And_Buffer()
    {
        var seg = new TextSegment { Text = "Hi", Fg = 1, Bg = 4, Bold = true, Underline = true, Reverse = true };
        var enc = Sgr.Encode(seg);
        Assert.Contains("Hi", enc, StringComparison.Ordinal);
        Assert.Contains("\x1b[", enc, StringComparison.Ordinal);
        Assert.Equal(enc, Sgr.EncodeMany([seg]));
        Assert.Equal("plain", Sgr.Encode(new TextSegment { Text = "plain" }));
        Assert.Equal("\x1b[0m", Sgr.Reset);

        var high = Sgr.Encode(new TextSegment { Text = "X", Fg = 196, Bg = 20 });
        Assert.Contains("38;5;196", high, StringComparison.Ordinal);

        var grid = new CellGrid(4, 2);
        grid.Put(0, 0, 'A');
        grid.Put(1, 0, 'B');
        var plain = grid.ToPlainText();
        Assert.StartsWith("AB", plain, StringComparison.Ordinal);
        grid.Clear();
        Assert.DoesNotContain("A", grid.ToPlainText(), StringComparison.Ordinal);

        var frames = ImageRender.ImageToAnsiFrames(new byte[] { 200, 10, 200, 10 }, 2, 2);
        Assert.NotEmpty(frames);

        var style = RBuf.DefaultStyle;
        Assert.Equal(style, style);
        var sgr = RBuf.StyleToSgr(new RBuf.Style { FG = "1", BG = "2", Bold = true });
        Assert.Contains("m", sgr, StringComparison.Ordinal);

        var emu = new TerminalEmulator(20, 5);
        emu.Process(Encoding.ASCII.GetBytes("\x1b[31mR\x1b[0m"));
        var rendered = RBuf.RenderScreenLines(emu.Screen, 20, 5);
        Assert.NotEmpty(rendered);
    }

    [Fact]
    public async Task Gateway_Telnet_And_Ssh_StartStop()
    {
        await using var telnet = new TelnetGateway();
        var accepted = new TaskCompletionSource();
        telnet.OnAccept = (client, _) =>
        {
            client.Dispose();
            accepted.TrySetResult();
            return Task.CompletedTask;
        };
        await telnet.StartAsync("127.0.0.1", 0);
        Assert.True(telnet.Port > 0);
        using (var c = new TcpClient())
        {
            await c.ConnectAsync(IPAddress.Loopback, telnet.Port);
            await accepted.Task.WaitAsync(TimeSpan.FromSeconds(2));
        }

        await telnet.StopAsync();

        await using var ssh = new SshGateway();
        await ssh.StartAsync("127.0.0.1", 0);
        Assert.True(ssh.Port > 0);
        await ssh.StopAsync();
    }

    [Fact]
    public void CanonicalJson_Types()
    {
        Assert.Equal("null", CanonicalJson.Serialize(null));
        Assert.Equal("true", CanonicalJson.Serialize(true));
        Assert.Equal("false", CanonicalJson.Serialize(false));
        Assert.Equal("1", CanonicalJson.Serialize(1));
        Assert.Equal("2", CanonicalJson.Serialize(2L));
        Assert.Equal("\"hi\"", CanonicalJson.Serialize("hi"));
        Assert.Equal("[1,2]", CanonicalJson.Serialize(new object[] { 1, 2 }));
        var map = CanonicalJson.Serialize(new Dictionary<string, object?> { ["b"] = 2, ["a"] = 1 });
        Assert.Equal("""{"a":1,"b":2}""", map);
        Assert.Contains(".", CanonicalJson.Serialize(1.5), StringComparison.Ordinal);
        Assert.Equal("\"x\"", CanonicalJson.Serialize("x"));
        // unicode escapes
        var uni = CanonicalJson.Serialize("\n\t\"\\");
        Assert.Contains("\\n", uni, StringComparison.Ordinal);
    }

    [Fact]
    public void ControlChannel_EncodeDecode_Mix()
    {
        var term = ControlChannelCodec.EncodeTerminalData("hello");
        Assert.Contains("hello", term, StringComparison.Ordinal);

        var ctrl = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "ping",
            ["ts"] = 1.0,
        });
        Assert.True(ControlChannelCodec.IsControlFrame(ctrl));

        var decoder = new ControlFrameDecoder(new DecoderOptions());
        var chunks = decoder.Feed(term + ctrl).ToList();
        chunks.AddRange(decoder.Finish());
        Assert.NotEmpty(chunks);
        Assert.Contains(chunks, c => c is DataChunk);
        Assert.Contains(chunks, c => c is ControlChunk);

        // Alias Decoder type if present
        var d2 = new Provide.Uterm.ControlChannel.Decoder();
        Assert.NotEmpty(d2.Feed("plain"));
    }

    [Fact]
    public void ServerAuth_ApiKeys_Roles_Principal()
    {
        var store = new ApiKeyStore();
        var (raw, record) = store.Create("user1", StringSet.Of("session.read"), expiresInS: 3600);
        Assert.False(string.IsNullOrEmpty(raw));
        Assert.Equal("user1", record.Name);
        Assert.NotNull(store.Validate(raw));
        Assert.Null(store.Validate("missing"));
        Assert.False(string.IsNullOrEmpty(ApiKeyStore.HashKey(raw)));

        var authz = new AuthorizationService();
        var p = new Principal
        {
            SubjectId = "u",
            Roles = StringSet.Of("viewer"),
            Scopes = StringSet.Of("session.read"),
        };
        Assert.True(authz.HasCapability(p, "session.read"));
        Assert.False(authz.IsAdmin(p));
        var admin = new Principal { SubjectId = "a", Roles = StringSet.Of("admin"), Scopes = StringSet.Of("*") };
        Assert.True(authz.IsAdmin(admin));
        Assert.True(authz.HasCapability(admin, "session.control.hijack"));

        var filtered = AuthRoles.FilterKnownRoles(new[] { "admin", "nope", "VIEWER" });
        Assert.True(filtered.Has("admin"));
        Assert.True(filtered.Has("viewer"));
    }


    [Fact]
    public void Expect_Session_Helpers()
    {
        var snap = new Snapshot { Screen = "ready>", Cols = 80, Rows = 25 };
        Assert.Equal(80, snap.Cols);
        var opts = new ExpectOptions { ExpectText = "ready", Timeout = TimeSpan.FromMilliseconds(10) };
        Assert.Equal("ready", opts.ExpectText);
        var pos = new CursorPos(1, 2);
        Assert.Equal(1, pos.X);
    }

    [Fact]
    public void Emulator_VtHeavySequences()
    {
        var emu = new TerminalEmulator(40, 12);
        // cursor movements, erase, scroll region, insert/delete, tabs, DEC modes, SGR 256/truecolor
        var seq = string.Concat(
            "start\r\n",
            "\x1b[2J\x1b[H",
            "\x1b[10;5H",
            "\x1b[A\x1b[B\x1b[C\x1b[D",
            "\x1b[1;10r",
            "\x1b[2K\x1b[K\x1b[1K",
            "\x1b[J\x1b[1J",
            "\x1b[3P\x1b[3@",
            "\x1b[3M\x1b[3L",
            "\x1b[?25h\x1b[?25l",
            "\x1b[?1049h\x1b[?1049l",
            "\x1b[38;5;196mX\x1b[48;5;21mY\x1b[0m",
            "\x1b[38;2;10;20;30mZ\x1b[0m",
            "\x1b(0\x1b)0",
            "\x1b[4h\x1b[4l",
            "line\r\nline2\r\n",
            "\x1b[H\x1b[2J",
            "DONE");
        emu.Process(Encoding.UTF8.GetBytes(seq));
        var snap = emu.GetSnapshot();
        Assert.Contains("DONE", snap.Screen, StringComparison.Ordinal);
        Assert.NotEmpty(emu.AnsiScreen());

        // raw tail trimming: feed more than 4k
        var big = Encoding.UTF8.GetBytes(new string('Z', 5000));
        emu.Process(big);
        Assert.True(Encoding.UTF8.GetByteCount(emu.RawTail) <= 4096 + 16);
    }

    [Fact]
    public void Auth_DevIdp_Setup()
    {
        var auth = new Provide.Uterm.ServerConfig.AuthConfig { Mode = "dev_token" };
        var path = Path.Combine(Path.GetTempPath(), "devtok-" + Guid.NewGuid().ToString("N"));
        try
        {
            var token = DevIdp.Setup(auth, new DevIdp.Options
            {
                TokenPath = path,
                Subject = "tester",
                Roles = new[] { "admin", "operator" },
            });
            Assert.False(string.IsNullOrEmpty(token));
            Assert.True(File.Exists(path));
            Assert.Contains(path, DevIdp.ResolvedTokenPath(path), StringComparison.Ordinal);
        }
        finally
        {
            if (File.Exists(path)) File.Delete(path);
        }
    }

    [Fact]
    public void DeckMux_Identity_Signed_And_NameFromSubject()
    {
        var secret = Encoding.UTF8.GetBytes("s3cret");
        var frame = Builders.MakeIdentity("user:bob",
            claims: new Dictionary<string, object?> { ["display"] = "Bob" },
            includeClaims: true,
            fingerprint: "fp",
            transport: "ssh",
            secret: secret);
        var id = Provide.Uterm.DeckMux.Identity.ParseIdentityFrame(frame!, secret);
        Assert.NotNull(id);
        Assert.Equal("user:bob", id!.Subject);

        // bad signature
        frame["signature"] = "00";
        Assert.Null(Provide.Uterm.DeckMux.Identity.ParseIdentityFrame(frame!, secret));

        // missing signature when required
        frame.Remove("signature");
        Assert.Null(Provide.Uterm.DeckMux.Identity.ParseIdentityFrame(frame!, secret));

        var bare = Provide.Uterm.DeckMux.Identity.ParseIdentityFrame(Builders.MakeIdentity("x")!);
        var presence = Provide.Uterm.DeckMux.Identity.PresenceFromIdentity(bare!, "c1", new HashSet<string> { "#ff0000" });
        Assert.False(string.IsNullOrEmpty(presence.Name));
        Assert.False(string.IsNullOrEmpty(presence.Color));
    }
}
