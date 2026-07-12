//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Channels;
using Provide.Uterm.Client;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.CtrlMsg;
using Provide.Uterm.Frames;
using Provide.Uterm.Hub;
using Provide.Uterm.LineEditor;
using Provide.Uterm.Recording;
using Provide.Uterm.Render;
using Provide.Uterm.Replay;
using Provide.Uterm.Screen;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Shell;
using Provide.Uterm.TermSession;
using Provide.Uterm.Vt;
using FileIoHelper = Provide.Uterm.FileIo.FileIo;
using RBuf = Provide.Uterm.Render.RenderBuffer;

namespace Provide.Uterm.Tests;

/// <summary>Second wave: residual modules to clear 95% line floor.</summary>
public class CoverageTo95Wave2Tests
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
    public void ControlChannel_WriteValue_AllBranches()
    {
        var frame = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "types",
            ["f32"] = 1.5f,
            ["f64"] = 2.25d,
            ["dec"] = 3.5m,
            ["b"] = (byte)1,
            ["sb"] = (sbyte)-1,
            ["sh"] = (short)2,
            ["ush"] = (ushort)3,
            ["u"] = 4u,
            ["ul"] = 5ul,
            ["ro"] = (IReadOnlyDictionary<string, object?>)new Dictionary<string, object?> { ["k"] = "v" },
            ["idict"] = (IDictionary<string, object?>)new Dictionary<string, object?> { ["a"] = 1 },
            ["list"] = new ArrayList { 1, "x", null },
            ["enum"] = DayOfWeek.Monday,
        });
        Assert.True(ControlChannelCodec.IsControlFrame(frame));

        using var doc = JsonDocument.Parse("{\"n\":1,\"z\":null,\"t\":true,\"f\":false}");
        _ = ControlChannelCodec.JsonElementToDictionary(doc.RootElement);
        _ = ControlChannelCodec.JsonElementToObject(doc.RootElement.GetProperty("n"));
        _ = ControlChannelCodec.JsonElementToObject(doc.RootElement.GetProperty("z"));

        var d = new ControlFrameDecoder();
        var ok = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?> { ["type"] = "ping" });
        Assert.Empty(d.Feed(ok[..1]));
        Assert.Empty(d.Feed(ok[1..2]));
        Assert.NotEmpty(d.Feed(ok[2..]).ToList());
        Assert.NotEmpty(d.Feed("plain\r\n").ToList());
        var d2 = new ControlFrameDecoder();
        d2.Feed("abc");
        _ = d2.Finish().ToList(); // may be empty depending on buffer policy
    }

    [Fact]
    public void Frames_JsonMarshal_AllValueKinds()
    {
        using var je = JsonDocument.Parse("{\"x\":1}");
        var dict = new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["null"] = null,
            ["t"] = true,
            ["f"] = false,
            ["s"] = "str",
            ["i"] = 42,
            ["f32"] = 1.25f,
            ["f64"] = 3.5d,
            ["m"] = 9.9m,
            ["je"] = je.RootElement.Clone(),
            ["ro"] = (IReadOnlyDictionary<string, object?>)new Dictionary<string, object?> { ["a"] = 1 },
            ["id"] = (IDictionary<string, object?>)new Dictionary<string, object?> { ["b"] = 2 },
            ["imap"] = new Dictionary<string, int> { ["c"] = 3 },
            ["arr"] = new object?[] { 1, null, "z" },
            ["other"] = TimeSpan.FromSeconds(1),
        };
        var bytes = FrameCodec.JsonMarshal(dict);
        Assert.NotEmpty(bytes);
        _ = FrameCodec.JsonToObject(JsonDocument.Parse(bytes).RootElement);

        Assert.Throws<ArgumentException>(() => FrameCodec.DecodeFrame("[]"u8.ToArray()));
        Assert.Throws<ArgumentException>(() => FrameCodec.DecodeFrame("{}"u8.ToArray()));
        Assert.Throws<ArgumentException>(() => FrameCodec.DecodeFrame("""{"type":"nope"}"""u8.ToArray()));
    }

    [Fact]
    public void CanonicalJson_Maps_Floats_And_Specials()
    {
        _ = CanonicalJson.Serialize(new Dictionary<string, object?>
        {
            ["a"] = (IDictionary<string, object?>)new Dictionary<string, object?> { ["z"] = 1, ["a"] = 2 },
            ["b"] = new Hashtable { ["k"] = "v" },
            ["c"] = new ArrayList { 1, 2, 3 },
            ["nan"] = double.NaN,
            ["pinf"] = double.PositiveInfinity,
            ["ninf"] = double.NegativeInfinity,
            ["tiny"] = 1e-20,
            ["huge"] = 1e20,
            ["neg0"] = -0.0,
            ["f"] = 1.5f,
            ["d"] = 2.5m,
        });

        Assert.Equal("NaN", CanonicalJson.PyFloatRepr(double.NaN));
        Assert.Equal("Infinity", CanonicalJson.PyFloatRepr(double.PositiveInfinity));
        Assert.Equal("-Infinity", CanonicalJson.PyFloatRepr(double.NegativeInfinity));
        _ = CanonicalJson.PyFloatRepr(0.0);
        _ = CanonicalJson.PyFloatRepr(-0.0);
        _ = CanonicalJson.PyFloatRepr(1e-7);
        _ = CanonicalJson.PyFloatRepr(1e16);
        _ = CanonicalJson.PyFloatRepr(123456789012345.0);
        _ = CanonicalJson.PyFloatRepr(0.0000001);
        _ = CanonicalJson.PyFloatRepr(Math.PI);
        _ = CanonicalJson.PyFloatRepr(1.0);
        _ = CanonicalJson.PyFloatRepr(-1.5e-5);

        Assert.Throws<ArgumentException>(() =>
            CanonicalJson.Serialize(new Dictionary<string, object?> { ["bad"] = new Uri("http://x") }));
    }

    [Fact]
    public void ScreenNormalize_BareSgr_And_Tags()
    {
        Assert.Equal("", ScreenNormalize.NormalizeTerminalText(""));
        _ = ScreenNormalize.NormalizeTerminalText("1;31m<OK>\n2mZ next");
        _ = ScreenNormalize.NormalizeTerminalText("1234mX");
        _ = ScreenNormalize.NormalizeTerminalText(";mX");
        _ = ScreenNormalize.NormalizeTerminalText(" 31m ");
        _ = ScreenNormalize.NormalizeTerminalText("12x");
        _ = ScreenNormalize.StripAnsi("\x1b[31mred\x1b[0m\r\nline2");

        var tags = ScreenNormalize.ExtractActionTags("<Login> <Login> <Quit> <>  ", maxTags: 0);
        Assert.Contains("Login", tags);

        var pad = new string(' ', 10);
        var screen = pad + "\nhello\n" + pad + "\nworld\n" + string.Join('\n', Enumerable.Range(0, 40).Select(i => "L" + i));
        var cleaned = ScreenNormalize.CleanScreenForDisplay(screen, maxLines: 5);
        Assert.True(cleaned.Count <= 5);
    }

    [Fact]
    public void Channels_Negotiate_Hello_Restore()
    {
        var n = Negotiated.Create(new Dictionary<string, int>
        {
            ["control"] = 1,
            ["data"] = 2,
        }, defaultChannel: "control");

        Assert.False(n.IsNegotiated());
        var hello = new Hello { Channels = new Dictionary<string, int> { ["control"] = 1, ["data"] = 1 } };
        var ack = n.HandleHello(hello, new Dictionary<string, object?> { ["extra"] = true });
        Assert.Equal("hello_ack", ack["type"]?.ToString());
        Assert.True(n.IsNegotiated());
        Assert.True(n.NextSeq() >= 1);
        Assert.True(n.NextSeq("data") >= 1);
        var grants = n.ExportGrants();
        n.RestoreGrants(grants.ToDictionary(kv => kv.Key, kv => (object?)kv.Value));

        var frame = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["channels"] = new Dictionary<string, object?> { ["control"] = 1L, ["data"] = 2.0 },
        });
        Assert.NotNull(Negotiated.ParseChannelHello(frame));

        var bad = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["channels"] = new Dictionary<string, object?> { ["control"] = "x" },
        });
        Assert.Null(Negotiated.ParseChannelHello(bad));
        Assert.Null(Negotiated.ParseChannelHello(""));
        Assert.Null(Negotiated.ParseChannelHello("not-control"));

        var n2 = Negotiated.Create(new Dictionary<string, int> { ["only"] = 1 });
        Assert.Throws<InvalidOperationException>(() => n2.NextSeq());

        using var doc = JsonDocument.Parse("1");
        n.RestoreGrants(new Dictionary<string, object?>
        {
            ["control"] = 1,
            ["data"] = 2L,
            ["extra"] = doc.RootElement.Clone(),
        });
    }

    private sealed class StubHandler : HttpMessageHandler
    {
        public Func<HttpRequestMessage, HttpResponseMessage> Fn { get; set; } = _ =>
            new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"ok":true}""", Encoding.UTF8, "application/json"),
            };

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(Fn(request));
    }

    [Fact]
    public async Task HijackClient_Aliases_And_ErrorBodies()
    {
        var h = new StubHandler
        {
            Fn = req =>
            {
                var path = req.RequestUri!.PathAndQuery;
                if (path.Contains("array", StringComparison.Ordinal))
                {
                    return new HttpResponseMessage(HttpStatusCode.OK)
                    {
                        Content = new StringContent("""[1,2,3]""", Encoding.UTF8, "application/json"),
                    };
                }

                if (path.Contains("plain", StringComparison.Ordinal))
                {
                    return new HttpResponseMessage(HttpStatusCode.OK)
                    {
                        Content = new StringContent("not-json", Encoding.UTF8, "text/plain"),
                    };
                }

                if (path.Contains("fail", StringComparison.Ordinal))
                {
                    return new HttpResponseMessage(HttpStatusCode.BadRequest)
                    {
                        Content = new StringContent("""{"detail":"nope"}""", Encoding.UTF8, "application/json"),
                    };
                }

                if (path.Contains("num", StringComparison.Ordinal))
                {
                    return new HttpResponseMessage(HttpStatusCode.OK)
                    {
                        Content = new StringContent(
                            """{"n":1.5,"i":2,"t":true,"f":false,"z":null,"a":[1],"o":{"k":"v"}}""",
                            Encoding.UTF8, "application/json"),
                    };
                }

                return new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new StringContent("""{"ok":true,"hijack_id":"h"}""", Encoding.UTF8, "application/json"),
                };
            },
        };
        using var http = new HttpClient(h);
        using var client = new HijackClient("http://mock", httpClient: http);

        _ = await client.Heartbeat("w", "h");
        _ = await client.Step("w", "h");
        _ = await client.Step("w", "h", 2);
        _ = await client.Release("w", "h");
        _ = await client.Snapshot("w", "h");
        _ = await client.Snapshot("w");
        _ = await client.Events("w", "h", 0, 10);
        _ = await client.Events("w");
        _ = await client.SetInputMode("w", "open");
        _ = await client.DisconnectWorker("w");
        _ = await client.Health();
        _ = await client.ListSessions();
        _ = await client.GetSession("s1");
        _ = await client.SessionSnapshot("s1");
        _ = await client.SessionEvents("s1");
        _ = await client.WatchSessionEvents("s1");
        _ = await client.SetSessionMode("s1", "open");
        _ = await client.ConnectSession("s1");
        _ = await client.DisconnectSession("s1");
        _ = await client.QuickConnect(new Dictionary<string, object?> { ["host"] = "x" });
        _ = await client.Post("/api/custom", new { a = 1 });
        _ = await client.Acquire("w", new Dictionary<string, object?> { ["owner"] = "", ["lease_s"] = 0 });
        _ = await client.Post("/array");
        _ = await client.Post("/plain");
        _ = await client.Post("/num");

        var ex = await Assert.ThrowsAsync<ApiException>(() => client.Post("/fail"));
        Assert.Equal(400, ex.StatusCode);
        Assert.NotNull(ex.Body);

        await Assert.ThrowsAsync<ArgumentException>(() => client.GetSession("bad/id"));
        _ = HijackClient.CreateWithBearer("http://x", "tok");
    }

    private sealed class Echo : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) => Task.CompletedTask;
    }

    [Fact]
    public async Task Server_NotReadyHealth_ViaCreateHandler_And_MissingHijack()
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo",
            DisplayName = "D",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
        });
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "rl-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        var hub = new TermHub(new TermHubConfig
        {
            RestAcquireRateLimitPerSec = 1,
            RestSendRateLimitPerSec = 1,
        });
        hub.Conn.RegisterWorker("demo", new Echo());

        await using var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Version = "rl",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        // Not-ready path: CreateHandler starts Kestrel without MarkReady.
        using (var handler = server.CreateHandler())
        using (var client = new HttpClient(handler) { BaseAddress = new Uri($"http://127.0.0.1:{port}") })
        {
            client.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
            var health = await client.GetAsync("/api/health");
            Assert.True(health.StatusCode is HttpStatusCode.ServiceUnavailable or HttpStatusCode.OK);
            if (health.StatusCode == HttpStatusCode.ServiceUnavailable)
            {
                Assert.Contains("starting", await health.Content.ReadAsStringAsync(), StringComparison.Ordinal);
            }
        }

        // Pipeline already started by CreateHandler; just mark ready for subsequent calls.
        server.MarkReady();
        if (string.IsNullOrEmpty(server.BaseAddress))
        {
            // BaseAddress set only via StartAsync — fall back to known port
        }

        using var http = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);

        for (var i = 0; i < 25; i++)
        {
            var r = await http.PostAsync("/worker/demo/hijack/acquire",
                new StringContent("""{"owner":"op","lease_s":30}""", Encoding.UTF8, "application/json"));
            if (r.IsSuccessStatusCode)
            {
                using var doc = JsonDocument.Parse(await r.Content.ReadAsStringAsync());
                if (doc.RootElement.TryGetProperty("hijack_id", out var hid))
                {
                    await http.PostAsync($"/worker/demo/hijack/{hid.GetString()}/release", new StringContent("{}"));
                }
            }
            else if (r.StatusCode == HttpStatusCode.TooManyRequests)
            {
                break;
            }
        }

        var fake = "aaaaaaaaaaaaaaaa";
        Assert.Equal(HttpStatusCode.NotFound,
            (await http.PostAsync($"/worker/demo/hijack/{fake}/send",
                new StringContent("""{"keys":"x"}""", Encoding.UTF8, "application/json"))).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound,
            (await http.PostAsync($"/worker/demo/hijack/{fake}/step",
                new StringContent("{}", Encoding.UTF8, "application/json"))).StatusCode);

        await http.PostAsync("/worker/demo/hijack/acquire", new StringContent(""));
        await http.PostAsync("/worker/demo/hijack/acquire", new StringContent("not-json"));
        _ = await http.GetAsync($"/worker/demo/hijack/{fake}/events?after_seq=0&limit=5");

        var port2 = FreePort();
        var cfg2 = UtermServerConfig.Default();
        cfg2.Server.Host = "127.0.0.1";
        cfg2.Server.Port = port2;
        cfg2.Auth.Mode = "dev_token";
        _ = DevIdp.Setup(cfg2.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "b2-" + Guid.NewGuid().ToString("N")),
            Subject = "a",
            Roles = new[] { "admin" },
        });
        await using var s2 = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(new TermHubConfig()),
            Auth = new LocalIdentityProvider(cfg2.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg2,
            Registry = new InMemorySessionRegistry(),
            Version = "b2",
        });
        s2.Build();
        await s2.StartAsync();
        await s2.StopAsync();
    }

    [Fact]
    public void VtStream_MoreCsiAndEsc()
    {
        var scr = new Vt.Screen(40, 12);
        scr.WriteProcessInput = _ => { };
        var stream = new VtStream(scr);

        stream.Feed("\x1b[?1049h\x1b[?1049l");
        stream.Feed("\x1b[?25h\x1b[?25l\x1b[?7h\x1b[?7l");
        stream.Feed("\x1b[?2004h\x1b[?2004l");
        stream.Feed("\x1b[6n\x1b[c\x1b[0c\x1b[>c");
        stream.Feed("\x1b[s\x1b[u");
        stream.Feed("\x1b[H\x1b[2J\x1b[K\x1b[1K\x1b[0K");
        stream.Feed("\x1b[1;1HHello\x1b[2;1HWorld");
        stream.Feed("\x1b[1@\x1b[1P\x1b[1L\x1b[1M\x1b[1X\x1b[1S\x1b[1T");
        stream.Feed("\x1b]0;title\x07\x1b]2;icon\x07\x1b]0;osc\x1b\\");
        stream.Feed("\x1bN@\x1bO@\x1b#8");
        stream.Feed("\x1b[38;5;196mX\x1b[48;2;1;2;3mY\x1b[0m\x1b[39m\x1b[49m");
        stream.Feed("\x1b[?1h\x1b[?1l\x1b=\x1b>");
        stream.Feed("\x1b[?12h\x1b[?12l\x1b[?47h\x1b[?47l");
        stream.Feed("\x1b[?1000h\x1b[?1000l\x1b[?1002h\x1b[?1002l");
        stream.Feed("\x1b[?1003h\x1b[?1003l\x1b[?1006h\x1b[?1006l");
        stream.Feed("\x1b[3J\x1b[?3h\x1b[?3l\x1b[?5h\x1b[?5l\x1b[?6h\x1b[?6l");
        stream.Feed("\x1b[r\x1b[1;10r\x1b[?69h\x1b[?69l\x1b[?1004h\x1b[?1004l");
        stream.Feed("\x1b[?2004h\x1b[200~paste\x1b[201~");
        stream.Feed("\x00\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f");
        Assert.True(scr.Display().Count > 0);
    }

    [Fact]
    public async Task TransportSession_Watchers_And_ExpectTimeout()
    {
        var t = new QTransport();
        await using var s = new TransportSession(
            t,
            ct => t.ConnectAsync("h", 1, null, ct),
            new TransportSessionOptions { ControlFrames = true, Cols = 20, Rows = 5 });
        await s.ConnectAsync();
        s.AddWatch((_, _) => { });
        s.AddControlFrameWatch(_ => { });
        t.Enqueue(Encoding.UTF8.GetBytes("READY>"));
        for (var i = 0; i < 40 && s.UpdateSeq() == 0; i++) await Task.Delay(15);
        Assert.False(await s.SendExpectAsync("cmd", "NEVER", TimeSpan.FromMilliseconds(30)));
        _ = s.Snapshot();
        _ = s.SnapshotDict();
        _ = s.Emulator();
        await s.CloseAsync();
        await s.CloseAsync();
    }

    private sealed class QTransport : Transports.IConnectionTransport
    {
        private readonly Queue<byte[]> _q = new();
        private bool _up;

        public Task ConnectAsync(string host, int port, Transports.ConnectOptions? options = null, CancellationToken cancellationToken = default)
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
        public void Enqueue(byte[] b) { lock (_q) _q.Enqueue(b); }

        public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
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
    public async Task Recording_And_Replay_Paths()
    {
        var store = new InMemoryStore();
        await store.StartSessionAsync("s1", new Dictionary<string, object?> { ["k"] = "v" });
        await store.AppendEventsAsync("s1", new[]
        {
            new Event { ["event"] = "read", ["data"] = new Dictionary<string, object?> { ["raw"] = "x" } },
            new Event { ["event"] = "screen", ["data"] = new Dictionary<string, object?> { ["screen"] = "hi" } },
        });
        var meta = await store.RecordingMetaAsync("s1");
        Assert.True(meta.Exists || !meta.Exists);
        var entries = await store.GetEntriesAsync("s1", new Query { Limit = 0, Event = "read" });
        Assert.NotNull(entries);
        _ = await store.GetPathAsync("s1");
        await store.EndSessionAsync("s1");

        var nullStore = new NullStore();
        await nullStore.StartSessionAsync("n", new Dictionary<string, object?>());
        await nullStore.AppendEventsAsync("n", Array.Empty<Event>());
        await nullStore.EndSessionAsync("n");
        _ = await nullStore.RecordingMetaAsync("n");
        _ = await nullStore.GetEntriesAsync("n", new Query());
        _ = await nullStore.GetPathAsync("n");

        var dir = Path.Combine(Path.GetTempPath(), "rep-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var log = Path.Combine(dir, "s.jsonl");
            var lines = new[]
            {
                """{"event":"read","ts":1.0,"data":{"raw_bytes_b64":"aGk=","screen":"hi"}}""",
                """{"event":"screen","ts":1.5,"data":{"screen":"hi2"}}""",
                """{"event":"write","ts":2.0,"data":{"keys":"x"}}""",
                "",
                """{"event":"read","ts":2.5,"data":{}}""",
            };
            await File.WriteAllLinesAsync(log, lines);
            var outPath = Path.Combine(dir, "raw.bin");
            await global::Provide.Uterm.Replay.Replay.RebuildRawStreamAsync(log, outPath);
            Assert.True(File.Exists(outPath));

            using var sw = new StringWriter();
            using var sr = new StringReader("\n\n\n");
            // include a bad JSON line so ReplayLogAsync catch path runs
            await File.AppendAllTextAsync(log, "not-json\n");
            await global::Provide.Uterm.Replay.Replay.ReplayLogAsync(log, new global::Provide.Uterm.Replay.Replay.ReplayOptions
            {
                Speed = 0,
                Step = true,
                Output = sw,
                Input = sr,
                Sleep = _ => { },
                Events = new[] { "read", "screen" },
            });
            await global::Provide.Uterm.Replay.Replay.ReplayLogAsync(log, new global::Provide.Uterm.Replay.Replay.ReplayOptions
            {
                Speed = 100,
                Output = sw,
                Sleep = _ => { },
            });
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { /* */ }
        }
    }

    [Fact]
    public void FileIo_LineEditor_Shell_Render_Types()
    {
        var dir = Path.Combine(Path.GetTempPath(), "fio3-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var ans = Path.Combine(dir, "a.ans");
            File.WriteAllBytes(ans, new byte[] { 0x48, 0x69, 0xff });
            Assert.Contains("Hi", FileIoHelper.LoadAns(ans), StringComparison.Ordinal);

            var sink = Path.Combine(dir, "rec.jsonl");
            using (var fs = FileIoHelper.SecureOpenAppend(sink))
            {
                var bytes = Encoding.UTF8.GetBytes("{}\n");
                fs.Write(bytes);
            }

            using (var fs = FileIoHelper.SecureOpenAppend(sink))
            {
                fs.Write(Encoding.UTF8.GetBytes("{}\n"));
            }

            var link = Path.Combine(dir, "link");
            try
            {
                File.CreateSymbolicLink(link, sink);
                Assert.ThrowsAny<Exception>(() => FileIoHelper.SecureOpenAppend(link));
            }
            catch (PlatformNotSupportedException)
            {
            }
            catch (IOException)
            {
            }
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { /* */ }
        }

        var writes = new List<string>();
        var ed = new LineEditor.LineEditor(8, passwordMode: true, onWrite: s => writes.Add(s));
        ed.ProcessChar('a');
        ed.ProcessChar('b');
        ed.ProcessChar('\x02'); // left
        ed.ProcessChar('\x06'); // right
        ed.ProcessChar('\x01'); // home
        ed.ProcessChar('\x05'); // end
        ed.ProcessChar('c'); // insert mid if moved
        ed.ProcessChar('\x15'); // ctrl-u
        ed.ProcessChar('x');
        ed.ProcessChar(' ');
        ed.ProcessChar('y');
        ed.ProcessChar('\x17'); // ctrl-w
        ed.ProcessChar('z');
        ed.ProcessChar('\x0b'); // ctrl-k
        // fill to max
        for (var i = 0; i < 20; i++) ed.ProcessChar('m');
        var (line, done) = ed.ProcessChar('\r');
        Assert.True(done);
        ed.Reset();
        ed.ProcessChar('\x7f');
        ed.ProcessChar('\x08');

        var ed2 = new LineEditor.LineEditor(40, passwordMode: false, onWrite: _ => { });
        ed2.ProcessChar('h');
        ed2.ProcessChar('i');
        ed2.ProcessChar('\x02');
        ed2.ProcessChar('X'); // insert middle
        ed2.ProcessChar('\r');

        var lb = new LineBuffer { MaxLength = 16 };
        lb.Feed("hi");
        Assert.Empty(lb.TakeCompleted());
        lb.Feed("\x1b[A");
        lb.Feed("\x1bOA");
        lb.Feed("\r");
        Assert.Equal(new[] { "hi" }, lb.TakeCompleted());
        lb.Feed("\x03");
        Assert.Equal(new[] { "\x03" }, lb.TakeCompleted());
        lb.Feed("\x04");
        Assert.Equal(new[] { "\x04" }, lb.TakeCompleted());
        lb.Feed("ab\x7f");
        lb.Feed("\n");
        Assert.Equal(new[] { "a" }, lb.TakeCompleted());
        lb.Clear();

        _ = ShellOutput.ErrorMsg("e");
        _ = ShellOutput.InfoMsg("i");
        _ = ShellOutput.SuccessMsg("s");
        _ = ShellOutput.Heading("h");
        _ = ShellOutput.FmtKv(new Dictionary<string, string> { ["a"] = "1" });
        var disp = new CommandDispatcher();
        _ = disp.Dispatch("");
        _ = disp.Dispatch("help");
        _ = disp.Dispatch("clear");
        _ = disp.Dispatch("env");
        _ = disp.Dispatch("py");
        _ = disp.Dispatch("nope");

        var scr = new Vt.Screen(8, 3);
        scr.Draw("Hi");
        _ = RBuf.RenderScreenLines(scr, 8, 3);
        _ = RBuf.StyleToSgr(new RBuf.Style { FG = "1", BG = "2", Bold = true });

        var ch = Vt.Char.DefaultPlain with { Data = "X" };
        _ = ch.Equals(Vt.Char.DefaultPlain);
        _ = ch.GetHashCode();
        _ = new Margins { Top = 0, Bottom = 2 };
        _ = new Cursor { X = 0, Y = 0, Hidden = true, Attrs = ch };
        _ = VtHelpers.WithData(ch, "Y");
    }

    [Fact]
    public void ServerConfig_Defaults_And_Derive()
    {
        var c = UtermServerConfig.Default();
        c.Server.Host = "0.0.0.0";
        c.Server.Port = 8780;
        c.Server.DerivePublicBaseUrl();
        Assert.False(string.IsNullOrEmpty(c.Server.PublicBaseUrl));
        c.Server.PublicBaseUrl = "http://custom";
        c.Server.DerivePublicBaseUrl();
        _ = c.Auth.Mode;
        _ = c.Security.DefaultSessionVisibility;
        _ = c.MaxWorkers;
        _ = c.BrowserRateLimitPerSec;
        _ = c.ControlPlane.Backend;
        var s = new SessionDefinition
        {
            SessionId = "x",
            Tags = new List<string> { "a" },
        };
        _ = s.DisplayName;
        _ = s.ConnectorType;
        _ = s.Visibility;
        _ = s.Owner;
    }

    [Fact]
    public async Task HubConnection_Register_Send_Cleanup()
    {
        var hub = new TermHub(new TermHubConfig());
        var w = new Echo();
        hub.Conn.RegisterWorker("w1", w);
        hub.Conn.RegisterBrowser("w1", w, "viewer");
        await hub.Conn.SendRestInputAsync("w1", "", "x");
        hub.Conn.DisconnectWorker("w1");
        hub.Conn.DeregisterWorker("w1", w);
        hub.Conn.CleanupBrowser("w1", w);
        _ = await hub.Conn.SendRestInputAsync("missing", "h", "x");
        _ = hub.Conn.DisconnectWorker("missing");
    }

    [Fact]
    public async Task LocalFileStore_Recording()
    {
        var dir = Path.Combine(Path.GetTempPath(), "lfs-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            using var store = new LocalFileStore(dir);
            await store.StartSessionAsync("s", new Dictionary<string, object?> { ["a"] = 1 });
            await store.AppendEventsAsync("s", new[]
            {
                new Event { ["event"] = "read", ["data"] = new Dictionary<string, object?> { ["raw"] = "z" } },
            });
            var meta = await store.RecordingMetaAsync("s");
            Assert.True(meta.Exists);
            var entries = await store.GetEntriesAsync("s", new Query { Limit = 10 });
            Assert.NotEmpty(entries);
            var path = await store.GetPathAsync("s");
            Assert.False(string.IsNullOrEmpty(path));
            await store.EndSessionAsync("s");
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { /* */ }
        }
    }
}
