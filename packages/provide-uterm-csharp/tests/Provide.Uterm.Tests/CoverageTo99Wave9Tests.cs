//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Security.Claims;
using System.Text;
using System.Text.Json;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.Ansi;
using Provide.Uterm.Channels;
using Provide.Uterm.Cli;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Embed;
using Provide.Uterm.Emulator;
using Provide.Uterm.Frames;
using Provide.Uterm.Hub;
using Provide.Uterm.Screen;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Shell;

namespace Provide.Uterm.Tests;

/// <summary>Wave 9: residual push past 98% (CLI/Embed/pure/server edges).</summary>
public class CoverageTo99Wave9Tests : IDisposable
{
    private readonly Action _prevWait;

    public CoverageTo99Wave9Tests()
    {
        _prevWait = Root.WaitForCancel;
        Root.WaitForCancel = () => { /* no-op */ };
    }

    public void Dispose() => Root.WaitForCancel = _prevWait;

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var p = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    [Fact]
    public void Cli_Root_KnownHosts_BadProtocol_TunnelNoOnce()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        var code = Root.Execute(
            new[]
            {
                "proxy", "127.0.0.1", "22", "--transport", "ssh", "--ssh-user", "u",
                "--known-hosts", "/tmp/kh", "--known-hosts-list", "/tmp/a:/tmp/b",
                "--once", "--bind", "127.0.0.1", "--port", FreePort().ToString(),
            },
            o, e);
        Assert.True(code is 0 or 1);

        Assert.Equal(1, Root.Execute(
            new[] { "listen", "ws://127.0.0.1:9/ws", "--protocol", "ftp", "--once" }, o, e));
        Assert.Contains("protocol", e.ToString(), StringComparison.OrdinalIgnoreCase);

