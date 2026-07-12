//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Hub;
using Provide.Uterm.Recording;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>HTTP parity tests for annotate + recording meta/entries/download.</summary>
public class ServerRecordingHttpTests
{
    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private static async Task<(UtermServer Server, HttpClient Client, string SessionId, IRecordingStore Store)> StartAsync(
        IRecordingStore? store = null,
        string visibility = "public",
        string[]? roles = null)
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Recording.StoreType = "memory";
        cfg.Recording.Directory = Path.Combine(Path.GetTempPath(), "uterm-rec-http-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(cfg.Recording.Directory);

        var sessionId = "s1";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = sessionId,
            DisplayName = "S1",
            ConnectorType = "shell",
            Visibility = visibility,
            Owner = "dev-user",
        });

        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "uterm-rec-token-" + Guid.NewGuid().ToString("N")),
            Subject = "dev-user",
            Roles = roles ?? new[] { "admin" },
        });

        store ??= new InMemoryStore();
        var apiKeys = new ApiKeyStore();
        var auth = new LocalIdentityProvider(cfg.Auth, apiKeys);
        var authz = new AuthorizationService();
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock });
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
            Recording = store,
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();

        var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        return (server, client, sessionId, store);
    }

    [Fact]
    public async Task RecordingMeta_Entries_And_Annotate()
    {
        var store = new InMemoryStore();
        await store.StartSessionAsync("s1", new Dictionary<string, object?> { ["k"] = "v" });
        await store.AppendEventsAsync("s1", new[]
        {
            new Event { ["ts"] = 1.0, ["event"] = "output", ["data"] = "a", ["session_id"] = "s1" },
            new Event { ["ts"] = 2.0, ["event"] = "input", ["data"] = "b", ["session_id"] = "s1" },
            new Event { ["ts"] = 3.0, ["event"] = "output", ["data"] = "c", ["session_id"] = "s1" },
        });

        var (server, client, sid, _) = await StartAsync(store);
        await using (server)
        using (client)
        {
            var metaResp = await client.GetAsync($"/api/sessions/{sid}/recording");
            Assert.Equal(HttpStatusCode.OK, metaResp.StatusCode);
            using var metaDoc = JsonDocument.Parse(await metaResp.Content.ReadAsStringAsync());
            Assert.True(metaDoc.RootElement.GetProperty("exists").GetBoolean());
            Assert.Equal(sid, metaDoc.RootElement.GetProperty("session_id").GetString());
            Assert.True(metaDoc.RootElement.GetProperty("size_bytes").GetInt64() > 0);

            var entriesResp = await client.GetAsync($"/api/sessions/{sid}/recording/entries?event=output&limit=10");
            Assert.Equal(HttpStatusCode.OK, entriesResp.StatusCode);
            using var entriesDoc = JsonDocument.Parse(await entriesResp.Content.ReadAsStringAsync());
            Assert.Equal(JsonValueKind.Array, entriesDoc.RootElement.ValueKind);
            Assert.Equal(2, entriesDoc.RootElement.GetArrayLength());

            var offsetResp = await client.GetAsync($"/api/sessions/{sid}/recording/entries?offset=1&limit=1");
            Assert.Equal(HttpStatusCode.OK, offsetResp.StatusCode);

            var badOff = await client.GetAsync($"/api/sessions/{sid}/recording/entries?offset=-1");
            Assert.Equal(HttpStatusCode.UnprocessableEntity, badOff.StatusCode);

            var annBody = new StringContent(
                """{"label":"deploy","description":"ok","severity":"info"}""",
                Encoding.UTF8,
                "application/json");
            var annResp = await client.PostAsync($"/api/sessions/{sid}/annotate", annBody);
            Assert.Equal(HttpStatusCode.OK, annResp.StatusCode);
            using var annDoc = JsonDocument.Parse(await annResp.Content.ReadAsStringAsync());
            Assert.True(annDoc.RootElement.TryGetProperty("ts", out _));
            Assert.True(annDoc.RootElement.TryGetProperty("seq", out _));

            var annEntries = await client.GetAsync($"/api/sessions/{sid}/recording/entries?event=annotation&limit=50");
            Assert.Equal(HttpStatusCode.OK, annEntries.StatusCode);
            using var annE = JsonDocument.Parse(await annEntries.Content.ReadAsStringAsync());
            Assert.True(annE.RootElement.GetArrayLength() >= 1);

            var noLabel = await client.PostAsync(
                $"/api/sessions/{sid}/annotate",
                new StringContent("""{"label":"  "}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.BadRequest, noLabel.StatusCode);

            var badSev = await client.PostAsync(
                $"/api/sessions/{sid}/annotate",
                new StringContent("""{"label":"x","severity":"nope"}""", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.BadRequest, badSev.StatusCode);
        }
    }

    [Fact]
    public async Task RecordingDownload_LocalFile_And_PathGate()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-rec-dl-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var store = new LocalFileStore(dir);
            await store.StartSessionAsync("s1", new Dictionary<string, object?>());
            await store.AppendEventsAsync("s1", new[]
            {
                new Event { ["ts"] = 1.0, ["event"] = "output", ["data"] = "hi", ["session_id"] = "s1" },
            });
            await store.EndSessionAsync("s1");

            var port = FreePort();
            var cfg = UtermServerConfig.Default();
            cfg.Server.Host = "127.0.0.1";
            cfg.Server.Port = port;
            cfg.Auth.Mode = "dev_token";
            cfg.Recording.StoreType = "local";
            cfg.Recording.Directory = dir;
            cfg.Sessions.Add(new SessionDefinition
            {
                SessionId = "s1",
                DisplayName = "S1",
                ConnectorType = "shell",
                Visibility = "public",
                Owner = "dev-user",
            });
            var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
            {
                TokenPath = Path.Combine(Path.GetTempPath(), "uterm-dl-token-" + Guid.NewGuid().ToString("N")),
                Subject = "dev-user",
                Roles = new[] { "admin" },
            });
            var clock = new RealClock();
            var server = new UtermServer(new ServerDeps
            {
                Hub = new TermHub(new TermHubConfig { Clock = clock }),
                Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
                Authz = new AuthorizationService(),
                Config = cfg,
                Registry = new InMemorySessionRegistry(cfg.Sessions),
                Version = "test",
                Clock = clock,
                Recording = store,
            });
            server.Build(new[] { $"http://127.0.0.1:{port}" });
            await server.StartAsync();
            await using (server)
            {
                using var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
                client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);

                var dl = await client.GetAsync("/api/sessions/s1/recording/download");
                Assert.Equal(HttpStatusCode.OK, dl.StatusCode);
                var body = await dl.Content.ReadAsStringAsync();
                Assert.Contains("log_start", body, StringComparison.Ordinal);
                Assert.Contains("output", body, StringComparison.Ordinal);

                // Memory store has no path → 404
                var mem = new InMemoryStore();
                await mem.StartSessionAsync("s1", new Dictionary<string, object?>());
                var memServer = new UtermServer(new ServerDeps
                {
                    Hub = new TermHub(new TermHubConfig { Clock = clock }),
                    Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
                    Authz = new AuthorizationService(),
                    Config = cfg,
                    Registry = new InMemorySessionRegistry(cfg.Sessions),
                    Version = "test",
                    Clock = clock,
                    Recording = mem,
                });
                var port2 = FreePort();
                memServer.Build(new[] { $"http://127.0.0.1:{port2}" });
                await memServer.StartAsync();
                await using (memServer)
                {
                    using var c2 = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port2}") };
                    c2.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
                    var miss = await c2.GetAsync("/api/sessions/s1/recording/download");
                    Assert.Equal(HttpStatusCode.NotFound, miss.StatusCode);
                }
            }
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { /* best effort */ }
        }
    }

    [Fact]
    public async Task Recording_Forbidden_And_Unknown()
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "priv",
            DisplayName = "Priv",
            ConnectorType = "shell",
            Visibility = "private",
            Owner = "someoneelse",
        });
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "uterm-forb-" + Guid.NewGuid().ToString("N")),
            Subject = "viewer-user",
            Roles = new[] { "viewer" },
        });
        var clock = new RealClock();
        var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(new TermHubConfig { Clock = clock }),
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Version = "test",
            Clock = clock,
            Recording = new InMemoryStore(),
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        await using (server)
        {
            using var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
            client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
            var forb = await client.GetAsync("/api/sessions/priv/recording");
            Assert.Equal(HttpStatusCode.Forbidden, forb.StatusCode);

            var missing = await client.GetAsync("/api/sessions/nope/recording");
            Assert.Equal(HttpStatusCode.NotFound, missing.StatusCode);
        }
    }

    [Fact]
    public void RecordingPathAllowed_ConfinesUnderDirectory()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-path-gate-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var inside = Path.Combine(dir, "s1.jsonl");
            File.WriteAllText(inside, "{}\n");
            Assert.True(UtermServer.RecordingPathAllowed(inside, dir));
            Assert.False(UtermServer.RecordingPathAllowed(Path.Combine(Path.GetTempPath(), "escape.jsonl"), dir));
            Assert.False(UtermServer.RecordingPathAllowed(inside, ""));
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { /* best effort */ }
        }
    }

    [Fact]
    public void BuildRecordingStore_SelectsByConfig()
    {
        var cfg = UtermServerConfig.Default();
        cfg.Recording.StoreType = "memory";
        Assert.IsType<InMemoryStore>(ServerFactory.BuildRecordingStore(cfg));
        cfg.Recording.StoreType = "local";
        cfg.Recording.Directory = Path.GetTempPath();
        Assert.IsType<LocalFileStore>(ServerFactory.BuildRecordingStore(cfg));
        cfg.Recording.StoreType = "null";
        Assert.IsType<NullStore>(ServerFactory.BuildRecordingStore(cfg));
    }
}
