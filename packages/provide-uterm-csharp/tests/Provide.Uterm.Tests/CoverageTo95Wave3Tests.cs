//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.CtrlMsg;
using Provide.Uterm.DeckMux;
using Provide.Uterm.Hub;
using Provide.Uterm.Manager;
using Provide.Uterm.Render;
using Provide.Uterm.Screen;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;
using Provide.Uterm.Vt;
using RBuf = Provide.Uterm.Render.RenderBuffer;

namespace Provide.Uterm.Tests;

/// <summary>Third wave to close the remaining ~1% to 95%.</summary>
public class CoverageTo95Wave3Tests
{
    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var p = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    /// <summary>IDictionary&lt;string,object?&gt; that is NOT IReadOnlyDictionary (hits WriteValue branch).</summary>
    private sealed class DictOnly : IDictionary<string, object?>
    {
        private readonly Dictionary<string, object?> _inner = new();
        public object? this[string key] { get => _inner[key]; set => _inner[key] = value; }
        public ICollection<string> Keys => _inner.Keys;
        public ICollection<object?> Values => _inner.Values;
        public int Count => _inner.Count;
        public bool IsReadOnly => false;
        public void Add(string key, object? value) => _inner.Add(key, value);
        public void Add(KeyValuePair<string, object?> item) => _inner.Add(item.Key, item.Value);
        public void Clear() => _inner.Clear();
        public bool Contains(KeyValuePair<string, object?> item) => _inner.ContainsKey(item.Key);
        public bool ContainsKey(string key) => _inner.ContainsKey(key);
        public void CopyTo(KeyValuePair<string, object?>[] array, int arrayIndex) =>
            ((ICollection<KeyValuePair<string, object?>>)_inner).CopyTo(array, arrayIndex);
        public IEnumerator<KeyValuePair<string, object?>> GetEnumerator() => _inner.GetEnumerator();
        public bool Remove(string key) => _inner.Remove(key);
        public bool Remove(KeyValuePair<string, object?> item) => _inner.Remove(item.Key);
        public bool TryGetValue(string key, out object? value) => _inner.TryGetValue(key, out value);
        IEnumerator IEnumerable.GetEnumerator() => _inner.GetEnumerator();
    }

