//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using Provide.Uterm.Annotation;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Recording;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Server;

/// <summary>
/// Automatic annotation of a recorded session: the reference's
/// <c>HostedSessionRuntime</c> runs the detector over every snapshot it sends,
/// over streamed <c>term</c> output with escape sequences removed, and over
/// input — each a "read" or "send" scan — and records every match as an
/// <c>annotation</c> event. Parity with
/// <c>provide-uterm-server/tests/server/test_output_annotation.py</c>.
/// </summary>
public sealed class SessionOutputAnnotationTests
{
    private const string Key = "AKIA0123456789AB"; // pragma: allowlist secret

    private static (SessionAnnotator Annotator, List<Dictionary<string, object?>> Recorded) NewAnnotator(
        bool recording = true)
    {
        var recorded = new List<Dictionary<string, object?>>();
        var annotator = new SessionAnnotator(new PatternDetector(), () => recording, data =>
        {
            recorded.Add(data);
            return Task.CompletedTask;
        });
        return (annotator, recorded);
    }

    private static List<string> Descriptions(IEnumerable<Dictionary<string, object?>> recorded) =>
        recorded.Select(d => (string)d["description"]!).ToList();

    [Fact]
    public async Task StreamedOutputIsScannedThroughItsEscapeSequences()
    {
        var (annotator, recorded) = NewAnnotator();

        await annotator.RecordAsync(annotator.ScanOutput($"\x1b[12;5H\x1b[38;2;255;176;0m{Key}\x1b[0m"));

        Assert.Equal(["AWS access key detected in read"], Descriptions(recorded));
    }

    [Fact]
    public async Task AMatchSplitAcrossFramesIsFoundOnce()
    {
        var (annotator, recorded) = NewAnnotator();

        await annotator.RecordAsync(annotator.ScanOutput("DROP TA"));
        await annotator.RecordAsync(annotator.ScanOutput("BLE callers;"));

        Assert.Equal(["SQL DROP statement detected: DROP TABLE"], Descriptions(recorded));
    }

    [Fact]
    public async Task NothingIsScannedWhenNothingIsRecorded()
    {
        var (annotator, recorded) = NewAnnotator(recording: false);

        await annotator.RecordAsync(annotator.ScanOutput(Key));
        await annotator.RecordAsync(annotator.ScanInput(Key));
        await annotator.RecordAsync(annotator.ScanSnapshot(new Dictionary<string, object?> { ["screen"] = Key }));

        Assert.Empty(recorded);
    }

    [Fact]
    public async Task InputAndOutputCarryTheirOwnTails()
    {
        // Half a key typed and the other half printed is not a key: the two
        // directions are separate streams, as the reference keeps them.
        var (annotator, recorded) = NewAnnotator();

        await annotator.RecordAsync(annotator.ScanInput(Key[..8]));
        await annotator.RecordAsync(annotator.ScanOutput(Key[8..]));

        Assert.Empty(recorded);
    }