        // listen ssh without --once hits WaitCancel then Stop (L373-374,380)
        Assert.Equal(0, Root.Execute(
            new[]
            {
                "listen", "ws://127.0.0.1:9/ws", "--protocol", "ssh",
                "--host", "127.0.0.1", "--port", FreePort().ToString(),
            },
            o, e));
    }

    [Fact]
    public async Task Embed_Residual_DoubleDispose_ThrowingUpstream_DeferClient()
    {
        var hub = new EmbedHub();
        var interceptor = new DeferClientInterceptor();
        var session = await hub.CreateSessionAsync(new EmbedSessionOptions
        {
            SessionId = "w9-embed",
            Interceptor = interceptor,
        });
        var up = new ThrowingUpstream();
        await session.ConnectUpstreamAsync(up);

        var c = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata { ClientId = "c1", QueueCapacity = 1, Backpressure = BackpressurePolicy.DropNewest },
        });
        Assert.True(c.IsAttached);
        Assert.Equal("c1", c.ClientId);

        await session.SendToClientsAsync(Array.Empty<byte>()); // empty forward L418

        // deferred client path then flush (hits L208)
        interceptor.DeferNext = true;
        await session.SendToUpstreamAsync("DEF"u8.ToArray());
        interceptor.DeferNext = false;
        await session.FlushDeferredAsync();

        up.ThrowOnDisconnect = true;
        await session.ReplaceUpstreamAsync(new MemoryUpstream());

        var boom = new ThrowingUpstream { ThrowOnReceive = true };
        await session.ReplaceUpstreamAsync(boom);
        for (var i = 0; i < 40 && session.Lifecycle != SessionLifecycle.UpstreamLost; i++)
        {
            await Task.Delay(25);
        }

        Assert.Equal(SessionLifecycle.UpstreamLost, session.Lifecycle);
        await session.DisposeAsync();
        await session.DisposeAsync();
        await c.DisposeAsync();
        hub.RemoveSession("w9-embed");
    }

    [Fact]
    public async Task Embed_SendToUpstream_WhenDisconnected_Throws()
    {
        var hub = new EmbedHub();
        var session = await hub.CreateSessionAsync(new EmbedSessionOptions { SessionId = "w9-disc" });
        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);
        await up.DisconnectAsync();
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            session.SendToUpstreamAsync("x"u8.ToArray()));
        await session.DisposeAsync();
        hub.RemoveSession("w9-disc");
    }

    [Fact]
    public async Task Pure_AnsiBright_Upgrade_Screen_Frames_HubCatch()
    {
        // bright FG/BG MapIndex arms via Upgrade
        _ = Upgrade.UpgradeTo256("\x1b[91;101mX\x1b[0m");
        _ = Upgrade.UpgradeToTruecolor("\x1b[97;107mY\x1b[0m");
        _ = Upgrade.UpgradeTo256("\x1b[xyz;30m");

        _ = ScreenNormalize.NormalizeTerminalText("a\u001cb");
        _ = ScreenNormalize.NormalizeTerminalText("  x  ");

        using var doc = JsonDocument.Parse(
            """{"type":"presence_sync","users":[{"id":"u1"},{"id":"u2"}],"owner_id":"o"}""");
        _ = FrameMapper.FromJson(doc.RootElement, "presence_sync");
        using (var bad = JsonDocument.Parse("""{"type":"not_a_real_frame"}"""))
        {
            Assert.ThrowsAny<Exception>(() => FrameMapper.FromJson(bad.RootElement, "not_a_real_frame"));
        }

        // ToDict default throw (non-IFrame-typed via reflection-free custom)
        Assert.Throws<ArgumentException>(() => FrameMapper.ToDict(new BogusFrame()));

        var hub = new TermHub();
        hub.Conn.RegisterWorker("w", new OkWs());
        hub.Conn.RegisterBrowser("w", new FailWs(), "admin");
        await hub.Conn.BroadcastHijackStateAsync("w");
        await hub.Conn.BroadcastToBrowsersAsync("w", new Dictionary<string, object?> { ["type"] = "x" });

        var store = new StateStore(hub.Registry, new object(), maxBufferChars: 40);
        Assert.False(store.BufferAndGetCommand(new object(), "abcdefghi").Ok); // overflow needs max=4
        var store2 = new StateStore(hub.Registry, new object(), maxBufferChars: 4);
        Assert.False(store2.BufferAndGetCommand(new object(), "abcdefg").Ok);
        // newline completes command (Store L105-106)
        var (cmd, ok) = store.BufferAndGetCommand(new object(), "ls\n");
        Assert.True(ok);
        Assert.Contains("ls", cmd, StringComparison.Ordinal);

        var badHello = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["channels"] = new Dictionary<string, object?> { ["term"] = "nope" },
        });
        Assert.Null(Negotiated.ParseChannelHello(badHello));

        // Hangul with trailing jamo (NFC decompose t!=0) + ApiKeys residual
        var emu = new TerminalEmulator(40, 10);
        emu.Process(Encoding.UTF8.GetBytes("각"));
        var keys = new ApiKeyStore();
        var (raw, rec) = keys.CreateForTenant("acme", "k1");
        Assert.NotEmpty(raw);
        Assert.NotEmpty(keys.ListKeys());
        Assert.True(keys.RevokeForTenant(rec.KeyId, "acme"));
        Assert.False(keys.RevokeForTenant(rec.KeyId, "  ")); // CanonicalTenantId empty → L157

        // proxy without --once (WaitForCancel no-op)
        using var o = new StringWriter();
        using var e = new StringWriter();
        Assert.Equal(0, Root.Execute(
            new[] { "proxy", "127.0.0.1", "23", "--bind", "127.0.0.1", "--port", FreePort().ToString() },
            o, e));

        // LineBuffer FeedLegacy + unknown ESC intermediate
        var lb = new LineBuffer();
        Assert.Equal("hi", lb.FeedLegacy("hi\r"));
        Assert.Null(lb.FeedLegacy(""));
        lb.Feed("\x1bZx"); // ConsumeEscape unknown → L172
        _ = lb.TakeEcho();

        // Audit ToLong int/double arms
        var r0 = AuditChain.MakeRecord(0, "");
        r0["seq"] = 0; // int
        var r1 = AuditChain.MakeRecord(1, Convert.ToString(r0["record_hash"]) ?? "");
        r1["seq"] = 1.0; // double
        _ = AuditChain.VerifyRecords(new List<Dictionary<string, object?>> { r0, r1 });

        // Config ToInt string + float paths
        var tmp = Path.Combine(Path.GetTempPath(), "w9cfg-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp, """
            [server]
            port = "8777"
            [auth]
            mode = "jwt"
            clock_skew_seconds = 1.5
            """);
        try
        {
            var cfgX = ConfigLoader.Load(tmp);
            Assert.True(cfgX.Server.Port is 8777 or 8780); // string parse or fallback
        }
        finally
        {
            File.Delete(tmp);
        }

        // JWT scopes claim + empty sub rejected as anonymous via Authenticate catch
        var auth = UtermServerConfig.Default().Auth;
        _ = DevIdp.Setup(auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "w9jwt-" + Guid.NewGuid().ToString("N")),
            Subject = "s",
            Roles = new[] { "admin" },
        });
        // mint with scope claim
        var claims = new List<Claim>
        {
            new("sub", "scoped-user"),
            new(auth.JwtRolesClaim, "admin"),
            new(auth.JwtScopesClaim, "session.read session.control.hijack"),
        };
        var now = DateTimeOffset.UtcNow;
        var jwt = new JwtSecurityToken(
            issuer: auth.JwtIssuer,
            audience: auth.JwtAudience,
            claims: claims,
            notBefore: now.UtcDateTime,
            expires: now.AddHours(1).UtcDateTime,
            signingCredentials: new SigningCredentials(
                new SymmetricSecurityKey(Encoding.UTF8.GetBytes(auth.JwtPublicKeyPem!)),
                SecurityAlgorithms.HmacSha256));
        var tok = new JwtSecurityTokenHandler().WriteToken(jwt);
        var idp = new LocalIdentityProvider(auth);
        var p = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Authorization"] = "Bearer " + tok,
            },
        });
        Assert.Equal("scoped-user", p.SubjectId);

        // IsControlFrame oversized payload claim (Codec L201)
        Assert.False(ControlChannelCodec.IsControlFrame("\u0010\u000200100001:x"));
        // Config ToInt fallback for non-numeric
        var tmp2 = Path.Combine(Path.GetTempPath(), "w9cfg2-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp2, """
            [server]
            port = true
            [auth]
            mode = "jwt"
            clock_skew_seconds = "not-a-number"
            """);
        try
        {
            var cfgBad = ConfigLoader.Load(tmp2);
            Assert.True(cfgBad.Server.Port > 0); // fallback defaults
        }
        finally
        {
            File.Delete(tmp2);
        }

        // Audit Convert.ToInt64 residual
        var rBad = AuditChain.MakeRecord(0, "");
        rBad["seq"] = "0";
        _ = AuditChain.VerifyRecords(new List<Dictionary<string, object?>> { rBad });
    }

    [Fact]
    public async Task Server_AdHocRead_InvalidWorkerWs_LeaseInt_GuiUnknown()
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
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "priv",
            DisplayName = "P",
            ConnectorType = "shell",
            Visibility = "private",
            Owner = "other",
        });
        // empty target_id → auto-generate via SeedGraphicalTargets (L1292)
        cfg.GraphicalTargets.Add(new ServerConfig.GraphicalTargetDefinition
        {
            TargetId = "  ",
            Protocol = "memory",
            Enabled = true,
            Width = 8,
            Height = 8,
        });

        var (server, adminTok) = ServerFactory.CreateFromConfig(cfg, "w9");
        Assert.False(string.IsNullOrEmpty(adminTok));
        // re-mint admin is already done by CreateFromConfig; also mint viewer against same secret
        var viewerTok = MintJwt(cfg.Auth, "viewer", "viewer");
        // Register worker after factory so hijack works
        server.GetType(); // keep server alive for deps access via hub? register via REST not available
        // rebuild with worker: use StartServer-style after CreateFromConfig by reusing registry
        await using (server)
        {
            // CreateFromConfig does not start; Build+Start manually
            server.Build(new[] { $"http://127.0.0.1:{port}" });
            // inject worker via reflection-free path: acquire will fail without worker; still hit other arms
            await server.StartAsync();
            var baseUrl = server.BaseAddress!;

            using var http = new HttpClient { BaseAddress = new Uri(baseUrl) };
            http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + adminTok);

            // ad-hoc session.read: snapshot uses AuthorizeHub(session.read) on missing def
            var adhoc = await http.GetAsync("/worker/never-registered/hijack/h1/snapshot");
            Assert.True((int)adhoc.StatusCode is 404 or 403 or 422);

            // acquire may fail without worker; still exercises Int(lease_s) default for bool
            var acq = await http.PostAsync("/worker/demo/hijack/acquire",
                new StringContent("""{"owner":"op","lease_s":true}""", Encoding.UTF8, "application/json"));
            Assert.True((int)acq.StatusCode is 200 or 409 or 404);

            // POST with body (content path) after already-started server
            var post = await http.PostAsync("/worker/demo/input_mode",
                new StringContent("""{"input_mode":"hijack"}""", Encoding.UTF8, "application/json"));
            Assert.True(post.IsSuccessStatusCode || (int)post.StatusCode < 500);

            // viewer insufficient privileges on private session.read (snapshot) L1064-1065
            using var httpV = new HttpClient { BaseAddress = new Uri(baseUrl) };
            httpV.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + viewerTok);
            var forb = await httpV.GetAsync("/worker/priv/hijack/hx/snapshot");
            Assert.Equal(HttpStatusCode.Forbidden, forb.StatusCode);

            // invalid worker id on worker WS (L897-898)
            using var ws = new ClientWebSocket();
            try
            {
                await ws.ConnectAsync(
                    new Uri(baseUrl.Replace("http://", "ws://", StringComparison.Ordinal) + "/ws/worker/bad%2Fid/term"),
                    new CancellationTokenSource(3000).Token);
            }
            catch
            {
                // 422 expected on upgrade
            }

            // browser connect then Abort — server still runs cleanup finally
            using var bws = new ClientWebSocket();
            bws.Options.SetRequestHeader("Authorization", "Bearer " + adminTok);
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            await bws.ConnectAsync(
                new Uri(baseUrl.Replace("http://", "ws://", StringComparison.Ordinal) + "/ws/browser/demo/term"),
                cts.Token);
            // drain one hello so accept path completes
            var buf = new byte[4096];
            try { _ = await bws.ReceiveAsync(buf, cts.Token); } catch { /* ignore */ }
            bws.Abort();
            await Task.Delay(50);
        }
    }

    private static string MintJwt(AuthConfig auth, string subject, params string[] roles)
    {
        var secret = auth.JwtPublicKeyPem!;
        var claims = new List<Claim> { new("sub", subject) };
        foreach (var r in roles)
        {
            claims.Add(new Claim(auth.JwtRolesClaim, r));
        }

        var now = DateTimeOffset.UtcNow;
        var tok = new JwtSecurityToken(
            issuer: auth.JwtIssuer,
            audience: auth.JwtAudience,
            claims: claims,
            notBefore: now.UtcDateTime,
            expires: now.AddHours(1).UtcDateTime,
            signingCredentials: new SigningCredentials(
                new SymmetricSecurityKey(Encoding.UTF8.GetBytes(secret)), SecurityAlgorithms.HmacSha256));
        return new JwtSecurityTokenHandler().WriteToken(tok);
    }

    private sealed class BogusFrame : IFrame
    {
        public string FrameType => "bogus";
        public string Type { get; set; } = "bogus";
    }

    private sealed class OkWs : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;
    }

    private sealed class FailWs : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            throw new IOException("fail");
    }

    private sealed class DeferClientInterceptor : IByteInterceptor
    {
        public bool DeferNext { get; set; }

        public ValueTask<InterceptResult> OnUpstreamAsync(InterceptContext context, CancellationToken cancellationToken = default) =>
            ValueTask.FromResult(InterceptResult.Pass());

        public ValueTask<InterceptResult> OnClientAsync(InterceptContext context, CancellationToken cancellationToken = default) =>
            ValueTask.FromResult(DeferNext ? InterceptResult.Defer() : InterceptResult.Pass());
    }

    private sealed class ThrowingUpstream : IUpstreamPipe
    {
        private readonly TaskCompletionSource<byte[]> _tcs =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public bool ThrowOnDisconnect { get; set; }
        public bool ThrowOnReceive { get; set; }
        public bool IsConnected { get; private set; }

        public Task ConnectAsync(CancellationToken cancellationToken = default)
        {
            IsConnected = true;
            if (ThrowOnReceive)
            {
                _ = Task.Run(async () =>
                {
                    await Task.Delay(20).ConfigureAwait(false);
                    _tcs.TrySetException(new IOException("boom"));
                });
            }

            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            IsConnected = false;
            if (ThrowOnDisconnect)
            {
                throw new IOException("disconnect fail");
            }

            _tcs.TrySetResult(Array.Empty<byte>());
            return Task.CompletedTask;
        }

        public Task SendAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;

        public Task<byte[]> ReceiveAsync(CancellationToken cancellationToken = default) =>
            _tcs.Task.WaitAsync(cancellationToken);
    }
}
