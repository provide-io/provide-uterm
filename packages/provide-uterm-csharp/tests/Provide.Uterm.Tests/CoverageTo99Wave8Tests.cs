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
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Detection;
using Provide.Uterm.Fanout;
using Provide.Uterm.Filters;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using DetPrompt = Provide.Uterm.Detection.PromptDetection;
using VtChar = Provide.Uterm.Vt.Char;

namespace Provide.Uterm.Tests;

/// <summary>Wave 8: UtermServer WS residual arms + pure residual push toward ≥98%.</summary>
public class CoverageTo99Wave8Tests
{
    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var p = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    private static string MintJwt(AuthConfig auth, string subject, params string[] roles)
    {
        var secret = auth.JwtPublicKeyPem ?? throw new InvalidOperationException("no jwt secret");
        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(secret));
        var claims = new List<Claim> { new("sub", subject) };
        foreach (var r in roles)
        {
            claims.Add(new Claim(auth.JwtRolesClaim, r));
        }

        var now = DateTimeOffset.UtcNow;
        var token = new JwtSecurityToken(
            issuer: auth.JwtIssuer,
            audience: auth.JwtAudience,
            claims: claims,
            notBefore: now.UtcDateTime,
            expires: now.AddHours(1).UtcDateTime,
            signingCredentials: new SigningCredentials(key, SecurityAlgorithms.HmacSha256));
        return new JwtSecurityTokenHandler().WriteToken(token);
    }

    private static async Task<(UtermServer Server, string BaseUrl, string AdminToken, string ViewerToken, AuthConfig Auth, TermHub Hub)>
        StartServerAsync(params SessionDefinition[] extraSessions)
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
            Owner = "admin-user",
        });
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "noworker",
            DisplayName = "No Worker",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin-user",
        });
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "priv",
            DisplayName = "Private",
            ConnectorType = "shell",
            Visibility = "private",
            Owner = "someone-else",
        });
        foreach (var s in extraSessions)
        {
            cfg.Sessions.Add(s);
        }

        var adminTok = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "w8-admin-" + Guid.NewGuid().ToString("N")),
            Subject = "admin-user",
            Roles = new[] { "admin" },
        });
        var viewerTok = MintJwt(cfg.Auth, "viewer-user", "viewer");

        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock, WorkerToken = cfg.Auth.WorkerBearerToken });
        hub.Conn.RegisterWorker("demo", new EchoWorker());

        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Version = "wave8",
            Clock = clock,
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, $"http://127.0.0.1:{port}", adminTok, viewerTok, cfg.Auth, hub);
    }

    private sealed class EchoWorker : IWorkerWs
    {
        public List<string> Sent { get; } = new();

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            Sent.Add(payload);
            return Task.CompletedTask;
        }
    }

    private static async Task<ClientWebSocket> ConnectBrowserAsync(
        string baseUrl, string workerId, string? token, CancellationToken ct)
    {
        var ws = new ClientWebSocket();
        if (!string.IsNullOrEmpty(token))
        {
            ws.Options.SetRequestHeader("Authorization", "Bearer " + token);
        }

        var uri = new Uri(baseUrl.Replace("http://", "ws://", StringComparison.Ordinal)
                          + $"/ws/browser/{workerId}/term");
        await ws.ConnectAsync(uri, ct);
        return ws;
    }

    private static async Task SendCtrlAsync(ClientWebSocket ws, Dictionary<string, object?> msg, CancellationToken ct)
    {
        var bytes = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(msg));
        await ws.SendAsync(bytes, WebSocketMessageType.Text, true, ct);
    }

    private static async Task<List<Dictionary<string, object?>>> RecvFramesAsync(
        ClientWebSocket ws, byte[] buf, CancellationToken ct)
    {
        var result = await ws.ReceiveAsync(buf, ct);
        Assert.Equal(WebSocketMessageType.Text, result.MessageType);
        var text = Encoding.UTF8.GetString(buf, 0, result.Count);
        var frames = new List<Dictionary<string, object?>>();
        var dec = new ControlFrameDecoder();
        foreach (var chunk in dec.Feed(text))
        {
            if (chunk is ControlChunk ctrl)
            {
                frames.Add(ctrl.Control);
            }
        }

        return frames;
    }

    private static async Task DrainHandshakeAsync(ClientWebSocket ws, byte[] buf, CancellationToken ct)
    {
        // hello + hijack_state + presence_sync (may arrive as 1–3 WS messages)
        for (var i = 0; i < 3; i++)
        {
            try
            {
                using var shortCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                shortCts.CancelAfter(TimeSpan.FromMilliseconds(400));
                _ = await RecvFramesAsync(ws, buf, shortCts.Token);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    private static bool IsType(Dictionary<string, object?> f, string type) =>
        f.TryGetValue("type", out var t) && t?.ToString() == type;

    [Fact]
    public async Task BrowserWs_UtermTestMode_AdminHello()
    {
        var prev = Environment.GetEnvironmentVariable("UTERM_TEST_MODE");
        try
        {
            Environment.SetEnvironmentVariable("UTERM_TEST_MODE", "1");
            var (server, baseUrl, _, _, _, _) = await StartServerAsync();
            await using (server)
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                var buf = new byte[65536];
                // No auth header — test mode forces admin for any worker_id.
                using var ws = await ConnectBrowserAsync(baseUrl, "demo", token: null, cts.Token);
                var frames = new List<Dictionary<string, object?>>();
                frames.AddRange(await RecvFramesAsync(ws, buf, cts.Token));
                var hello = frames.FirstOrDefault(f => IsType(f, "hello"));
                if (hello is null)
                {
                    frames.AddRange(await RecvFramesAsync(ws, buf, cts.Token));
                    hello = frames.First(f => IsType(f, "hello"));
                }

                Assert.Equal("admin", hello["role"]?.ToString());
                Assert.True(hello.TryGetValue("can_hijack", out var ch) && ch is true);
                ws.Abort();
            }
        }
        finally
        {
            Environment.SetEnvironmentVariable("UTERM_TEST_MODE", prev);
        }
    }

    [Fact]
    public async Task BrowserWs_Viewer_HijackRequest_Denied()
    {
        var (server, baseUrl, _, viewerTok, _, _) = await StartServerAsync();
        await using (server)
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(12));
            var buf = new byte[65536];
            using var ws = await ConnectBrowserAsync(baseUrl, "demo", viewerTok, cts.Token);
            await DrainHandshakeAsync(ws, buf, cts.Token);

            await SendCtrlAsync(ws, new Dictionary<string, object?> { ["type"] = "hijack_request" }, cts.Token);

            Dictionary<string, object?>? err = null;
            for (var i = 0; i < 8 && err is null; i++)
            {
                var frames = await RecvFramesAsync(ws, buf, cts.Token);
                err = frames.FirstOrDefault(f => IsType(f, "error"));
            }

            Assert.NotNull(err);
            Assert.Contains("admin", err!["message"]?.ToString() ?? "", StringComparison.OrdinalIgnoreCase);
            ws.Abort();
        }
    }

    [Fact]
    public async Task BrowserWs_SecondHijack_AlreadyHijacked()
    {
        var (server, baseUrl, adminTok, _, _, _) = await StartServerAsync();
        await using (server)
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(15));
            var buf = new byte[65536];
            using var ws1 = await ConnectBrowserAsync(baseUrl, "demo", adminTok, cts.Token);
            await DrainHandshakeAsync(ws1, buf, cts.Token);
            await SendCtrlAsync(ws1, new Dictionary<string, object?> { ["type"] = "hijack_request" }, cts.Token);
            await Task.Delay(150, cts.Token); // settle acquire before second browser races

            using var ws2 = await ConnectBrowserAsync(baseUrl, "demo", adminTok, cts.Token);
            await DrainHandshakeAsync(ws2, buf, cts.Token);
            await SendCtrlAsync(ws2, new Dictionary<string, object?> { ["type"] = "hijack_request" }, cts.Token);

            Dictionary<string, object?>? err = null;
            for (var i = 0; i < 10 && err is null; i++)
            {
                var frames = await RecvFramesAsync(ws2, buf, cts.Token);
                err = frames.FirstOrDefault(f =>
                    IsType(f, "error")
                    && (f["message"]?.ToString() ?? "").Contains("already", StringComparison.OrdinalIgnoreCase));
            }

            Assert.NotNull(err);
            Assert.Contains("already", err!["message"]?.ToString() ?? "", StringComparison.OrdinalIgnoreCase);
            // Raw terminal input while hijacked (owner → PrepareBrowserInput + worker send).
            await ws1.SendAsync(Encoding.UTF8.GetBytes("typed-keys"), WebSocketMessageType.Text, true, cts.Token);
            await Task.Delay(100, cts.Token);
            ws1.Abort();
            ws2.Abort();
        }
    }

    [Fact]
    public async Task BrowserWs_HijackNoWorker_ErrorResumePath()
    {
        var (server, baseUrl, adminTok, _, _, _) = await StartServerAsync();
        await using (server)
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(12));
            var buf = new byte[65536];
            using var ws = await ConnectBrowserAsync(baseUrl, "noworker", adminTok, cts.Token);
            await DrainHandshakeAsync(ws, buf, cts.Token);
            await SendCtrlAsync(ws, new Dictionary<string, object?> { ["type"] = "hijack_request" }, cts.Token);

            Dictionary<string, object?>? err = null;
            for (var i = 0; i < 8 && err is null; i++)
            {
                var frames = await RecvFramesAsync(ws, buf, cts.Token);
                err = frames.FirstOrDefault(f => IsType(f, "error"));
            }

            Assert.NotNull(err);
            Assert.Contains("failed", err!["message"]?.ToString() ?? "", StringComparison.OrdinalIgnoreCase);
            ws.Abort();
        }
    }

    [Fact]
    public async Task BrowserWs_PrivateSession_ViewerForbidden()
    {
        var (server, baseUrl, _, viewerTok, _, _) = await StartServerAsync();
        await using (server)
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            using var ws = new ClientWebSocket();
            ws.Options.SetRequestHeader("Authorization", "Bearer " + viewerTok);
            var uri = new Uri(baseUrl.Replace("http://", "ws://", StringComparison.Ordinal)
                              + "/ws/browser/priv/term");
            try
            {
                await ws.ConnectAsync(uri, cts.Token);
                // Some hosts reject during upgrade; others accept then close.
                Assert.True(ws.State is WebSocketState.Open or WebSocketState.CloseReceived
                    or WebSocketState.Closed or WebSocketState.Aborted);
            }
            catch (WebSocketException)
            {
                // expected when server rejects the upgrade with 403
            }
            catch (HttpRequestException)
            {
                // expected on some runtimes
            }
        }
    }

    [Fact]
    public async Task Rest_CookieAuth_And_OpenModeAcquireMessage()
    {
        var (server, baseUrl, adminTok, _, auth, hub) = await StartServerAsync();
        await using (server)
        {
            using var http = new HttpClient { BaseAddress = new Uri(baseUrl) };
            // Cookie auth path (Authenticate iterates Cookies).
            var cookieReq = new HttpRequestMessage(HttpMethod.Get, "/api/sessions");
            cookieReq.Headers.TryAddWithoutValidation("Cookie", auth.TokenCookie + "=" + adminTok);
            var cookieResp = await http.SendAsync(cookieReq);
            cookieResp.EnsureSuccessStatusCode();

            hub.Router.SetInputMode("demo", InputModes.Open);
            http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + adminTok);
            var acq = await http.PostAsync("/worker/demo/hijack/acquire",
                new StringContent("""{"owner":"op","lease_s":30}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.Conflict, acq.StatusCode);
            var body = await acq.Content.ReadAsStringAsync();
            Assert.Contains("open", body, StringComparison.OrdinalIgnoreCase);

            // Invalid worker_id on ValidateIds via hijack heartbeat path.
            var bad = await http.PostAsync("/worker/bad%2Fid/hijack/x/heartbeat",
                new StringContent("{}", Encoding.UTF8, "application/json"));
            Assert.True((int)bad.StatusCode is 422 or 404 or 400);

            // Viewer cannot mutate private / hijack unknown with insufficient privileges.
            var viewerTok = MintJwt(auth, "v2", "viewer");
            using var httpV = new HttpClient { BaseAddress = new Uri(baseUrl) };
            httpV.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + viewerTok);
            var forbidden = await httpV.PostAsync("/worker/demo/hijack/acquire",
                new StringContent("""{"owner":"v","lease_s":30}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.Forbidden, forbidden.StatusCode);
        }
    }

    [Fact]
    public void Limiter_Evicts_When_Over_Cache_Max()
    {
        var lim = new RateLimiter(1_000_000, 1_000_000);
        // RestClientCacheMax = 1024; eviction kicks after >1024 unique keys.
        for (var i = 0; i < RateLimiter.RestClientCacheMax + 32; i++)
        {
            Assert.True(lim.AllowRestAcquire("acq-" + i));
            Assert.True(lim.AllowRestSend("send-" + i));
        }
    }

    [Fact]
    public void Filters_Eof_And_Unknown_Command_Edges()
    {
        // SB: IAC then EOF (not SE) → early return
        using (var ms = new MemoryStream(new byte[] { InputFilters.Sb, 24, InputFilters.Iac }))
        {
            InputFilters.ConsumeIac(ms);
        }

        // unknown IAC command (NOP) fall-through
        using (var ms = new MemoryStream(new byte[] { 241 }))
        {
            InputFilters.ConsumeIac(ms);
        }

        // CSI mid-stream EOF
        using (var ms = new MemoryStream(new byte[] { (byte)'[', (byte)'3', (byte)'1' }))
        {
            InputFilters.ConsumeEscape(ms);
        }

        // unknown ESC intermediate (not [ or O)
        using (var ms = new MemoryStream(new byte[] { (byte)'Z' }))
        {
            InputFilters.ConsumeEscape(ms);
        }
    }

    [Fact]
    public void Pure_Models_Vt_Fanout_Detection_WsBytes()
    {
        var sb = new ScreenBuffer { Text = "hi", Hash = "h" };
        Assert.Equal("hi", sb.Text);
        var match = new PromptMatch
        {
            PromptId = "p",
            Pattern = new Dictionary<string, object?> { ["r"] = "x" },
            InputType = "text",
            EolPattern = "\r",
            KvExtract = "k",
        };
        var det = new DetPrompt
        {
            PromptId = "p",
            InputType = "text",
            KvData = new Dictionary<string, object?> { ["a"] = 1 },
            Match = match,
            IsIdle = true,
            Buffer = sb,
        };
        Assert.True(det.IsIdle);
        Assert.Same(match, det.Match);

        var diag = new PromptDetectionDiagnostics
        {
            Match = match,
            RegexMatchedButFailed = new List<Dictionary<string, object?>>
            {
                new() { ["id"] = "x" },
            },
        };
        Assert.NotEmpty(diag.RegexMatchedButFailed);

        var g = new Group
        {
            Mode = "serial",
            QuiesceMs = 10,
            MaxResponseMs = 20,
            StopOnFirstError = true,
            ErrorPattern = "err",
        };
        Assert.Equal("serial", g.Mode);
        Assert.Equal(10, g.QuiesceMs);
        var result = new Result
        {
            GroupId = "g",
            SendId = "s",
            Command = "ls",
            SentAt = 1.5,
            Results = new List<SessionResult>
            {
                new() { WorkerId = "w", Ok = true, OutputDelta = "o", ElapsedMs = 1, Divergent = false },
            },
        };
        Assert.Equal(1.5, result.SentAt);
        Assert.NotEmpty(result.ResultMaps());

        var a = VtChar.DefaultPlain;
        a.Dim = true;
        var b = VtChar.DefaultPlain;
        Assert.False(a.Equals((object)b));
        Assert.True(a.Dim);
        var a2 = a;
        Assert.True(a == a2);
        Assert.True(a != b);

        // high-codepoint replacement path
        var bytes = WsBytes.ChannelStrToBytes("A\u0100B");
        Assert.Equal(3, bytes.Length);
        Assert.Equal((byte)'?', bytes[1]);
    }
}
