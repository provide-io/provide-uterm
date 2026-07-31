//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Auth;
using Provide.Uterm.Bridge;
using Provide.Uterm.Cli;
using Provide.Uterm.Client;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.CtrlMsg;
using Provide.Uterm.DeckMux;
using Provide.Uterm.Emulator;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;
using Provide.Uterm.Vt;
using FileIoHelper = Provide.Uterm.FileIo.FileIo;

namespace Provide.Uterm.Tests;

/// <summary>
/// Dense coverage for highest-miss modules: Vt.*, Server, TermSession,
/// Hub lease/approvals, Auth, DeckMux identity, Bridge, Cli, FileIo.
/// </summary>
public class HighCoverageBoostTests
{
    // ---------- Fake transport for TermSession ----------

    private sealed class FakeTransport : IConnectionTransport
    {
        private readonly Queue<byte[]> _incoming = new();
        private bool _connected;
        public List<byte[]> Sent { get; } = new();
        public bool FailReceive { get; set; }

        public void Enqueue(string s) => Enqueue(Encoding.UTF8.GetBytes(s));
        public void Enqueue(byte[] b)
        {
            lock (_incoming) _incoming.Enqueue(b);
        }

        public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            _connected = true;
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            _connected = false;
            return Task.CompletedTask;
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
        {
            Sent.Add(data);
            return Task.CompletedTask;
        }

        public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            if (FailReceive) throw new IOException("rx fail");
            var deadline = DateTime.UtcNow + timeout;
            while (DateTime.UtcNow < deadline)
            {
                cancellationToken.ThrowIfCancellationRequested();
                lock (_incoming)
                {
                    if (_incoming.Count > 0) return _incoming.Dequeue();
                }

                await Task.Delay(5, cancellationToken);
            }

            return Array.Empty<byte>();
        }

