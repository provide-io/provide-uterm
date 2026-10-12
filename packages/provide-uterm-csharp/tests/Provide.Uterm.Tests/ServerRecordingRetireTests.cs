//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Reflection;
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
    internal const string Sid = "pam-suokki-capture-4242-777";
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

    internal static async Task<(UtermServer Server, HttpClient Client)> StartAsync(
        IRecordingStore store, Func<ISessionRegistry, ISessionRegistry>? wrapRegistry = null)
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
            Registry = (wrapRegistry ?? (r => r))(new InMemorySessionRegistry(cfg.Sessions)),
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

    internal static Task<HttpResponseMessage> Create(HttpClient client, string id) =>
        client.PostAsync("/api/sessions", new StringContent(
            JsonSerializer.Serialize(new { session_id = id, connector_type = "shell" }), Encoding.UTF8, "application/json"));

    internal static async Task Annotate(HttpClient client, string id, string label)
    {
        var resp = await client.PostAsync($"/api/sessions/{id}/annotate", new StringContent(
            JsonSerializer.Serialize(new { label }), Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.OK, resp.StatusCode);
    }

    internal static async Task<JsonElement> Entries(HttpClient client, string id)
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

    /// <summary>An in-memory store whose retire and end can be held up.</summary>
    internal sealed class GatedStore(IRecordingStore? inner = null) : IRecordingStore
    {
        private readonly IRecordingStore _inner = inner ?? new InMemoryStore();

        /// <summary>When set, the next append waits for it before it is written.</summary>
        public TaskCompletionSource? HoldNextAppend { get; set; }

        /// <summary>When set, the next start waits for it before it is written.</summary>
        public TaskCompletionSource? HoldNextStart { get; set; }

        /// <summary>Completes when a held write (start or append) is waiting.</summary>
        public TaskCompletionSource AppendHeld { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource RetireEntered { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource? HoldRetire { get; set; }
        public bool EndHangs { get; set; }
        public TimeSpan RetireDelay { get; set; }

        public async Task StartSessionAsync(string sessionId, IReadOnlyDictionary<string, object?> metadata)
        {
            if (HoldNextStart is { } hold)
            {
                HoldNextStart = null;
                AppendHeld.TrySetResult();
                await hold.Task;
            }

            await _inner.StartSessionAsync(sessionId, metadata);
        }

        public async Task AppendEventsAsync(string sessionId, IReadOnlyList<Event> events)
        {
            if (HoldNextAppend is { } hold)
            {
                HoldNextAppend = null;
                AppendHeld.TrySetResult();
                await hold.Task;
            }

            await _inner.AppendEventsAsync(sessionId, events);
        }

        public Task EndSessionAsync(string sessionId) =>
            EndHangs ? new TaskCompletionSource().Task : _inner.EndSessionAsync(sessionId);

        public Task<Meta> RecordingMetaAsync(string sessionId) => _inner.RecordingMetaAsync(sessionId);

        public Task<IReadOnlyList<Event>> GetEntriesAsync(string sessionId, Query query) =>
            _inner.GetEntriesAsync(sessionId, query);

        public Task<string> GetPathAsync(string sessionId) => _inner.GetPathAsync(sessionId);

        public async Task RetireSessionAsync(string sessionId)
        {
            if (HoldRetire is { } hold)
            {
                RetireEntered.TrySetResult();
                await hold.Task;
            }

            if (RetireDelay > TimeSpan.Zero)
            {
                await Task.Delay(RetireDelay);
            }

            await _inner.RetireSessionAsync(sessionId);
        }
    }

    [Fact]
    public async Task ACreateWaitsForAnIdBeingDeleted()
    {
        // A tty-named PAM id is reused by the next login on the tty while the
        // last one's session is still being torn down.
        var store = new GatedStore();
        var (server, client) = await StartAsync(store);
        await using (server)
        using (client)
        {
            await Create(client, Sid);
            await Annotate(client, Sid, "secretA");
            store.HoldRetire = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var deleting = client.DeleteAsync($"/api/sessions/{Sid}");
            await store.RetireEntered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            var creating = Create(client, Sid);
            Assert.NotSame(creating, await Task.WhenAny(creating, Task.Delay(100)));
            var hold = store.HoldRetire;
            store.HoldRetire = null;
            hold.SetResult();
            Assert.Equal(HttpStatusCode.OK, (await deleting).StatusCode);
            Assert.Equal(HttpStatusCode.OK, (await creating).StatusCode);
            Assert.Equal(0, (await Entries(client, Sid)).GetArrayLength());
        }
    }

    [Fact]
    public async Task AWriteForADeletedSessionWritesNothing()
    {
        // A detector annotation from a worker's late output can arrive after its
        // session is gone; nothing of it reaches the store.
        var store = new InMemoryStore();
        var (server, client) = await StartAsync(store);
        await using (server)
        using (client)
        {
            await Create(client, Sid);
            await Annotate(client, Sid, "secretA");
            await client.DeleteAsync($"/api/sessions/{Sid}");
            var append = typeof(UtermServer).GetMethod(
                "AppendSessionRecordingAsync", BindingFlags.Instance | BindingFlags.NonPublic)!;
            await (Task)append.Invoke(server, [Sid, new[] { new Event { ["ts"] = 1.0, ["event"] = "annotation" } }])!;
            Assert.False((await store.RecordingMetaAsync(Sid)).Exists);
        }
    }

    /// <summary>A registry whose Delete waits for the test before it removes anything.</summary>
    private sealed class HeldDeleteRegistry(ISessionRegistry inner) : ISessionRegistry
    {
        public TaskCompletionSource DeleteEntered { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public ManualResetEventSlim ReleaseDelete { get; } = new(false);

        public bool TryGetDefinition(string sessionId, out SessionDefinition definition) =>
            inner.TryGetDefinition(sessionId, out definition);

        public bool TryGetStatus(string sessionId, out SessionStatus status) => inner.TryGetStatus(sessionId, out status);
        public IReadOnlyList<SessionItem> ListWithDefinitions() => inner.ListWithDefinitions();
        public SessionDefinition Upsert(SessionDefinition def) => inner.Upsert(def);

        public bool Delete(string sessionId)
        {
            DeleteEntered.TrySetResult();
            ReleaseDelete.Wait();
            return inner.Delete(sessionId);
        }

        public SessionStatus? StartSession(string sessionId) => inner.StartSession(sessionId);
        public SessionStatus? StopSession(string sessionId) => inner.StopSession(sessionId);
        public SessionStatus? RestartSession(string sessionId) => inner.RestartSession(sessionId);
        public SessionStatus? ClearSession(string sessionId) => inner.ClearSession(sessionId);
        public SessionStatus? SetMode(string sessionId, string inputMode) => inner.SetMode(sessionId, inputMode);

        public SessionStatus? PatchSession(string sessionId, string? displayName, string? visibility, IReadOnlyList<string>? tags) =>
            inner.PatchSession(sessionId, displayName, visibility, tags);
    }

    [Fact]
    public async Task ASecondDeleteLeavesTheTeardownToTheFirst()
    {
        HeldDeleteRegistry? registry = null;
        var (server, client) = await StartAsync(new InMemoryStore(), inner => registry = new HeldDeleteRegistry(inner));
        await using (server)
        using (client)
        {
            await Create(client, Sid);
            var first = client.DeleteAsync($"/api/sessions/{Sid}");
            try
            {
                await registry!.DeleteEntered.Task.WaitAsync(TimeSpan.FromSeconds(5)); // the first holds the id
                Assert.Equal(HttpStatusCode.OK, (await client.DeleteAsync($"/api/sessions/{Sid}")).StatusCode);
                Assert.False(first.IsCompleted);
                Assert.True(registry.TryGetDefinition(Sid, out _)); // the second deleted nothing
            }
            finally
            {
                registry!.ReleaseDelete.Set();
            }

            Assert.Equal(HttpStatusCode.OK, (await first).StatusCode);
            Assert.False(registry.TryGetDefinition(Sid, out _));
        }
    }

    [Fact]
    public async Task ConcurrentCreatesOfOneIdSeeAlreadyExists()
    {
        // The relay retries an open: the losers must get "already exists".
        var (server, client) = await StartAsync(new GatedStore { RetireDelay = TimeSpan.FromMilliseconds(50) });
        await using (server)
        using (client)
        {
            var responses = await Task.WhenAll(Enumerable.Range(0, 3).Select(_ => Create(client, Sid)));
            Assert.Single(responses, r => r.StatusCode == HttpStatusCode.OK);
            var conflicts = responses.Where(r => r.StatusCode == HttpStatusCode.Conflict).ToList();
            Assert.Equal(2, conflicts.Count);
            foreach (var conflict in conflicts)
            {
                Assert.Contains("already exists", await conflict.Content.ReadAsStringAsync());
            }
        }
    }
}

/// <summary>Tests that shorten <see cref="UtermServer.RemovalStopTimeout"/>, a process-wide static: run alone.</summary>
[CollectionDefinition(nameof(RecordingTimeoutCollection), DisableParallelization = true)]
public sealed class RecordingTimeoutCollection;

[Collection(nameof(RecordingTimeoutCollection))]
public sealed class ServerRecordingTimeoutTests : IDisposable
{
    private const string Sid = ServerRecordingRetireTests.Sid;
    private readonly TimeSpan _saved = UtermServer.RemovalStopTimeout;
    private readonly TimeSpan _savedWait = UtermServer.ReservedIdWait;
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "uterm-rec-timeout-" + Guid.NewGuid().ToString("N"));

    public ServerRecordingTimeoutTests() => UtermServer.RemovalStopTimeout = TimeSpan.FromMilliseconds(100);

    public void Dispose()
    {
        UtermServer.RemovalStopTimeout = _saved;
        UtermServer.ReservedIdWait = _savedWait;
        if (Directory.Exists(_dir))
        {
            Directory.Delete(_dir, recursive: true);
        }
    }

    private static Task<(UtermServer Server, HttpClient Client)> StartAsync(IRecordingStore store) =>
        ServerRecordingRetireTests.StartAsync(store);

    private static Task<HttpResponseMessage> Create(HttpClient client, string id) =>
        ServerRecordingRetireTests.Create(client, id);

    private static Task Annotate(HttpClient client, string id, string label) =>
        ServerRecordingRetireTests.Annotate(client, id, label);

    private static Task<JsonElement> Entries(HttpClient client, string id) =>
        ServerRecordingRetireTests.Entries(client, id);

    [Fact]
    public async Task ACreateThatOutwaitsTheBoundIsRefused()
    {
        // Only a teardown that outlasts the wait is "being deleted".
        UtermServer.ReservedIdWait = TimeSpan.Zero;
        var store = new ServerRecordingRetireTests.GatedStore();
        var (server, client) = await StartAsync(store);
        await using (server)
        using (client)
        {
            await Create(client, Sid);
            store.HoldRetire = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var deleting = client.DeleteAsync($"/api/sessions/{Sid}");
            await store.RetireEntered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            var refused = await Create(client, Sid);
            Assert.Equal(HttpStatusCode.Conflict, refused.StatusCode);
            Assert.Contains("being deleted", await refused.Content.ReadAsStringAsync());
            var hold = store.HoldRetire;
            store.HoldRetire = null;
            hold.SetResult();
            Assert.Equal(HttpStatusCode.OK, (await deleting).StatusCode);
        }
    }

    [Fact]
    public async Task AHungEndStillRetiresAndReleasesTheId()
    {
        var store = new ServerRecordingRetireTests.GatedStore();
        var (server, client) = await StartAsync(store);
        await using (server)
        using (client)
        {
            await Create(client, Sid);
            await Annotate(client, Sid, "secretA");
            store.EndHangs = true;
            var deleted = await client.DeleteAsync($"/api/sessions/{Sid}").WaitAsync(TimeSpan.FromSeconds(5));
            Assert.Equal(HttpStatusCode.OK, deleted.StatusCode);
            Assert.Equal(HttpStatusCode.OK, (await Create(client, Sid)).StatusCode);
            Assert.Equal(0, (await Entries(client, Sid)).GetArrayLength());
        }
    }

    /// <summary>
    /// A write stuck in the store past the bound: the delete's gate wait times
    /// out, the id is retired, released and created again, and only then does
    /// the old write land. The new holder's recording must have none of it --
    /// whether the old write was stuck starting the recording (it would then
    /// record its instance for the id, and the new holder would append into
    /// the old file) or appending to one already started.
    /// </summary>
    [Theory]
    [InlineData("file", true)]
    [InlineData("memory", true)]
    [InlineData("file", false)]
    [InlineData("memory", false)]
    public async Task AWriteThatOutlivesItsSessionDoesNotReachTheNextHolder(string kind, bool stuckStarting)
    {
        var store = new ServerRecordingRetireTests.GatedStore(
            kind == "file" ? new LocalFileStore(_dir) : new InMemoryStore());
        var (server, client) = await StartAsync(store);
        await using (server)
        using (client)
        {
            await Create(client, Sid);
            var hold = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            if (stuckStarting)
            {
                store.HoldNextStart = hold;
            }
            else
            {
                await Annotate(client, Sid, "secret0");
                store.HoldNextAppend = hold;
            }

            var stuck = client.PostAsync($"/api/sessions/{Sid}/annotate", new StringContent(
                JsonSerializer.Serialize(new { label = "secretA" }), Encoding.UTF8, "application/json"));
            await store.AppendHeld.Task.WaitAsync(TimeSpan.FromSeconds(5)); // the write holds the gate

            // The delete's wait on the gate times out: it retires and releases anyway.
            Assert.Equal(HttpStatusCode.OK, (await client.DeleteAsync($"/api/sessions/{Sid}")).StatusCode);
            Assert.Equal(HttpStatusCode.OK, (await Create(client, Sid)).StatusCode);

            hold.SetResult(); // the old write lands now
            await stuck;
            await Annotate(client, Sid, "secretB");
            var entries = (await Entries(client, Sid)).GetRawText();
            Assert.DoesNotContain("secretA", entries);
            Assert.DoesNotContain("secret0", entries);
            Assert.Contains("secretB", entries);
        }
    }
}
