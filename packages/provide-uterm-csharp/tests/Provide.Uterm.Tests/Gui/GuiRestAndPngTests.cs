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
        var (server, baseUrl, token) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);

            var attach = await client.GuiAttachAsync("demo", new Dictionary<string, object?>
            {
                ["mode"] = "memory",
                ["width"] = 32,
                ["height"] = 24,
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
    public async Task Gui_Attach_UnsupportedMode_Is501()
    {
        var (server, baseUrl, token) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var ex = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiAttachAsync("demo", new Dictionary<string, object?>
                {
                    ["mode"] = "litevirt",
                }));
            Assert.Equal(501, ex.StatusCode);
        }
    }

    [Fact]
    public async Task Gui_Attach_EmptyMode_InfersFromTarget()
    {
        var (server, baseUrl, token) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var ok = await client.GuiAttachAsync("demo", new Dictionary<string, object?>
            {
                ["mode"] = "",
                ["target_address"] = "memory",
                ["width"] = 8,
                ["height"] = 8,
            });
            Assert.True(ok.TryGetValue("ok", out var a) && a is true);

            var bad = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiAttachAsync("demo", new Dictionary<string, object?>
                {
                    ["mode"] = "",
                    ["target_address"] = "grpc://litevirt:50051",
                }));
            Assert.Equal(501, bad.StatusCode);

            var huge = await Assert.ThrowsAsync<ApiException>(() =>
                client.GuiAttachAsync("demo", new Dictionary<string, object?>
                {
                    ["mode"] = "memory",
                    ["width"] = RgbaImage.MaxDimension + 1,
                    ["height"] = 1,
                }));
            Assert.Equal(422, huge.StatusCode);
        }
    }

    [Fact]
    public async Task Gui_KeyVariants_And_Buttons()
    {
        var (server, baseUrl, token) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            await client.GuiAttachAsync("demo");
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

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private static async Task<(UtermServer Server, string BaseUrl, string Token)> StartServerAsync()
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
            Roles = new[] { "admin" },
        });

        var apiKeys = new ApiKeyStore();
        var auth = new LocalIdentityProvider(cfg.Auth, apiKeys);
        var authz = new AuthorizationService();
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock, WorkerToken = cfg.Auth.WorkerBearerToken });
        hub.Conn.RegisterWorker("demo", new TestEchoWorker());

        var registry = new InMemorySessionRegistry(cfg.Sessions);
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = auth,
            Authz = authz,
            Config = cfg,
            Registry = registry,
            Version = "test",
            Clock = clock,
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, $"http://127.0.0.1:{port}", token);
    }

    private sealed class TestEchoWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;
    }
}
