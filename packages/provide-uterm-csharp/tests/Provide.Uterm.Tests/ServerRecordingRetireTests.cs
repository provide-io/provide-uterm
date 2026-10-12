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

/// <summary>
/// A deleted session's recording is never served to the id's next holder. The
/// server starts each session's recording under an instance of its own before
/// the first write, retires the recording when the session is deleted, and
/// retires anything left under an id when a session is created there. Port of
/// test_registry_recording_retire.py.
/// </summary>
public sealed class ServerRecordingRetireTests : IDisposable
{
    private const string Sid = "pam-suokki-capture-4242-777";
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "uterm-rec-retire-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        if (Directory.Exists(_dir))
        {
            Directory.Delete(_dir, recursive: true);
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

    private static async Task<(UtermServer Server, HttpClient Client)> StartAsync(IRecordingStore store)
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "uterm-rec-token-" + Guid.NewGuid().ToString("N")),
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
        var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        return (server, client);
    }

    private static Task<HttpResponseMessage> Create(HttpClient client, string id) =>
        client.PostAsync("/api/sessions", new StringContent(
            JsonSerializer.Serialize(new { session_id = id, connector_type = "shell" }), Encoding.UTF8, "application/json"));

    private static async Task Annotate(HttpClient client, string id, string label)
    {
        var resp = await client.PostAsync($"/api/sessions/{id}/annotate", new StringContent(
            JsonSerializer.Serialize(new { label }), Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.OK, resp.StatusCode);
    }

    private static async Task<JsonElement> Entries(HttpClient client, string id)
    {
        var resp = await client.GetAsync($"/api/sessions/{id}/recording/entries?limit=500");
        Assert.Equal(HttpStatusCode.OK, resp.StatusCode);
        return JsonDocument.Parse(await resp.Content.ReadAsStringAsync()).RootElement;
    }

    private IRecordingStore Make(string kind) => kind == "file" ? new LocalFileStore(_dir) : new InMemoryStore();

    [Theory]
    [InlineData("file")]
    [InlineData("memory")]
    public async Task RecreatedIdReadsNothingOfTheDeletedSession(string kind)
    {
        var (server, client) = await StartAsync(Make(kind));
        await using (server)
        using (client)
        {
            Assert.Equal(HttpStatusCode.OK, (await Create(client, Sid)).StatusCode);
            await Annotate(client, Sid, "secretA");
            Assert.Contains("secretA", (await Entries(client, Sid)).GetRawText());

            Assert.Equal(HttpStatusCode.OK, (await client.DeleteAsync($"/api/sessions/{Sid}")).StatusCode);
            Assert.Equal(HttpStatusCode.OK, (await Create(client, Sid)).StatusCode);

            // Before the new session has written anything -- it may never.
            Assert.Equal(0, (await Entries(client, Sid)).GetArrayLength());
            await Annotate(client, Sid, "secretB");
            var after = (await Entries(client, Sid)).GetRawText();
            Assert.DoesNotContain("secretA", after);
            Assert.Contains("secretB", after);
        }
    }

    [Theory]
    [InlineData("file")]
    [InlineData("memory")]
    public async Task ALeftoverRecordingIsRetiredWhenItsIdIsCreated(string kind)
    {
        var store = Make(kind);
        await store.AppendEventsAsync(Sid, [new Event { ["ts"] = 1.0, ["event"] = "read", ["data"] = "before-restart" }]);
        var (server, client) = await StartAsync(store);
        await using (server)
        using (client)
        {
            Assert.Equal(HttpStatusCode.OK, (await Create(client, Sid)).StatusCode);
            Assert.Equal(0, (await Entries(client, Sid)).GetArrayLength());
        }
    }

    [Fact]
    public async Task TheFirstWriteStartsTheRecordingUnderAnInstance()
    {
        var store = new InMemoryStore();
        var (server, client) = await StartAsync(store);
        await using (server)
        using (client)
        {
            await Create(client, Sid);
            await Annotate(client, Sid, "one");
            await Annotate(client, Sid, "two");
            var entries = await Entries(client, Sid);
            var events = entries.EnumerateArray().Select(e => e.GetProperty("event").GetString()).ToList();
            Assert.Equal(["log_start", "annotation", "annotation"], events);
            Assert.False(string.IsNullOrEmpty(
                entries[0].GetProperty("data").GetProperty(RecordingInstance.Key).GetString()));
        }
    }

    [Fact]
    public async Task TheDeletedFileIsKeptAsideWithItsLogStop()
    {
        var (server, client) = await StartAsync(new LocalFileStore(_dir));
        await using (server)
        using (client)
        {
            await Create(client, Sid);
            await Annotate(client, Sid, "secretA");
            await client.DeleteAsync($"/api/sessions/{Sid}");
            Assert.False(File.Exists(Path.Combine(_dir, Sid + ".jsonl")));
            var retired = Directory.GetFiles(_dir, Sid + ".*.jsonl");
            Assert.Single(retired);
            var text = File.ReadAllText(retired[0]);
            Assert.Contains("secretA", text);
            Assert.Contains("log_stop", text);
        }
    }

    [Fact]
    public async Task CreatingOverALiveIdIsAConflict()
    {
        var (server, client) = await StartAsync(new InMemoryStore());
        await using (server)
        using (client)
        {
            Assert.Equal(HttpStatusCode.OK, (await Create(client, Sid)).StatusCode);
            Assert.Equal(HttpStatusCode.Conflict, (await Create(client, Sid)).StatusCode);
        }
    }

    [Fact]
    public async Task SessionIdsAreCappedAndCannotLookRetired()
    {
        var (server, client) = await StartAsync(new InMemoryStore());
        await using (server)
        using (client)
        {
            Assert.Equal(HttpStatusCode.OK, (await Create(client, new string('a', 200))).StatusCode);
            Assert.Equal(HttpStatusCode.UnprocessableEntity, (await Create(client, new string('a', 201))).StatusCode);
            // A "." would let a session's recording path be another's retired file.
            Assert.Equal(HttpStatusCode.UnprocessableEntity, (await Create(client, "s.1791735216837765500-0123abcd")).StatusCode);
        }

        Assert.Null(UtermServer.SessionIdError(new string('a', 200)));
        Assert.NotNull(UtermServer.SessionIdError(new string('端', 67)));
    }

    [Fact]
    public void AConfiguredSessionIdObeysTheSameRule()
    {
        var table = new Tomlyn.Model.TomlTable { ["session_id"] = "a.b" };
        Assert.Throws<ArgumentException>(() => SessionLoader.FromTable(table));
        Assert.Equal("ok_id", SessionLoader.FromTable(new Tomlyn.Model.TomlTable { ["session_id"] = "ok_id" }).SessionId);
    }
}
