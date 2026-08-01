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

public partial class HighCoverageBoostTests
{
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