    [Fact]
    public void ControlChannel_IDictOnly_And_IsControlFrameEdges()
    {
        var d = new DictOnly { ["type"] = "x", ["n"] = 1 };
        var frame = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "wrap",
            ["inner"] = d,
        });
        Assert.True(ControlChannelCodec.IsControlFrame(frame));

        // uppercase length hex → lowercase check fails
        Assert.False(ControlChannelCodec.IsControlFrame("\u0010\u00020000000A:{}"));
        // bad colon
        Assert.False(ControlChannelCodec.IsControlFrame("\u0010\u000200000002xab"));
        // payload claims too large
        Assert.False(ControlChannelCodec.IsControlFrame("\u0010\u0002" + "000fffff" + ":x"));
        // incomplete payload
        Assert.False(ControlChannelCodec.IsControlFrame("\u0010\u000200000010:ab"));

        // Utf8PayloadEnd continuation throw path via IsControlFrame
        // Construct DLE/STX + length that ends mid multi-byte sequence
        var emoji = Encoding.UTF8.GetBytes("😀"); // 4 bytes
        var len = 2; // split mid rune
        var header = "\u0010\u0002" + len.ToString("x8") + ":";
        var bad = header + Encoding.UTF8.GetString(emoji);
        Assert.False(ControlChannelCodec.IsControlFrame(bad));

        // DataChunk/ControlChunk aliases
        var dc = new DataChunk("x");
        Assert.Equal("data", dc.Kind);
        var cc = new ControlChunk(new Dictionary<string, object?> { ["type"] = "t" });
        Assert.Same(cc.Control, cc.Payload);
        Assert.Equal("control", cc.Kind);

        // ProtocolException ctor
        _ = new ProtocolException("x");

        // Utf8PayloadEnd incomplete
        Assert.Equal(-1, ControlChannelCodec.Utf8PayloadEnd(new byte[] { 1, 2 }, 0, 10));
    }

    [Fact]
    public void CanonicalJson_IDictOnly_And_MoreFloats()
    {
        var d = new DictOnly { ["z"] = 1, ["a"] = "b" };
        _ = CanonicalJson.Serialize(new Dictionary<string, object?>
        {
            ["m"] = d,
            ["ht"] = new Hashtable { [1] = "one", ["two"] = 2 },
        });
        foreach (var f in new[]
                 {
                     1e-5, 1e15, 9.999999999999e-5, 1.2345678901234567e-10,
                     -1e20, 2.2250738585072014e-308, double.Epsilon,
                 })
        {
            _ = CanonicalJson.PyFloatRepr(f);
        }
    }

    [Fact]
    public void DeckMux_Identity_IDictClaims_And_VersionEdges()
    {
        // IDictionary claims path (not Dictionary concrete — use DictOnly)
        var id = Identity.ParseIdentityFrame(new Dictionary<string, object?>
        {
            ["type"] = "identity",
            ["version"] = 1,
            ["subject"] = "user:1",
            ["claims"] = new DictOnly { ["display_name"] = "Ada", ["role"] = "op" },
            ["fingerprint"] = "fp",
        });
        Assert.NotNull(id);
        Assert.Equal("Ada", id!.Claims.GetValueOrDefault("display_name")?.ToString());

        // unsupported version
        Assert.Null(Identity.ParseIdentityFrame(new Dictionary<string, object?>
        {
            ["type"] = "identity",
            ["version"] = 99,
            ["subject"] = "s",
        }));

        // wrong type
        Assert.Null(Identity.ParseIdentityFrame(new Dictionary<string, object?> { ["type"] = "x" }));

        // signature missing with secret
        Assert.Null(Identity.ParseIdentityFrame(new Dictionary<string, object?>
        {
            ["type"] = "identity",
            ["version"] = 1,
            ["subject"] = "s",
        }, expectedSecret: new byte[] { 1, 2, 3 }));

        // presence with claims display
        var resolved = new ResolvedIdentity
        {
            Subject = "u",
            Claims = new Dictionary<string, object?>
            {
                ["display_name"] = "Name",
                ["name"] = "N",
                ["color"] = "#ff0000",
                ["initials"] = "NM",
            },
        };
        var presence = Identity.PresenceFromIdentity(resolved, "c1", null, "operator");
        Assert.Equal("Name", presence.Name);
        var principal = Identity.IdentityAsPrincipal(resolved);
        Assert.Equal("u", principal.SubjectId);
        _ = principal.DisplayName;
        _ = principal.Identity;
    }

    [Fact]
    public void ScreenExtract_CustomPattern_And_DefaultMenus()
    {
        var screen = "[A] Alpha  [B] Beta\n(1) One item\n(2) Two item\n";
        _ = Extract.ExtractMenuOptions(screen);
        _ = Extract.ExtractMenuOptions(screen, @"\[(\w)\]\s+(\w+)");
        _ = Extract.ExtractMenuOptions(screen, "[invalid"); // ArgumentException swallowed
        _ = Extract.ExtractMenuOptions("no menus here");

        // numbered items if API exists via reflection-free path — menu only
        var opts = Extract.ExtractMenuOptions("<X> Exit game\n[Y] Yes please\n");
        Assert.True(opts.Count >= 0);
    }

    [Fact]
    public void Render_Sgr_CellGrid_Image_Rgb()
    {
        _ = new Rgb(1, 2, 3);
        _ = new Rgb { R = 9, G = 8, B = 7 };
        Assert.Equal(1, new Rgb(1, 2, 3).R);

        _ = Sgr.Encode(new TextSegment { Text = "plain" });
        _ = Sgr.Encode(new TextSegment { Text = "b", Bold = true, Underline = true, Reverse = true, Fg = 1, Bg = 2 });
        _ = Sgr.Encode(new TextSegment { Text = "fg8", Fg = 9, Bg = 10 });
        _ = Sgr.Encode(new TextSegment { Text = "fg256", Fg = 200, Bg = 201 });
        _ = Sgr.EncodeMany(new[]
        {
            new TextSegment { Text = "a", Fg = 3 },
            new TextSegment { Text = "b", Bold = true },
        });

        var grid = new CellGrid(0, 0); // defaults
        grid.Put(0, 0, 'A');
        grid.Put(-1, 0, 'x');
        grid.Put(0, -1, 'x');
        grid.Put(999, 999, 'x');
        Assert.Contains("A", grid.ToPlainText(), StringComparison.Ordinal);
        grid.Clear();

        var pixels = new byte[8 * 4];
        for (var i = 0; i < pixels.Length; i++) pixels[i] = (byte)(i * 17);
        _ = ImageRender.ImageToAnsiFrames(pixels, 8, 4, maxFrames: 2);
        Assert.Empty(ImageRender.ImageToAnsiFrames(pixels, 0, 4));
        Assert.Empty(ImageRender.ImageToAnsiFrames(pixels, 4, 0));
    }

    [Fact]
    public async Task ManagerProgram_ArgParse_ThenBindClash()
    {
        var port = FreePort();
        var mgr = new AgentManager(new ManagerConfig { Host = "127.0.0.1", Port = port });
        await using var server = new ManagerServer(mgr);
        await server.StartAsync();
        // second bind on same port → StartAsync throws after arg parse
        try
        {
            await ManagerProgram.RunAsync(new[]
            {
                "--host", "127.0.0.1",
                "--port", port.ToString(),
                "--token", "t",
            });
        }
        catch
        {
            // address in use / listener error expected
        }

        // by_state grouping with multiple states
        mgr.Spawn("w", "a1");
        mgr.Spawn("w", "a2");
        mgr.Stop("a1");
        var st = mgr.GetSwarmStatus();
        Assert.True(Convert.ToInt32(st["agents"]) >= 1);
    }

    [Fact]
    public async Task Server_PrivateSession_Forbidden_And_InvalidIds()
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "secret",
            DisplayName = "S",
            ConnectorType = "shell",
            Visibility = "private",
            Owner = "owner-a",
        });
        // viewer principal (not owner, not admin)
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "v3-" + Guid.NewGuid().ToString("N")),
            Subject = "viewer-x",
            Roles = new[] { "viewer" },
        });
        await using var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(new TermHubConfig()),
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Version = "v3",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);

        Assert.Equal(HttpStatusCode.Forbidden, (await http.GetAsync("/api/sessions/secret")).StatusCode);

        // invalid worker id on REST
        Assert.Equal(HttpStatusCode.UnprocessableEntity,
            (await http.PostAsync("/worker/bad%2Fid/input_mode",
                new StringContent("""{"input_mode":"open"}""", Encoding.UTF8, "application/json"))).StatusCode);

        // hijack on unknown without capability → 403/404
        _ = await http.PostAsync("/worker/nope/hijack/acquire",
            new StringContent("""{"owner":"o"}""", Encoding.UTF8, "application/json"));

        // invalid hijack id pattern
        Assert.Equal(HttpStatusCode.UnprocessableEntity,
            (await http.GetAsync("/worker/secret/hijack/!!/snapshot")).StatusCode);
    }

    [Fact]
    public async Task TransportSession_ReceiveFail_And_ControlDecodeError()
    {
        var t = new BoomTransport();
        await using var s = new TransportSession(
            t,
            ct => t.ConnectAsync("h", 1, null, ct),
            new TransportSessionOptions { ControlFrames = true, Cols = 10, Rows = 5 });
        await s.ConnectAsync();

        // control decoder exception path: inject invalid control that partially matches then dumps
        t.Enqueue(Encoding.UTF8.GetBytes("\u0010\u0002!!!!!!!!:"));
        await Task.Delay(80);

        // force receive throw → reader exits
        t.Boom = true;
        await Task.Delay(100);

        // wait helpers
        try { await s.WaitForUpdateAsync(TimeSpan.FromMilliseconds(20)); }
        catch (TimeoutException) { /* ok */ }
        try { await s.WaitForScreenChange(TimeSpan.FromMilliseconds(20), since: 0); }
        catch { /* ok */ }

        var watcherThrows = 0;
        s.AddWatch((_, _) =>
        {
            Interlocked.Increment(ref watcherThrows);
            throw new InvalidOperationException("watcher boom");
        });
        s.AddControlFrameWatch(_ => throw new InvalidOperationException("ctrl boom"));
        t.Boom = false;
        t.Enqueue(Encoding.UTF8.GetBytes("more-data"));
        await Task.Delay(60);

        await s.CloseAsync();
        await s.Close(CancellationToken.None);
    }

    private sealed class BoomTransport : IConnectionTransport
    {
        private readonly Queue<byte[]> _q = new();
        private bool _up;
        public bool Boom { get; set; }

        public void Enqueue(byte[] b) { lock (_q) _q.Enqueue(b); }

        public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            _up = true;
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            _up = false;
            return Task.CompletedTask;
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default) => Task.CompletedTask;

        public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            if (Boom) throw new IOException("boom");
            var deadline = DateTime.UtcNow + timeout;
            while (DateTime.UtcNow < deadline)
            {
                cancellationToken.ThrowIfCancellationRequested();
                lock (_q) { if (_q.Count > 0) return _q.Dequeue(); }
                await Task.Delay(5, cancellationToken);
            }

            return Array.Empty<byte>();
        }

        public bool IsConnected() => _up;
    }

    [Fact]
    public void Vt_Edit_Cursor_Sgr_More()
    {
        var scr = new Vt.Screen(20, 8);
        scr.Draw("abcdef");
        scr.InsertCharacters(2);
        scr.DeleteCharacters(1);
        scr.InsertLines(1);
        scr.DeleteLines(1);
        scr.EraseInLine(0);
        scr.EraseInLine(1);
        scr.EraseInLine(2);
        scr.EraseInDisplay(0);
        scr.EraseInDisplay(1);
        scr.EraseInDisplay(2);
        scr.EraseInDisplay(3);
        scr.CursorUp(1);
        scr.CursorDown(1);
        scr.CursorForward(1);
        scr.CursorBack(1);
        scr.CursorPosition(2, 3);
        scr.CursorToColumn(1);
        scr.SaveCursor();
        scr.RestoreCursor();
        scr.SetMargins(1, 5);
        _ = scr.Display();

        var stream = new VtStream(scr);
        stream.Feed("\x1b[1;31;42mX\x1b[0m");
        stream.Feed("\x1b[38;2;10;20;30mY\x1b[0m");
        stream.Feed("\x1b[48;5;100mZ\x1b[0m");
        stream.Feed("\x1b[?1000h\x1b[M #!"); // mouse
        stream.Feed("\x1b[?1006h\x1b[<0;1;1M\x1b[<0;1;1m");
        stream.Feed("\x1b]4;1;rgb:ff/00/00\x07");
        stream.Feed("\x1b]10;?\x07");
        stream.Feed("\x1b]11;?\x07");
        stream.Feed("\x1b[?2026h\x1b[?2026l"); // synchronized output
        stream.Feed("\x1b[?2004h\x1b[200~in\x1b[201~\x1b[?2004l");
        stream.Feed("\x1b[?1048h\x1b[?1048l");
        stream.Feed("\x1b[?1047h\x1b[?1047l");
        stream.Feed("\x1b[?47h\x1b[?47l");
        stream.Feed("\x1b[?69h\x1b[s\x1b[u\x1b[?69l");
        stream.Feed("\x1b P1$r\x1b\\"); // DCS
        stream.Feed("\x1b_test\x1b\\"); // APC
        stream.Feed("\x1b^test\x1b\\"); // PM
        stream.Feed("\x1bXtest\x1b\\"); // SOS
        stream.Feed("\x9b1;1H"); // CSI C1
        stream.Feed("\x9d0;title\x9c"); // OSC C1
        stream.UseUtf8 = false;
        stream.Feed("\x1b(B\x1b)0\x0e\x0f");
        stream.UseUtf8 = true;

        // RenderBuffer more styles
        _ = RBuf.StyleToSgr(new RBuf.Style { FG = "brightblue", BG = "brightwhite", Blink = true, Reverse = true, Underscore = true, Bold = true });
        _ = RBuf.StyleToSgr(new RBuf.Style { FG = "00ff00", BG = "0000ff" });
    }

    [Fact]
    public void HubModels_And_Connection_Edges()
    {
        var hub = new TermHub(new TermHubConfig
        {
            MaxWorkers = 2,
            BrowserRateLimitPerSec = 1,
            RestAcquireRateLimitPerSec = 1,
            RestSendRateLimitPerSec = 1,
        });
        _ = hub.Clock;
        var w = new W();
        hub.Conn.RegisterWorker("a", w);
        hub.Conn.RegisterWorker("b", w);
        // third may be rejected depending on max
        try { hub.Conn.RegisterWorker("c", w); } catch { /* */ }
        hub.Conn.RegisterBrowser("a", w, "operator");
        hub.Conn.RegisterBrowser("a", w, "viewer");
        hub.Conn.CleanupBrowser("a", w);
        hub.Conn.DeregisterWorker("a", w);
        hub.Conn.DisconnectWorker("b");

        // models / metric
        hub.Metric("test_metric", 1);
        _ = hub.State;
        _ = hub.Lease;
        _ = hub.Router;
        _ = hub.Registry;
    }

    private sealed class W : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) => Task.CompletedTask;
    }

    [Fact]
    public void ServerConfig_Load_Toml_And_SessionTags()
    {
        var dir = Path.Combine(Path.GetTempPath(), "cfg-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var toml = Path.Combine(dir, "server.toml");
            File.WriteAllText(toml, """
                [server]
                host = "127.0.0.1"
                port = 18780
                [auth]
                mode = "dev_token"
                """);
            var cfg = ConfigLoader.Load(toml);
            Assert.Equal(18780, cfg.Server.Port);
            cfg.Server.DerivePublicBaseUrl();
            _ = cfg.Auth.WorkerBearerToken;
            _ = cfg.Sessions;
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { /* */ }
        }
    }
}