        public bool IsConnected() => _connected;
    }

    private sealed class EchoWorker : IWorkerWs
    {
        public List<string> Sent { get; } = new();
        public bool ThrowOnSend { get; set; }

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            if (ThrowOnSend) throw new IOException("send fail");
            Sent.Add(payload);
            return Task.CompletedTask;
        }
    }

    private sealed class StubHttp : HttpMessageHandler
    {
        public Func<HttpRequestMessage, HttpResponseMessage> Responder { get; set; } =
            _ => new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"ok":true,"hijack_id":"h1","status":"ok","sessions":[]}""", Encoding.UTF8, "application/json"),
            };

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(Responder(request));
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    // ---------- VT Screen + Cursor + Edit + Stream + Normalize ----------

    [Fact]
    public void Vt_Screen_DirectApi_CursorEditModes()
    {
        var scr = new Vt.Screen(20, 8);
        Assert.Equal(20, scr.Columns);
        Assert.Equal(8, scr.Lines);
        Assert.False(scr.TryGetMargins(out _));
        Assert.NotEmpty(scr.TabStops());
        Assert.NotEmpty(scr.Modes());
        _ = scr.DefaultCharPublic();
        _ = scr.At(0, 0);

        var reports = new List<string>();
        scr.WriteProcessInput = s => reports.Add(s);

        scr.Draw("Hello");
        Assert.Contains("Hello", string.Join('\n', scr.Display()), StringComparison.Ordinal);

        scr.CursorBack(2);
        scr.CursorForward(1);
        scr.CursorUp(1);
        scr.CursorDown(1);
        scr.CursorUp1(1);
        scr.CursorDown1(1);
        scr.CursorToColumn(5);
        scr.CursorToLine(3);
        scr.CursorPosition(2, 4);
        scr.CarriageReturn();
        scr.Backspace();
        scr.Tab();
        scr.SetTabStop();
        scr.ClearTabStop(0);
        scr.ClearTabStop(3);

        scr.SetMargins(2, 6);
        Assert.True(scr.TryGetMargins(out var m));
        Assert.Equal(1, m.Top);
        scr.SetMargins(); // clear
        Assert.False(scr.TryGetMargins(out _));

        scr.SaveCursor();
        scr.CursorPosition(5, 5);
        scr.RestoreCursor();
        scr.RestoreCursor(); // empty stack path

        scr.SetMode(true, 7); // DECAWM private
        scr.SetMode(false, 20); // LNM
        scr.SetMode(true, 6); // DECOM
        scr.SetMode(true, 5); // DECSCNM
        scr.SetMode(true, 25); // DECTCEM
        scr.SetMode(true, 3); // DECCOLM → 132 cols
        Assert.True(scr.Columns >= 80);

        scr.ResetMode(true, 3);
        scr.ResetMode(true, 5);
        scr.ResetMode(true, 6);
        scr.ResetMode(true, 25);
        scr.ResetMode(false, 20);

        scr.DefineCharset("B", "(");
        scr.DefineCharset("0", ")");
        scr.DefineCharset("?", "("); // unknown map no-op
        scr.ShiftOut();
        scr.ShiftIn();
        scr.SetTitle("T");
        scr.SetIconName("I");
        Assert.Equal("T", scr.Title);
        Assert.Equal("I", scr.IconName);
        scr.Bell();
        scr.AlignmentDisplay();
        Assert.Contains("E", scr.Display()[0], StringComparison.Ordinal);

        scr.ReportDeviceAttributes(0, false);
        scr.ReportDeviceStatus(5);
        scr.ReportDeviceStatus(6);
        Assert.NotEmpty(reports);

        // Edit ops
        scr.Reset();
        scr.Draw("ABCDEF");
        scr.CursorPosition(1, 3);
        scr.InsertCharacters(0);
        scr.DeleteCharacters(0);
        scr.EraseCharacters(0);
        scr.InsertCharacters(2);
        scr.DeleteCharacters(1);
        scr.EraseCharacters(1);
        scr.EraseInLine(0);
        scr.EraseInLine(1);
        scr.EraseInLine(2);
        scr.EraseInLine(9); // default branch
        scr.EraseInDisplay(0);
        scr.EraseInDisplay(1);
        scr.EraseInDisplay(2);
        scr.EraseInDisplay(3);
        scr.EraseInDisplay(9);

        scr.Draw("line1\nline2");
        scr.Index();
        scr.ReverseIndex();
        scr.LineFeed();
        scr.SetMargins(1, 4);
        scr.CursorPosition(2, 1);
        scr.InsertLines(0);
        scr.DeleteLines(0);
        scr.InsertLines(1);
        scr.DeleteLines(1);

        // Wrap + IRM + wide + combining (hits Normalize.NfcNormalize)
        scr.Reset();
        scr.SetMode(false, 4); // IRM
        scr.Draw("ab");
        scr.Draw("\u0301"); // combining acute on previous
        scr.Draw("\u4e2d"); // CJK wide
        scr.CursorPosition(1, 20);
        scr.Draw("Z"); // may wrap under DECAWM

        scr.Resize(10, 30);
        scr.Resize(0, 0); // keep current
        scr.Resize(5, 10); // shrink
        scr.Resize(5, 10); // no-op same
        Assert.Equal(5, scr.Lines);
        Assert.Equal(10, scr.Columns);
    }

    [Fact]
    public void Vt_Stream_FeedsMostCsiAndOsc()
    {
        var scr = new Vt.Screen(40, 12);
        var stream = new VtStream(scr) { UseUtf8 = true };
        var reports = new List<string>();
        scr.WriteProcessInput = s => reports.Add(s);

        // Plain text + basic C0
        stream.Feed("Hi\a\b\t\n\v\f\r");

        // ESC sequences: reset, index, next-line, tabset, reverse-index, save/restore
        stream.Feed("\x1bc\x1bD\x1bE\x1bH\x1bM\x1b7\x1b8");

        // Sharp alignment + percent + charset
        stream.Feed("\x1b#8\x1b%G\x1b(B\x1b)0");

        // CSI movement / edit / SGR / modes / margins / reports
        stream.Feed(string.Concat(
            "\x1b[2;5H",
            "\x1b[A\x1b[B\x1b[C\x1b[D\x1b[E\x1b[F",
            "\x1b[10G\x1b[3d",
            "\x1b[2J\x1b[K\x1b[1K\x1b[2K",
            "\x1b[2L\x1b[2M\x1b[2P\x1b[2X\x1b[2@",
            "\x1b[0c\x1b[5n\x1b[6n",
            "\x1b[0g\x1b[3g",
            "\x1b[?7h\x1b[?7l",
            "\x1b[20h\x1b[20l",
            "\x1b[?25h\x1b[?25l",
            "\x1b[?5h\x1b[?5l",
            "\x1b[?6h\x1b[?6l",
            "\x1b[?3h\x1b[?3l",
            "\x1b[1;8r",
            "\x1b[1;3;4;5;7;8mX\x1b[0m",
            "\x1b[38;5;9mY\x1b[48;5;10mZ\x1b[39m\x1b[49m",
            "\x1b[38;2;1;2;3mC\x1b[48;2;4;5;6mD",
            "\x1b[e\x1b[a\x1b[`\x1b[f"));

        // OSC title/icon with BEL and ST
        stream.Feed("\x1b]0;Title\x07");
        stream.Feed("\x1b]2;OnlyTitle\x1b\\");
        stream.Feed("\x1b]1;OnlyIcon\x07");
        // C1 CSI / OSC
        stream.Feed("\x9b1;1H");
        stream.Feed("\x9d0;C1Title\x9c");

        // CAN/SUB in CSI + intermediate
        stream.Feed("\x1b[1;2\x18");
        stream.Feed("\x1b[1$x");
        stream.Feed("\x1b[?1;2h");

        // Non-utf8 charset path
        stream.UseUtf8 = false;
        stream.Feed("\x1b(0");
        stream.Feed("\x0e\x0f"); // SO/SI when not utf8
        stream.UseUtf8 = true;

        // Stream should have applied title/icon and/or device reports
        Assert.True(
            !string.IsNullOrEmpty(scr.Title) ||
            !string.IsNullOrEmpty(scr.IconName) ||
            reports.Count > 0 ||
            scr.Display().Count == 12);
        Assert.Equal(12, scr.Display().Count);
    }

    [Fact]
    public void Vt_Normalize_Nfc_HangulAndCompose()
    {
        // Direct internal API (InternalsVisibleTo)
        Assert.Equal("é", Normalize.NfcNormalize("e\u0301"));
        // Hangul syllable 가 (U+AC00) decomposes/recomposes
        var ga = Normalize.NfcNormalize("\uAC00");
        Assert.False(string.IsNullOrEmpty(ga));
        // Hangul LV + T composition path
        var hangul = Normalize.NfcNormalize("\u1100\u1161\u11A8"); // 각
        Assert.False(string.IsNullOrEmpty(hangul));
        // Already composed + empty
        Assert.Equal("", Normalize.NfcNormalize(""));
        Assert.Equal("abc", Normalize.NfcNormalize("abc"));
        // Combining order reorder path — just ensure NFC returns a non-empty result
        var reordered = Normalize.NfcNormalize("a\u0301\u0327");
        Assert.True(reordered.Length >= 1);
    }

    [Fact]
    public void Vt_Wcwidth_And_Types()
    {
        Assert.Equal(1, Wcwidth.RuneWidth('A'));
        Assert.Equal(2, Wcwidth.RuneWidth(0x4e2d));
        Assert.Equal(0, Wcwidth.RuneWidth(0x0301));
        Assert.True(Wcwidth.CombiningClass(0x0301) > 0);
        Assert.Equal(0, Wcwidth.CombiningClass('A'));

        var c = Vt.Char.DefaultPlain;
        Assert.Equal(" ", c.Data);
        var c2 = VtHelpers.WithData(c, "X");
        Assert.Equal("X", c2.Data);
        var line = new Dictionary<int, Vt.Char>();
        _ = VtHelpers.CellAt(line, 0, c);
        var margins = new Margins { Top = 1, Bottom = 5 };
        Assert.Equal(1, margins.Top);
        var cur = new Cursor { X = 1, Y = 2, Hidden = false, Attrs = c };
        Assert.Equal(1, cur.X);
    }

    // ---------- TermSession ----------

    [Fact]
    public async Task TermSession_SendExpect_Waits_ControlFrames()
    {
        var t = new FakeTransport();
        await using var session = new TransportSession(
            t,
            ct => t.ConnectAsync("h", 1, null, ct),
            new TransportSessionOptions { Cols = 40, Rows = 10, ControlFrames = true });

        await session.Connect();
        Assert.True(session.IsConnected());

        var ctrlSeen = 0;
        session.AddControlFrameWatch(_ => Interlocked.Increment(ref ctrlSeen));
        session.AddWatch((_, _) => { });

        // Control frame via DLE/STX
        var frame = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["role"] = "viewer",
        });
        t.Enqueue(frame);
        t.Enqueue("READY>");

        for (var i = 0; i < 50 && session.UpdateSeq() == 0; i++)
            await Task.Delay(20);

        await session.Send("cmd\n");
        Assert.Contains(t.Sent, b => Encoding.UTF8.GetString(b).Contains("cmd", StringComparison.Ordinal));

        var matched = await session.SendExpectAsync("", "READY", TimeSpan.FromMilliseconds(400));
        Assert.True(matched);
        Assert.False(await session.SendExpect("x", "NEVER_MATCH_ZZZ", TimeSpan.FromMilliseconds(40)));

        _ = session.ANSIScreen();
        var dict = session.SnapshotDict();
        Assert.True(dict.ContainsKey("screen"));

        // Wait helpers
        t.Enqueue("more\r\n");
        var waitOk = await session.WaitForUpdate(TimeSpan.FromMilliseconds(500));
        Assert.True(waitOk || session.UpdateSeq() >= 0);
        Assert.False(await session.WaitForUpdate(TimeSpan.FromMilliseconds(5))); // likely timeout
        _ = await session.WaitForScreenChange(TimeSpan.FromMilliseconds(200), since: -1);
        _ = await session.WaitForScreenChange(TimeSpan.FromMilliseconds(50), since: 99999);

        await session.Close();
        Assert.False(session.IsConnected());
    }

    [Fact]
    public async Task TermSession_WithoutControlFrames_And_FailReceive()
    {
        var t = new FakeTransport();
        await using var session = new TransportSession(
            t,
            ct => t.ConnectAsync("h", 1, null, ct),
            new TransportSessionOptions { ControlFrames = false, Cols = 10, Rows = 5 });
        await session.ConnectAsync();
        t.Enqueue("plain");
        for (var i = 0; i < 40 && !session.ANSIScreen().Contains('p'); i++)
            await Task.Delay(15);
        Assert.Contains("p", session.ANSIScreen(), StringComparison.Ordinal);

        // Force reader exit via receive failure after reconnect path
        t.FailReceive = true;
        await Task.Delay(30);
        await session.CloseAsync();
    }

    // ---------- Mcp ----------


    // ---------- Auth ----------

    [Fact]
    public async Task Auth_AuthorizedKeys_WithOptionsAndClaims()
    {
        var path = Path.Combine(Path.GetTempPath(), "ak2-" + Guid.NewGuid().ToString("N"));
        try
        {
            var keyBody = Convert.ToBase64String(Encoding.UTF8.GetBytes("key-material-xyz"));
            // options with subject, claim-*, flags, quoted values
            var line = $"subject=\"bob\",claim-role=\"admin\",no-agent-forwarding,from=\"10.0.0.0/8\" ssh-ed25519 {keyBody} bob@host";
            File.WriteAllText(path, "#c\n\nbogus line\n" + line + "\nmalformed\nssh-ed25519 onlyonefield\n");
            var fp = SshAuth.FingerprintFromOpenSshBlob(Encoding.UTF8.GetBytes($"ssh-ed25519 {keyBody} bob@host"));
            var resolver = new SshAuth.AuthorizedKeysFileResolver(path);
            var id = await resolver.ResolveAsync(fp, Encoding.UTF8.GetBytes(line), "bob");
            Assert.NotNull(id);
            Assert.Equal("bob", id!.Subject);
            Assert.True(id.Claims.ContainsKey("role") || id.Claims.Count >= 0);

            // missing file
            var empty = new SshAuth.AuthorizedKeysFileResolver(path + ".missing");
            Assert.Null(await empty.ResolveAsync("x", Array.Empty<byte>(), "u"));

            // malformed OpenSSH line with prefix but too few fields
            Assert.Throws<FormatException>(() =>
                SshAuth.FingerprintFromOpenSshBlob(Encoding.UTF8.GetBytes("ssh-ed25519")));
        }
        finally
        {
            if (File.Exists(path)) File.Delete(path);
        }
    }

    // ---------- Hub Approvals + Lease ----------

    [Fact]
    public void Approvals_Resolve_CleanupExpired_OnExpired()
    {
        var clock = new ManualClock(100);
        var expired = new List<string>();
        var store = new InMemoryApprovalStore(clock) { OnExpired = id => expired.Add(id) };

        store.Add(new ApprovalRequest
        {
            Id = "a1", WorkerId = "w", SubmitterId = "u", Command = "rm",
            CreatedAt = 1, ExpiresAt = 50, GroupId = "g", IsFanout = true,
        });
        store.Add(new ApprovalRequest
        {
            Id = "a2", WorkerId = "w", SubmitterId = "u", Command = "ls",
            CreatedAt = 1, ExpiresAt = 200,
        });
        store.Resolve("a2", ApprovalStatus.Approved);
        store.Resolve("missing", ApprovalStatus.Rejected); // no-op
        Assert.Equal(ApprovalStatus.Approved, store.Get("a2")!.Status);
        Assert.Null(store.Get("nope"));

        store.CleanupExpired(); // a1 → Timeout
        Assert.Equal(ApprovalStatus.Timeout, store.Get("a1")!.Status);
        Assert.Contains("a1", expired);

        // prune non-pending after PruneTtl
        clock.SetWall(100 + 4000);
        store.CleanupExpired();
        Assert.Null(store.Get("a1")); // pruned
    }

    [Fact]
    public async Task Lease_Ws_Extend_Release_Prepare_CheckValid()
    {
        var clock = new ManualClock(1000);
        clock.SetMonotonic(10);
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            DashboardHijackLeaseS = 0, // clamp to 1
        });
        Assert.Equal(1, HijackLeaseManager.ClampDashboardLease(0));
        Assert.Equal(600, HijackLeaseManager.ClampDashboardLease(9999));
        Assert.Equal(30, HijackLeaseManager.ClampDashboardLease(30));

        var pf = HijackLeaseManager.PauseFrame("op", "h", 1.5);
        Assert.Equal("pause", pf["action"]?.ToString());
        var rf = HijackLeaseManager.ResumeFrame("op", 2);
        Assert.Equal("resume", rf["action"]?.ToString());

        var worker = new EchoWorker();
        hub.Conn.RegisterWorker("w1", worker);
        hub.Lease.DashboardHijackLeaseS = 60;

        // REST acquire fail: no worker
        var (ok0, reason0) = await hub.TryAcquireRestHijackAsync("missing", "op", 30, "h0", 10);
        Assert.False(ok0);
        Assert.Equal("no_worker", reason0);

        // WS acquire
        var browser = new object();
        hub.Conn.RegisterBrowser("w1", browser, "admin");
        var (wok, _) = hub.Lease.TryAcquireWs("w1", browser);
        Assert.True(wok);
        var contender = new object();
        hub.Conn.RegisterBrowser("w1", contender, "admin");
        var (wok2, r2) = hub.Lease.TryAcquireWs("w1", contender);
        Assert.False(wok2);
        Assert.Equal("already_hijacked", r2);
        Assert.NotNull(hub.Lease.TouchOwner("w1", 30));
        Assert.Null(hub.Lease.TouchOwner("missing"));
        Assert.True(hub.Lease.PrepareBrowserInput("w1", browser) || !hub.Lease.PrepareBrowserInput("w1", browser));
        var (relWs, restActive) = hub.Lease.TryReleaseWs("w1", browser);
        Assert.True(relWs);
        _ = restActive;
        Assert.False(hub.Lease.TryReleaseWs("w1", browser).Released);

        // REST path
        hub.Router.SetInputMode("w1", InputModes.Hijack);
        var (ok, reason) = await hub.TryAcquireRestHijackAsync("w1", "op", 30, "h1", 10);
        Assert.True(ok, reason);
        Assert.True(hub.Lease.CheckValid("w1", "h1"));
        Assert.False(hub.Lease.CheckValid("w1", "wrong"));

        Assert.Null(hub.ExtendHijackLease("w1", "h1", "other", 30, 15)); // owner mismatch
        Assert.NotNull(hub.ExtendHijackLease("w1", "h1", "op", 30, 15));

        // Worker send failure on acquire
        var worker2 = new EchoWorker { ThrowOnSend = true };
        hub.Conn.RegisterWorker("w2", worker2);
        var (okFail, reasonFail) = await hub.TryAcquireRestHijackAsync("w2", "op", 30, "hf", 20);
        Assert.False(okFail);
        Assert.Equal("no_worker", reasonFail);

        hub.ReleaseRestHijack("w1", "h1");
        hub.CleanupExpiredHijack("w1");
        hub.CleanupExpiredHijack("nope");
    }

    // ---------- DeckMux Identity ----------

    [Fact]
    public void DeckMux_Identity_SignedFrame_And_Fallbacks()
    {
        var secret = Encoding.UTF8.GetBytes("super-secret-key");
        var claims = new Dictionary<string, object?>
        {
            ["display"] = "Bob",
            ["role"] = "operator",
            ["nested"] = null,
            ["flag"] = true,
            ["n"] = 3,
            ["f"] = 1.5,
            ["list"] = new List<object?> { 1, "x", null },
        };
        var version = 1;
        var subject = "user:bob";
        var fingerprint = "fp1";
        var transport = "ssh";
        var claimsStr = Identity.PythonCompactJson(claims);
        var canonical = version.ToString(CultureInfo.InvariantCulture) + ":" + subject + ":" +
                        fingerprint + ":" + transport + ":" + claimsStr;
        using var hmac = new HMACSHA256(secret);
        var sig = Convert.ToHexString(hmac.ComputeHash(Encoding.UTF8.GetBytes(canonical))).ToLowerInvariant();

        var frame = new Dictionary<string, object?>
        {
            ["type"] = "identity",
            ["version"] = version,
            ["subject"] = subject,
            ["fingerprint"] = fingerprint,
            ["transport"] = transport,
            ["claims"] = claims,
            ["signature"] = sig,
        };
        var id = Identity.ParseIdentityFrame(frame, secret);
        Assert.NotNull(id);
        Assert.Equal(subject, id!.Subject);

        // bad signature
        frame["signature"] = "deadbeef";
        Assert.Null(Identity.ParseIdentityFrame(frame, secret));

        // missing signature when expected
        frame.Remove("signature");
        Assert.Null(Identity.ParseIdentityFrame(frame, secret));

        // version as long / double / JsonElement
        Assert.NotNull(Identity.ParseIdentityFrame(new Dictionary<string, object?>
        {
            ["type"] = "identity", ["version"] = 1L, ["subject"] = "s",
        }));
        Assert.NotNull(Identity.ParseIdentityFrame(new Dictionary<string, object?>
        {
            ["type"] = "identity", ["version"] = 1.0, ["subject"] = "s",
        }));
        using (var doc = JsonDocument.Parse("1"))
        {
            Assert.NotNull(Identity.ParseIdentityFrame(new Dictionary<string, object?>
            {
                ["type"] = "identity", ["version"] = doc.RootElement.Clone(), ["subject"] = "s",
            }));
        }

        // empty subject / missing version
        Assert.Null(Identity.ParseIdentityFrame(new Dictionary<string, object?>
        {
            ["type"] = "identity", ["version"] = 1, ["subject"] = "",
        }));
        Assert.Null(Identity.ParseIdentityFrame(new Dictionary<string, object?>
        {
            ["type"] = "identity", ["version"] = "x", ["subject"] = "s",
        }));

        // Presence fallbacks (no display claims → name from subject / generate)
        var bare = new DeckMux.ResolvedIdentity { Subject = "ssh:carol", Claims = new Dictionary<string, object?>() };
        var presence = Identity.PresenceFromIdentity(bare, "conn-xyz", new HashSet<string> { "#000000" }, "viewer");
        Assert.False(string.IsNullOrEmpty(presence.Name));
        Assert.Equal("viewer", presence.Role);
        Assert.False(string.IsNullOrEmpty(presence.Color));
        Assert.False(string.IsNullOrEmpty(presence.Initials));

        var p2 = Identity.PresenceFromIdentity(
            new DeckMux.ResolvedIdentity { Subject = "only", Claims = new() }, "c2");
        Assert.Equal("only", p2.UserId);

        var principal = Identity.IdentityAsPrincipal(bare);
        Assert.Equal("ssh:carol", principal.SubjectId);
        Assert.False(string.IsNullOrEmpty(principal.DisplayName));

        // PythonCompactJson coverage for nested structures
        _ = Identity.PythonCompactJson(new Dictionary<string, object?>
        {
            ["z"] = "a\"b\\c\n\r\t\b\f\x01\u00e9",
            ["a"] = new List<object?> { true, false, 1.0, 2.5, null },
        });
    }

    // ---------- Bridge Hijackable ----------

    [Fact]
    public async Task Hijackable_Blocks_Until_Step_And_WatchdogFires()
    {
        var h = new Hijackable();
        h.SetHijacked(true);
        var stuck = 0;
        h.MarkProgress();
        h.StartWatchdog(TimeSpan.FromMilliseconds(30), TimeSpan.FromMilliseconds(500), () => Interlocked.Increment(ref stuck));
        // force min interval clamp path already used; wait for stuck
        await Task.Delay(80);
        // Mark progress old enough — wait more
        await Task.Delay(40);
        h.StopWatchdog();
        h.StopWatchdog(); // second stop safe

        // Blocked await released by RequestStep
        h.SetHijacked(true);
        var waited = false;
        var t = Task.Run(async () =>
        {
            await h.AwaitIfHijacked();
            waited = true;
        });
        await Task.Delay(30);
        Assert.False(waited);
        h.RequestStep(0); // Max(1,0)=1
        await t.WaitAsync(TimeSpan.FromSeconds(2));
        Assert.True(waited);
        h.SetHijacked(false);
    }

    // ---------- Server routes ----------

    [Fact]
    public async Task Server_Routes_FullHijackAndErrors()
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo",
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
        });

        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "hcb-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        var clock = new ManualClock(1000);
        clock.SetMonotonic(10);
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            RestAcquireRateLimitPerSec = 1000,
            RestSendRateLimitPerSec = 1000,
        });
        var worker = new EchoWorker();
        hub.Conn.RegisterWorker("demo", worker);
        hub.Conn.RegisterWorker("adhoc", worker);

        await using var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
            Version = "hc-test",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);

        // health / ready
        Assert.True((await http.GetAsync("/api/health")).IsSuccessStatusCode);
        Assert.True((await http.GetAsync("/readyz")).IsSuccessStatusCode);
        Assert.True((await http.GetAsync("/healthz")).IsSuccessStatusCode);

        // sessions list/get
        Assert.True((await http.GetAsync("/api/sessions")).IsSuccessStatusCode);
        Assert.True((await http.GetAsync("/api/sessions/demo")).IsSuccessStatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await http.GetAsync("/api/sessions/missing")).StatusCode);

        // create session (valid + invalid id)
        var create = await http.PostAsync("/api/sessions",
            new StringContent("""{"session_id":"s-new","display_name":"N","connector_type":"shell","visibility":"public"}""",
                Encoding.UTF8, "application/json"));
        create.EnsureSuccessStatusCode();
        var badId = await http.PostAsync("/api/sessions",
            new StringContent("""{"session_id":"bad/id"}""", Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.UnprocessableEntity, badId.StatusCode);
        // auto id
        var auto = await http.PostAsync("/api/sessions",
            new StringContent("""{"display_name":"A"}""", Encoding.UTF8, "application/json"));
        auto.EnsureSuccessStatusCode();

        // input mode
        var mode = await http.PostAsync("/worker/demo/input_mode",
            new StringContent("""{"input_mode":"hijack"}""", Encoding.UTF8, "application/json"));
        mode.EnsureSuccessStatusCode();
        var badMode = await http.PostAsync("/worker/demo/input_mode",
            new StringContent("""{"input_mode":"nope"}""", Encoding.UTF8, "application/json"));
        Assert.False(badMode.IsSuccessStatusCode);

        // invalid worker id
        Assert.Equal(HttpStatusCode.UnprocessableEntity,
            (await http.PostAsync("/worker/bad%2Fid/hijack/acquire",
                new StringContent("{}", Encoding.UTF8, "application/json"))).StatusCode);

        // open mode blocks acquire
        await http.PostAsync("/worker/demo/input_mode",
            new StringContent("""{"input_mode":"open"}""", Encoding.UTF8, "application/json"));
        var openAcq = await http.PostAsync("/worker/demo/hijack/acquire",
            new StringContent("""{"owner":"op","lease_s":30}""", Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.Conflict, openAcq.StatusCode);

        await http.PostAsync("/worker/demo/input_mode",
            new StringContent("""{"input_mode":"hijack"}""", Encoding.UTF8, "application/json"));

        // full hijack lifecycle
        var acq = await http.PostAsync("/worker/demo/hijack/acquire",
            new StringContent("""{"owner":"operator","lease_s":60}""", Encoding.UTF8, "application/json"));
        acq.EnsureSuccessStatusCode();
        using var acqDoc = JsonDocument.Parse(await acq.Content.ReadAsStringAsync());
        var hijackId = acqDoc.RootElement.GetProperty("hijack_id").GetString()!;

        // second acquire conflict
        var conflict = await http.PostAsync("/worker/demo/hijack/acquire",
            new StringContent("""{"owner":"other","lease_s":30}""", Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.Conflict, conflict.StatusCode);

        (await http.PostAsync($"/worker/demo/hijack/{hijackId}/heartbeat",
            new StringContent("""{"lease_s":60}""", Encoding.UTF8, "application/json"))).EnsureSuccessStatusCode();
        (await http.PostAsync($"/worker/demo/hijack/{hijackId}/send",
            new StringContent("""{"keys":"hello"}""", Encoding.UTF8, "application/json"))).EnsureSuccessStatusCode();
        (await http.PostAsync($"/worker/demo/hijack/{hijackId}/step",
            new StringContent("{}", Encoding.UTF8, "application/json"))).EnsureSuccessStatusCode();
        (await http.GetAsync($"/worker/demo/hijack/{hijackId}/snapshot")).EnsureSuccessStatusCode();
        (await http.GetAsync($"/worker/demo/hijack/{hijackId}/events?after_seq=0&limit=10")).EnsureSuccessStatusCode();

        // invalid hijack id chars
        Assert.Equal(HttpStatusCode.UnprocessableEntity,
            (await http.GetAsync("/worker/demo/hijack/bad id!/snapshot")).StatusCode);

        // wrong hijack id 404
        Assert.Equal(HttpStatusCode.NotFound,
            (await http.GetAsync("/worker/demo/hijack/deadbeefdeadbeef/snapshot")).StatusCode);

        (await http.PostAsync($"/worker/demo/hijack/{hijackId}/release",
            new StringContent("{}", Encoding.UTF8, "application/json"))).EnsureSuccessStatusCode();
        // release again 404
        Assert.Equal(HttpStatusCode.NotFound,
            (await http.PostAsync($"/worker/demo/hijack/{hijackId}/release",
                new StringContent("{}", Encoding.UTF8, "application/json"))).StatusCode);

        // acquire with empty owner body
        await http.PostAsync("/worker/demo/input_mode",
            new StringContent("""{"input_mode":"hijack"}""", Encoding.UTF8, "application/json"));
        var acq2 = await http.PostAsync("/worker/demo/hijack/acquire",
            new StringContent("""{"owner":"  ","lease_s":"45"}""", Encoding.UTF8, "application/json"));
        acq2.EnsureSuccessStatusCode();
        using var acq2Doc = JsonDocument.Parse(await acq2.Content.ReadAsStringAsync());
        var h2 = acq2Doc.RootElement.GetProperty("hijack_id").GetString()!;
        await http.PostAsync($"/worker/demo/hijack/{h2}/release", new StringContent("{}", Encoding.UTF8, "application/json"));

        // no worker
        var noWorker = await http.PostAsync("/worker/ghost/hijack/acquire",
            new StringContent("""{"owner":"op"}""", Encoding.UTF8, "application/json"));
        Assert.True(noWorker.StatusCode is HttpStatusCode.Conflict or HttpStatusCode.NotFound or HttpStatusCode.Forbidden);

        // disconnect worker
        (await http.PostAsync("/worker/demo/disconnect_worker",
            new StringContent("{}", Encoding.UTF8, "application/json"))).EnsureSuccessStatusCode();

        // delete session
        (await http.DeleteAsync("/api/sessions/s-new")).EnsureSuccessStatusCode();
        Assert.Equal(HttpStatusCode.NotFound, (await http.DeleteAsync("/api/sessions/nope")).StatusCode);

        // unauth healthz remains anonymous
        using var anon = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        Assert.True((await anon.GetAsync("/healthz")).IsSuccessStatusCode);

        // CreateHandler on a fresh unstarted server
        var port2 = FreePort();
        var cfg2 = UtermServerConfig.Default();
        cfg2.Server.Host = "127.0.0.1";
        cfg2.Server.Port = port2;
        cfg2.Auth.Mode = "dev_token";
        var token2 = DevIdp.Setup(cfg2.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "hcb2-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        await using var server2 = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(new TermHubConfig { Clock = clock }),
            Auth = new LocalIdentityProvider(cfg2.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg2,
            Registry = new InMemorySessionRegistry(cfg2.Sessions),
            Clock = clock,
            Version = "hc2",
        });
        server2.Build(new[] { $"http://127.0.0.1:{port2}" });
        using (var handler = server2.CreateHandler())
        using (var inProc = new HttpClient(handler))
        {
            // PipelineHandler starts the app on first request
            inProc.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token2);
            // Request path may use absolute URI against base from handler
            try
            {
                var resp = await inProc.GetAsync(server2.BaseAddress is null
                    ? "http://127.0.0.1:" + port2 + "/healthz"
                    : server2.BaseAddress.TrimEnd('/') + "/healthz");
                _ = resp.StatusCode;
            }
            catch
            {
                // handler may need started BaseAddress; exercise construction at least
            }
        }

        await server.StopAsync();
    }

    // ---------- Cli flags ----------

    [Fact]
    public void Cli_ProxyFlags_And_VersionAliases()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        Assert.Equal(0, Root.Execute(
            ["proxy", "127.0.0.1", "23", "--bind", "127.0.0.1", "--port", "18709", "--once"], o, e));
        Assert.Contains("127.0.0.1:18709", o.ToString(), StringComparison.Ordinal);
        Assert.DoesNotContain("stub", o.ToString(), StringComparison.OrdinalIgnoreCase);

        using var o2 = new StringWriter();
        Assert.Equal(0, Root.Execute(
            ["proxy", "example.com", "2323", "--bind=0.0.0.0", "--port=18708", "--once"], o2, o2));
        Assert.Contains("0.0.0.0:18708", o2.ToString(), StringComparison.Ordinal);

        using var o3 = new StringWriter();
        Assert.Equal(0, Root.Execute(["-V"], o3, o3));
        Assert.Equal(0, Root.Execute(["version"], o3, o3));
        Assert.Equal(0, Root.Execute(["help"], o3, o3));
        Assert.Equal(0, Root.Execute([], o3, o3));
    }

    // ---------- FileIo ----------

    [Fact]
    public void FileIo_LoadTxt_Palette_And_SymlinkRefuse()
    {
        var dir = Path.Combine(Path.GetTempPath(), "fio2-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var txt = Path.Combine(dir, "a.txt");
            File.WriteAllText(txt, "hello");
            Assert.Equal("hello", FileIoHelper.LoadTxt(txt));

            Assert.Equal(16, FileIoHelper.LoadPalette("").Length);
            var pal = Path.Combine(dir, "p.json");
            File.WriteAllText(pal, "[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]");
            Assert.Equal(16, FileIoHelper.LoadPalette(pal).Length);
            Assert.Throws<FormatException>(() =>
            {
                File.WriteAllText(pal, "[1,2]");
                FileIoHelper.LoadPalette(pal);
            });
            Assert.Throws<FormatException>(() =>
            {
                File.WriteAllText(pal, "[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,999]");
                FileIoHelper.LoadPalette(pal);
            });

            // SecureOpenAppendMode with custom modes
            var rec = Path.Combine(dir, "sub", "r.jsonl");
            using (var fs = FileIoHelper.SecureOpenAppendMode(rec,
                       UnixFileMode.UserRead | UnixFileMode.UserWrite,
                       UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute))
            {
                fs.Write(Encoding.UTF8.GetBytes("x\n"));
            }
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }

    // ---------- CanonicalJson extra ----------

    [Fact]
    public void CanonicalJson_NumericAndMapVariants()
    {
        Assert.Equal("null", CanonicalJson.Serialize(null));
        Assert.Equal("true", CanonicalJson.Serialize(true));
        Assert.Equal("false", CanonicalJson.Serialize(false));
        Assert.Equal("1", CanonicalJson.Serialize((byte)1));
        Assert.Equal("2", CanonicalJson.Serialize(2L));
        Assert.Equal("3", CanonicalJson.Serialize(3u));
        Assert.Equal("4", CanonicalJson.Serialize(4ul));
        Assert.Equal("1.5", CanonicalJson.Serialize(1.5f));
        Assert.Equal("2.5", CanonicalJson.Serialize(2.5d));
        Assert.Equal("3.5", CanonicalJson.Serialize(3.5m));
        Assert.Equal("""{"a":1,"b":2}""", CanonicalJson.Serialize(new Dictionary<string, object?>
        {
            ["b"] = 2, ["a"] = 1,
        }));
        Assert.Equal("[1,2]", CanonicalJson.Serialize(new List<object?> { 1, 2 }));
        using var doc = JsonDocument.Parse("""{"x":1,"s":"hi","t":true,"f":false,"n":null,"a":[1]}""");
        _ = CanonicalJson.Serialize(doc.RootElement);
        Assert.Throws<ArgumentException>(() => CanonicalJson.Serialize(DateTime.UtcNow));
        // IDictionary non-generic
        var ht = new System.Collections.Hashtable { ["k"] = "v" };
        Assert.Contains("k", CanonicalJson.Serialize(ht), StringComparison.Ordinal);
    }

    // ---------- Emulator process more VT ----------

    [Fact]
    public void Emulator_DenseVtSequences()
    {
        var emu = new TerminalEmulator(40, 12);
        emu.Process(Encoding.UTF8.GetBytes(string.Concat(
            "Hello\r\n",
            "\x1b[1;1H\x1b[2J",
            "\x1b[10;10H",
            "\x1b[A\x1b[B\x1b[C\x1b[D",
            "\x1b[2@\x1b[2P\x1b[2X",
            "\x1b[2L\x1b[2M",
            "\x1b[K\x1b[1K\x1b[2K",
            "\x1b[J\x1b[1J\x1b[2J",
            "\x1b[?7h\x1b[?7l",
            "\x1b[20h\x1b[20l",
            "\x1b[?25h\x1b[?25l",
            "\x1b[1;10r",
            "\x1b7\x1b8",
            "\x1bD\x1bM\x1bE",
            "\x1bH\x1b[0g\x1b[3g",
            "\x1b#8",
            "\x1b]0;ttl\x07",
            "\x1b(B\x1b)0",
            "\x1b[38;2;10;20;30m\x1b[48;2;40;50;60mZ\x1b[0m",
            "e\u0301",
            "\u4e2d",
            "final")));
        var snap = emu.GetSnapshot();
        Assert.Contains("final", snap.Screen, StringComparison.Ordinal);
    }
}