    [Fact]
    public async Task SnapshotsAndInputAdvanceTheSequenceTheSpanCarries()
    {
        var (annotator, recorded) = NewAnnotator();

        await annotator.RecordAsync(annotator.ScanSnapshot(new Dictionary<string, object?> { ["screen"] = "$ " }));
        await annotator.RecordAsync(annotator.ScanInput("sudo reboot"));
        await annotator.RecordAsync(annotator.ScanOutput("sudo: " + Key));

        Assert.Equal(
            ["sudo command detected: sudo", "reboot command detected: reboot", "AWS access key detected in read",
                "sudo command detected: sudo"],
            Descriptions(recorded));
        Assert.All(recorded, d => Assert.Equal(
            new Dictionary<string, object?> { ["from_seq"] = 2, ["to_seq"] = 2 },
            (Dictionary<string, object?>)d["span"]!));
        Assert.Equal(
            ["label", "description", "severity", "source", "principal", "span"],
            recorded[0].Keys.ToList());
        Assert.Equal("detector", recorded[0]["source"]);
        Assert.Equal("system", recorded[0]["principal"]);
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private static async Task<(UtermServer Server, TermHub Hub, InMemoryStore Store)> StartAsync(
        bool recording, bool redact = true)
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Recording.RedactSensitive = redact;
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "rec1",
            DisplayName = "rec1",
            ConnectorType = "shell",
            Visibility = "public",
            InputMode = InputModes.Open,
            AutoStart = true,
            RecordingEnabled = recording,
        });
        var store = new InMemoryStore();
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Version = "test",
            Clock = clock,
            Recording = store,
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        return (server, hub, store);
    }

    private static async Task<List<Dictionary<string, object?>>> RecordedAnnotationsAsync(InMemoryStore store)
    {
        var entries = await store.GetEntriesAsync("rec1", new Query { Limit = 500, Event = "annotation" });
        return entries.Select(e => (Dictionary<string, object?>)e["data"]!).ToList();
    }

    [Fact]
    public async Task ARecordedSessionRecordsWhatItsInputOutputAndSnapshotsShow()
    {
        var (server, hub, store) = await StartAsync(recording: true);
        await using (server)
        {
            var worker = hub.Registry.Get("rec1")?.WorkerWs;
            Assert.IsType<LocalWorkerLink>(worker);

            // Typed: scanned as "send", then its echo comes back as "term" output.
            await worker.SendTextAsync("export K=" + Key);
            // A snapshot request: the screen now shows the line being typed.
            await worker.SendTextAsync(ControlChannelCodec.EncodeControlFrame(
                new Dictionary<string, object?> { ["type"] = "snapshot_req" }));

            // The snapshot shows the same key the echo already did: the stream
            // recorded it, so the snapshot does not record it again.
            var recorded = await RecordedAnnotationsAsync(store);
            Assert.Equal(
                ["AWS access key detected in send", "AWS access key detected in read"],
                Descriptions(recorded));
            Assert.Equal(
                [2, 2],
                recorded.Select(d => (int)((Dictionary<string, object?>)d["span"]!)["from_seq"]!).ToList());

            // Recording entries only, as the reference logs them: none of them
            // enters the hub's live event ring.
            Assert.DoesNotContain(hub.Registry.Get("rec1")!.Events, e => (string?)e["type"] == "annotation");
        }
    }

    private const string Token = "abcdef1234567890"; // pragma: allowlist secret

    [Fact]
    public async Task AnAutomaticAnnotationIsRedactedInTheStoreAndKeptOutOfTheEventRing()
    {
        // "curl HTTP request detected: {match}" embeds what was typed, token and all.
        var (server, hub, store) = await StartAsync(recording: true);
        await using (server)
        {
            var worker = hub.Registry.Get("rec1")!.WorkerWs!;

            await worker.SendTextAsync($"curl -H token={Token} https://example.com\r");

            var recorded = await RecordedAnnotationsAsync(store);
            var curl = recorded.Where(d => ((string)d["description"]!).StartsWith("curl HTTP", StringComparison.Ordinal))
                .ToList();
            Assert.NotEmpty(curl);
            Assert.All(curl, d => Assert.Equal(
                "curl HTTP request detected: curl -H [TOKEN_REDACTED] https://", (string)d["description"]!));
            Assert.All(recorded, d => Assert.DoesNotContain(Token, (string)d["description"]!));
            Assert.All(recorded, d => Assert.Equal("detector", d["source"]));

            // Each is a whole recording entry: timestamped and stamped with its session.
            var entries = await store.GetEntriesAsync("rec1", new Query { Limit = 500, Event = "annotation" });
            Assert.All(entries, e =>
            {
                Assert.IsType<double>(e["ts"]);
                Assert.True((double)e["ts"]! > 0);
                Assert.Equal("rec1", e["session_id"]);
            });

            Assert.DoesNotContain(hub.Registry.Get("rec1")!.Events, e => (string?)e["type"] == "annotation");
            Assert.DoesNotContain(
                hub.Registry.Get("rec1")!.Events,
                e => System.Text.Json.JsonSerializer.Serialize(e).Contains("curl HTTP", StringComparison.Ordinal));
        }
    }

    [Fact]
    public async Task WithRedactionOffAnAutomaticAnnotationIsRecordedAsDetected()
    {
        var (server, hub, store) = await StartAsync(recording: true, redact: false);
        await using (server)
        {
            var worker = hub.Registry.Get("rec1")!.WorkerWs!;

            await worker.SendTextAsync($"curl -H token={Token} https://example.com\r");

            var recorded = await RecordedAnnotationsAsync(store);
            Assert.Contains(recorded, d => (string)d["description"]! == $"curl HTTP request detected: curl -H token={Token} https://");
        }
    }

    [Fact]
    public async Task OnlyAnnotationsAreRecordedSoTypedInputNeverIs()
    {
        // The reference records keystrokes and masks those typed at a password
        // prompt (checked on stripped text). This port records no keystrokes at
        // all, so a password typed after a styled "Password:" prompt cannot
        // reach the store in clear: every entry is a detector annotation.
        var (server, hub, store) = await StartAsync(recording: true);
        await using (server)
        {
            var worker = hub.Registry.Get("rec1")!.WorkerWs!;

            await worker.SendTextAsync("hunter2-not-a-command\r");

            var entries = await store.GetEntriesAsync("rec1", new Query { Limit = 500 });
            Assert.All(entries, e => Assert.Equal("annotation", e["event"]));
            Assert.All(entries, e => Assert.DoesNotContain(
                "hunter2", System.Text.Json.JsonSerializer.Serialize(e), StringComparison.Ordinal));
        }
    }

    [Fact]
    public async Task AnUnrecordedSessionRecordsNoAnnotations()
    {
        var (server, hub, store) = await StartAsync(recording: false);
        await using (server)
        {
            var worker = hub.Registry.Get("rec1")!.WorkerWs!;

            await worker.SendTextAsync("export K=" + Key);

            Assert.Empty(await RecordedAnnotationsAsync(store));
        }
    }
}
