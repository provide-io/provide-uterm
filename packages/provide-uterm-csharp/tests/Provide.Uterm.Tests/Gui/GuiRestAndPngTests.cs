//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using Provide.Uterm.Client;
using Provide.Uterm.Gui;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Gui;

public class GuiRestAndPngTests
{
    [Fact]
    public void RgbaImage_RejectsHugeDimensions()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => new RgbaImage(RgbaImage.MaxDimension + 1, 1));
    }

    [Fact]
    public void Png_Encode_HasSignatureAndChunks()
    {
        var img = new RgbaImage(2, 2);
        img.Pixels[0] = 255;
        img.Pixels[3] = 255;
        var png = Png.EncodeRgba(img.Width, img.Height, img.Pixels);
        Assert.Equal(137, png[0]);
        Assert.Equal(80, png[1]); // 'P'
        Assert.Equal(78, png[2]); // 'N'
        Assert.Equal(71, png[3]); // 'G'
        Assert.True(png.Length > 50);
    }

    [Fact]
    public async Task Gui_Attach_Screenshot_Input_RequiresLease()
    {
        var (server, baseUrl, token, graphicalTargets) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var targetId = CreateGraphicalTarget(graphicalTargets);

            var attach = await client.GuiAttachAsync("demo", new Dictionary<string, object?>
            {
                ["target_id"] = targetId,
            });
            Assert.True(attach.TryGetValue("ok", out var aok) && aok is true);

            // No hijack → screenshot fails
            var noLease = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiScreenshotAsync("demo", "deadbeef"));
            Assert.Equal(404, noLease.StatusCode);

            var acq = await client.AcquireAsync("demo", owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]?.ToString()!;
            Assert.False(string.IsNullOrEmpty(hid));

            var shot = await client.GuiScreenshotAsync("demo", hid);
            Assert.True(shot.TryGetValue("ok", out var sok) && sok is true);
            var b64 = shot["screenshot"]?.ToString();
            Assert.False(string.IsNullOrEmpty(b64));
            var bytes = Convert.FromBase64String(b64!);
            Assert.Equal(137, bytes[0]);

            var click = await client.GuiClickAsync("demo", hid, 1, 1);
            Assert.True(click.TryGetValue("ok", out var cok) && cok is true);

            var typed = await client.GuiTypeAsync("demo", hid, "ab");
            Assert.True(typed.TryGetValue("ok", out var tok) && tok is true);

            var key = await client.GuiKeyAsync("demo", hid, "Enter");
            Assert.True(key.TryGetValue("ok", out var kok) && kok is true);

            var drag = await client.GuiDragAsync("demo", hid, 0, 0, 2, 2);
            Assert.True(drag.TryGetValue("ok", out var dok) && dok is true);

            // Release then input denied
            await client.ReleaseAsync("demo", hid);
            var denied = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiClickAsync("demo", hid, 1, 1));
            Assert.Equal(404, denied.StatusCode);
        }
    }

    [Fact]
    public async Task Gui_Attach_UsesTargetId()
    {
        var (server, baseUrl, token, graphicalTargets) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var targetId = CreateGraphicalTarget(graphicalTargets);
            var ok = await client.GuiAttachAsync("demo", new Dictionary<string, object?>
            {
                ["mode"] = "litevirt",
                ["target_id"] = targetId,
            });
            Assert.True(ok.TryGetValue("ok", out var okValue) && okValue is true);
        }
    }

    [Fact]
    public async Task Gui_Attach_Rfb_RequiresTarget_And_FailsClosedOnConnect()
    {
        var (server, baseUrl, token, graphicalTargets) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var missing = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiAttachAsync("demo", new Dictionary<string, object?>
                {
                    ["mode"] = "rfb",
                }));
            Assert.Equal(422, missing.StatusCode);

            var closed = FreePort();
            var targetId = CreateGraphicalTarget(
                graphicalTargets,
                protocol: "rfb",
                endpoint: $"127.0.0.1:{closed}");

            // Closed port → 502
            var fail = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiAttachAsync("demo", new Dictionary<string, object?>
                {
                    ["target_id"] = targetId,
                }));
            Assert.Equal(502, fail.StatusCode);
        }
    }

    [Fact]
    public async Task Gui_Attach_TargetId_And_InvalidTargetId()
    {
        var (server, baseUrl, token, graphicalTargets) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var targetId = CreateGraphicalTarget(
                graphicalTargets,
                width: 8,
                height: 8);
            var ok = await client.GuiAttachAsync("demo", new Dictionary<string, object?>
            {
                ["target_id"] = targetId,
            });
            Assert.True(ok.TryGetValue("ok", out var a) && a is true);

            var missing = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiAttachAsync("demo", new Dictionary<string, object?>
                {
                    ["target_id"] = "non-existent",
                }));
            Assert.Equal(404, missing.StatusCode);

            var hugeTarget = await Assert.ThrowsAsync<ApiException>(() =>
                client.Post(
                    "/api/graphical-targets",
                    new Dictionary<string, object?>
                    {
                        ["protocol"] = "memory",
                        ["width"] = 100_000,
                        ["height"] = 1,
                    }));
            Assert.Equal(422, hugeTarget.StatusCode);
        }
    }

    [Fact]
    public async Task Gui_Attach_Rejects_Bad_WorkerId()
    {
        var (server, baseUrl, token, _) = await StartServerAsync();
        await using (server)
        {
            // Raw request: the typed client validates worker_id before sending, so
            // we hit the server route directly to exercise its SafeId guard.
            using var raw = new HttpClient { BaseAddress = new Uri(baseUrl) };
            raw.DefaultRequestHeaders.Add("Authorization", "Bearer " + token);
            var resp = await raw.PostAsync("/worker/bad%20worker/gui/attach",
                new StringContent("{\"target_id\":\"x\"}", System.Text.Encoding.UTF8, "application/json"));
            Assert.Equal(422, (int)resp.StatusCode);
        }
    }

    [Fact]
    public async Task Gui_Attach_Denied_Without_Attach_Capability()
    {
        // Viewer lacks graphical.session.attach → 403.
        var (server, baseUrl, token, graphicalTargets) = await StartServerAsync(roles: new[] { "viewer" });
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var targetId = CreateGraphicalTarget(graphicalTargets);
            var denied = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiAttachAsync("demo", new Dictionary<string, object?> { ["target_id"] = targetId }));
            Assert.Equal(403, denied.StatusCode);
        }
    }

    [Fact]
    public async Task Gui_Attach_Denied_Without_Tenant_Scope()
    {
        // Admin with no tenant claim: capability + hijack pass, but tenant scope
        // cannot be resolved → 403 graphical target access denied.
        var (server, baseUrl, token, graphicalTargets) = await StartServerAsync(tenant: null);
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var denied = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiAttachAsync("demo", new Dictionary<string, object?> { ["target_id"] = "gt-any" }));
            Assert.Equal(403, denied.StatusCode);
        }
    }

    [Fact]
    public async Task Gui_KeyVariants_And_Buttons()
    {
        var (server, baseUrl, token, graphicalTargets) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var targetId = CreateGraphicalTarget(graphicalTargets);
            await client.GuiAttachAsync("demo", new Dictionary<string, object?> { ["target_id"] = targetId });
            var acq = await client.AcquireAsync("demo");
            var hid = acq["hijack_id"]!.ToString()!;
            foreach (var k in new[] { "Tab", "Esc", "Backspace", "Up", "Down", "Left", "Right", "Unknown" })
            {
                var r = await client.GuiKeyAsync("demo", hid, k);
                Assert.True(r.TryGetValue("ok", out var o) && o is true);
            }

            await client.GuiClickAsync("demo", hid, 0, 0, "middle");
            await client.GuiClickAsync("demo", hid, 0, 0, "right");
            await client.GuiClickAsync("demo", hid, 0, 0, "other");
            var shot = await client.GuiScreenshotAsync("demo", hid);
            Assert.True(shot.TryGetValue("ok", out var s) && s is true);
        }
    }

    [Fact]
    public void Png_Rejects_ShortBuffer()
    {
        Assert.Throws<ArgumentException>(() => Png.EncodeRgba(2, 2, new byte[4]));
        Assert.Throws<ArgumentOutOfRangeException>(() => Png.EncodeRgba(0, 1, Array.Empty<byte>()));
    }

    [Fact]
    public void RgbaImage_Rejects_WrongPixelLength()
    {
        Assert.Throws<ArgumentException>(() => new RgbaImage(2, 2, new byte[3]));
    }

    [Fact]
    public async Task Gui_Attach_Litevirt_Returns_501()
    {
        // litevirt is a canonical protocol, but this C# port ships no litevirt
        // client → attach must fail closed with 501 (not supported).
        var (server, baseUrl, token, graphicalTargets) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var targetId = CreateGraphicalTarget(
                graphicalTargets,
                protocol: "litevirt",
                endpoint: "10.0.0.5:7443");

            var notSupported = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiAttachAsync("demo", new Dictionary<string, object?> { ["target_id"] = targetId }));
            Assert.Equal(501, notSupported.StatusCode);
        }
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private const string TestTenant = "acme";

    private static string CreateGraphicalTarget(
        InMemoryGraphicalTargetRegistry graphicalTargets,
        string protocol = "memory",
        string? endpoint = null,
        int width = 32,
        int height = 24)
    {
        var targetId = "gt-" + Guid.NewGuid().ToString("N")[..12];
        var target = new Provide.Uterm.Server.GraphicalTargetDefinition
        {
            TargetId = targetId,
            TenantId = TestTenant,
            DisplayName = targetId,
            Protocol = protocol,
            Endpoint = endpoint,
            Width = width,
            Height = height,
            Secret = null,
            IsSystem = false,
            CreatedBy = "test",
            UpdatedBy = "test",
        };
        Assert.True(GraphicalTargetScope.TryForTenant(TestTenant, out var scope));
        graphicalTargets.Create(scope, target);
        return targetId;
    }

    private static async Task<(UtermServer Server, string BaseUrl, string Token, InMemoryGraphicalTargetRegistry GraphicalTargets)> StartServerAsync(
        string[]? roles = null,
        string? tenant = TestTenant)
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
            Owner = "dev-user",
        });

        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "uterm-gui-token-" + Guid.NewGuid().ToString("N")),
            Subject = "dev-user",
            Roles = roles ?? new[] { "admin" },
            Tenant = tenant,
        });

        var apiKeys = new ApiKeyStore();
        var auth = new LocalIdentityProvider(cfg.Auth, apiKeys);
        var authz = new AuthorizationService();
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock, WorkerToken = cfg.Auth.WorkerBearerToken });
        hub.Conn.RegisterWorker("demo", new TestEchoWorker());

        var registry = new InMemorySessionRegistry(cfg.Sessions);
        var graphicalTargets = new InMemoryGraphicalTargetRegistry();
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = auth,
            Authz = authz,
            Config = cfg,
            Registry = registry,
            GraphicalTargets = graphicalTargets,
            Version = "test",
            Clock = clock,
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, $"http://127.0.0.1:{port}", token, graphicalTargets);
    }

    private sealed class TestEchoWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;
    }
}
