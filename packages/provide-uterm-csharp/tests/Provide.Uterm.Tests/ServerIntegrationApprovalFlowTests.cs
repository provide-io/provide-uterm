//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// The browser-input approval path end to end: a real browser WebSocket types,
/// a policy gate holds the keystroke, an admin decides over REST, and the held
/// command reaches (or never reaches) the worker. Mirrors Go
/// <c>server/approvals_deck_test.go</c>.
///
/// The submitting browser and the deciding admin are different principals
/// (<c>test-admin</c> from UTERM_TEST_MODE vs the dev token's <c>admin</c>), so
/// the two-person control does not refuse every approve as a self-approval.
///
/// Runs in the ~ServerIntegration gate batch.
/// </summary>
[Collection("UTERM_TEST_MODE")]
public sealed class ServerIntegrationApprovalFlowTests
{
    private const string WorkerId = "demo";

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private sealed record Fixture(
        UtermServer Server,
        HttpClient Http,
        string BaseUrl,
        TermHub Hub,
        RecordingWorkerWs Worker) : IAsyncDisposable
    {
        public async ValueTask DisposeAsync()
        {
            Http.Dispose();
            await Server.DisposeAsync();
        }
    }

    private static async Task<Fixture> StartAsync(
        IInputPolicyGate? gate = null,
        TimeSpan? sweepInterval = null,
        TermHub? existingHub = null)
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Environment = "development";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = WorkerId,
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
            // Otherwise the boot loop spawns a real shell connector and registers
            // it as the worker, replacing the recorder these tests assert on.
            AutoStart = false,
        });
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "approval-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });

        var hub = existingHub ?? new TermHub(new TermHubConfig { PolicyGate = gate });
        var worker = new RecordingWorkerWs();
        Assert.True(hub.Conn.RegisterWorker(WorkerId, worker));
        var apiKeys = new ApiKeyStore();
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, apiKeys),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            ApiKeys = apiKeys,
            Version = "approval-flow",
            ApprovalSweepInterval = sweepInterval,
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        return new Fixture(server, http, server.BaseAddress!, hub, worker);
    }

    /// <summary>A browser WebSocket that owns the session's input.</summary>
    private sealed class OwningBrowser : IDisposable
    {
        private readonly ClientWebSocket _ws = new();
        private readonly ControlFrameDecoder _decoder = new();
        private readonly Queue<Dictionary<string, object?>> _pending = new();
        private readonly byte[] _buffer = new byte[65536];

        public static async Task<OwningBrowser> ConnectAsync(string baseUrl, CancellationToken ct)
        {
            var browser = new OwningBrowser();
            var uri = new Uri(baseUrl.Replace("http://", "ws://", StringComparison.Ordinal)
                + $"/ws/browser/{WorkerId}/term");
            await browser._ws.ConnectAsync(uri, ct);
            await browser.WaitAsync("hello", ct);
            await browser.SendControlAsync(new Dictionary<string, object?> { ["type"] = "hijack_request" }, ct);
            await browser.WaitWhereAsync(
                "hijack_state",
                f => f.TryGetValue("owner", out var o) && o?.ToString() == "me",
                ct);
            return browser;
        }

        public Task SendControlAsync(Dictionary<string, object?> msg, CancellationToken ct) =>
            _ws.SendAsync(
                Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(msg)),
                WebSocketMessageType.Text,
                true,
                ct);

        public Task SendInputAsync(string text, CancellationToken ct) =>
            _ws.SendAsync(Encoding.UTF8.GetBytes(text), WebSocketMessageType.Text, true, ct);

        public Task<Dictionary<string, object?>> WaitAsync(string type, CancellationToken ct) =>
            WaitWhereAsync(type, _ => true, ct);

        public async Task<Dictionary<string, object?>> WaitWhereAsync(
            string type,
            Func<Dictionary<string, object?>, bool> predicate,
            CancellationToken ct)
        {
            while (true)
            {
                while (_pending.Count > 0)
                {
                    var frame = _pending.Dequeue();
                    if (frame.TryGetValue("type", out var t) && t?.ToString() == type && predicate(frame))
                    {
                        return frame;
                    }
                }

                var result = await _ws.ReceiveAsync(_buffer, ct);
                var text = Encoding.UTF8.GetString(_buffer, 0, result.Count);
                foreach (var chunk in _decoder.Feed(text))
                {
                    if (chunk is ControlChunk ctrl) _pending.Enqueue(ctrl.Control);
                }
            }
        }

        // Dispose aborts rather than closing politely: a close handshake would
        // wait on a server this test is about to stop anyway.
        public void Dispose() => _ws.Dispose();
    }

    private static string? Text(Dictionary<string, object?> frame, string key) =>
        frame.TryGetValue(key, out var v) ? v?.ToString() : null;

    private static CancellationTokenSource Deadline() => new(TimeSpan.FromSeconds(20));

    [Fact]
    public async Task HeldInput_IsApprovedOverRestAndReachesTheWorker()
    {
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        await using var fx = await StartAsync(new Hub.HoldGate());
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync("whoami\n", cts.Token);
        var pending = await browser.WaitAsync("approval_pending", cts.Token);
        var requestId = Text(pending, "request_id");

        Assert.False(string.IsNullOrEmpty(requestId));
        Assert.Equal("whoami\n", Text(pending, "command"));
        Assert.Empty(fx.Worker.Inputs); // held, not forwarded

        var approve = await fx.Http.PostAsync($"/api/approvals/{requestId}/approve", null);

        Assert.Equal(HttpStatusCode.OK, approve.StatusCode);
        var resolved = await browser.WaitWhereAsync(
            "approval_resolved", f => Text(f, "request_id") == requestId, cts.Token);
        Assert.Equal("approved", Text(resolved, "outcome"));
        Assert.Equal(["whoami\n"], fx.Worker.Inputs);

        // A second decision on a settled request is not pending.
        var again = await fx.Http.PostAsync($"/api/approvals/{requestId}/approve", null);
        Assert.Equal(HttpStatusCode.BadRequest, again.StatusCode);
    }

    [Fact]
    public async Task HeldInput_IsRejectedWithTheRedBannerAndNeverReachesTheWorker()
    {
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        await using var fx = await StartAsync(new Hub.HoldGate());
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync("shutdown\n", cts.Token);
        var requestId = Text(await browser.WaitAsync("approval_pending", cts.Token), "request_id");

        var reject = await fx.Http.PostAsync($"/api/approvals/{requestId}/reject?reason=too-risky", null);

        Assert.Equal(HttpStatusCode.OK, reject.StatusCode);
        var banner = await browser.WaitWhereAsync(
            "term",
            f => Text(f, "data")?.Contains("[REJECTED]", StringComparison.Ordinal) is true,
            cts.Token);
        Assert.Contains("Command 'shutdown' blocked by Admin.", Text(banner, "data")!, StringComparison.Ordinal);
        Assert.Contains("Reason: too-risky", Text(banner, "data")!, StringComparison.Ordinal);
        var resolved = await browser.WaitWhereAsync(
            "approval_resolved", f => Text(f, "request_id") == requestId, cts.Token);
        Assert.Equal("rejected", Text(resolved, "outcome"));
        Assert.Empty(fx.Worker.Inputs);
    }

    [Fact]
    public async Task AParkedBrowsersFurtherKeystrokesAreBufferedAndReplayedOnApproval()
    {
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        await using var fx = await StartAsync(new Hub.HoldGate());
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync("base\n", cts.Token);
        var requestId = Text(await browser.WaitAsync("approval_pending", cts.Token), "request_id");

        // Buffered, so no second approval is raised for it.
        await browser.SendInputAsync("more\n", cts.Token);
        // Over MaxBufferChars: refused with an error, and the buffer is untouched.
        await browser.SendInputAsync(new string('z', 41000), cts.Token);
        var error = await browser.WaitAsync("error", cts.Token);
        Assert.Equal("Input too long.", Text(error, "message"));

        Assert.Equal(HttpStatusCode.OK,
            (await fx.Http.PostAsync($"/api/approvals/{requestId}/approve", null)).StatusCode);

        await browser.WaitWhereAsync("approval_resolved", f => Text(f, "request_id") == requestId, cts.Token);
        Assert.Equal(["base\n", "more\n"], fx.Worker.Inputs);
    }

    [Fact]
    public async Task ApprovingAfterTheSubmitterReleasedTheLeaseIsAConflict()
    {
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        await using var fx = await StartAsync(new Hub.HoldGate());
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync("base\n", cts.Token);
        var requestId = Text(await browser.WaitAsync("approval_pending", cts.Token), "request_id");
        await browser.SendInputAsync("buffered\n", cts.Token);
        await browser.SendControlAsync(new Dictionary<string, object?> { ["type"] = "hijack_release" }, cts.Token);
        await browser.SendControlAsync(new Dictionary<string, object?> { ["type"] = "ping" }, cts.Token);
        await browser.WaitAsync("pong", cts.Token);

        var approve = await fx.Http.PostAsync($"/api/approvals/{requestId}/approve", null);

        Assert.Equal(HttpStatusCode.Conflict, approve.StatusCode);
        Assert.Contains(
            "ownership is no longer valid",
            await approve.Content.ReadAsStringAsync(cts.Token),
            StringComparison.Ordinal);
        Assert.Empty(fx.Worker.Inputs);
        Assert.Equal(ApprovalStatus.Refused, fx.Hub.Approvals.Get(requestId!)!.Status);
    }

    [Fact]
    public async Task ADenyingGateAnswersWithAnErrorAndForwardsNothing()
    {
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        await using var fx = await StartAsync(new Hub.DenyGate());
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync("rm\n", cts.Token);

        var error = await browser.WaitAsync("error", cts.Token);
        Assert.Contains("blocked by policy", Text(error, "message")!, StringComparison.Ordinal);
        Assert.Empty(fx.Worker.Inputs);
    }

    [Fact]
    public async Task AnOversizedInputIsRefusedBeforeTheGateSeesIt()
    {
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        var gate = new CountingHoldGate();
        await using var fx = await StartAsync(gate);
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync(new string('x', 11000), cts.Token);

        var error = await browser.WaitAsync("error", cts.Token);
        Assert.Equal("Input too long.", Text(error, "message"));
        Assert.Equal(0, gate.Calls);
        Assert.Empty(fx.Worker.Inputs);
    }

    [Fact]
    public async Task AGateThatCannotDecideDropsTheInputAndLeavesTheSocketUsable()
    {
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        await using var fx = await StartAsync(new FailingGate());
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync("x\n", cts.Token);
        await browser.SendControlAsync(new Dictionary<string, object?> { ["type"] = "ping" }, cts.Token);

        await browser.WaitAsync("pong", cts.Token);
        Assert.Empty(fx.Worker.Inputs);
    }

    [Fact]
    public async Task AHoldIsRefusedWhenTheLeaseMovesWhileTheGateIsDeciding()
    {
        // The window the re-validation inside ParkBrowserForApprovalAsync exists
        // to close: parking a browser that may no longer type would hold its
        // keystrokes for a decision that could never be carried out.
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        var gate = new LeaseStealingGate();
        var logs = new List<string>();
        var hub = new TermHub(new TermHubConfig
        {
            PolicyGate = gate,
            OnLog = (level, message) => { lock (logs) logs.Add(message); },
        });
        gate.Hub = hub;
        await using var fx = await StartAsync(existingHub: hub);
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync("cmd\n", cts.Token);
        await browser.SendControlAsync(new Dictionary<string, object?> { ["type"] = "ping" }, cts.Token);
        await browser.WaitAsync("pong", cts.Token);

        Assert.Empty(hub.Approvals.PendingApprovals());
        Assert.Empty(fx.Worker.Inputs);
        lock (logs)
        {
            Assert.Contains(logs, m =>
                m.Contains("park_for_approval_failed", StringComparison.Ordinal)
                && m.Contains(ApprovalParkReasons.OwnershipInvalid, StringComparison.Ordinal));
        }
    }

    [Fact]
    public async Task AGateThatAllowsForwardsAtTheFencedGeneration()
    {
        // The allow arm is not the no-op fast path: the policy context is built,
        // the gate decides, and only then is the keystroke forwarded — at the
        // generation the fence captured, so a lease that moved mid-decision
        // cannot be typed at.
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        await using var fx = await StartAsync(new Hub.AllowGate());
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync("id\n", cts.Token);

        await WaitForInputsAsync(fx, cts.Token);
        Assert.Equal(["id\n"], fx.Worker.Inputs);
        Assert.Empty(fx.Hub.Approvals.PendingApprovals());
    }

    private static async Task WaitForInputsAsync(Fixture fx, CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(10);
        while (fx.Worker.Inputs.Count == 0 && DateTime.UtcNow < deadline)
        {
            await Task.Delay(20, ct);
        }
    }

    [Fact]
    public async Task WithNoGateConfiguredInputStillGoesStraightToTheWorker()
    {
        // The default path every existing deployment is on: no policy context is
        // built, no approval is raised, the keystroke is forwarded.
        using var testMode = new EnvironmentVariableScope("UTERM_TEST_MODE", "1");
        using var cts = Deadline();
        await using var fx = await StartAsync();
        using var browser = await OwningBrowser.ConnectAsync(fx.BaseUrl, cts.Token);

        await browser.SendInputAsync("ls\n", cts.Token);

        await WaitForInputsAsync(fx, cts.Token);
        Assert.Equal(["ls\n"], fx.Worker.Inputs);
        Assert.Empty(fx.Hub.Approvals.PendingApprovals());
    }

    [Fact]
    public async Task TheSweepRetiresAnApprovalNobodyDecidedAndReleasesItsBrowser()
    {
        var hub = new TermHub(new TermHubConfig { PolicyGate = new Hub.HoldGate() });
        var browser = new RecordingWorkerWs();
        await using var fx = await StartAsync(
            sweepInterval: TimeSpan.FromMilliseconds(25), existingHub: hub);
        hub.Conn.RegisterBrowser(WorkerId, browser, "admin", principalSubjectId: "submitter");
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);
        Assert.True(hub.Lease.TryAcquireWs(WorkerId, browser).Ok);
        var parked = await hub.ParkBrowserForApprovalAsync(
            WorkerId, browser, "cmd\n", new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 0 });
        Assert.NotNull(parked.RequestId);
        Assert.True(hub.IsBrowserParked(browser));

        // Only the sweep touches the store here, so an unparked browser is the
        // sweep's doing and nothing else's.
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(10);
        while (hub.IsBrowserParked(browser) && DateTime.UtcNow < deadline)
        {
            await Task.Delay(10);
        }

        Assert.False(hub.IsBrowserParked(browser));
        Assert.Equal(ApprovalStatus.Timeout, hub.Approvals.Get(parked.RequestId!)!.Status);
    }

    [Fact]
    public async Task AZeroSweepIntervalDisablesTheSweep()
    {
        var hub = new TermHub(new TermHubConfig { PolicyGate = new Hub.HoldGate() });
        var browser = new RecordingWorkerWs();
        await using var fx = await StartAsync(sweepInterval: TimeSpan.Zero, existingHub: hub);
        hub.Conn.RegisterBrowser(WorkerId, browser, "admin", principalSubjectId: "submitter");
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);
        Assert.True(hub.Lease.TryAcquireWs(WorkerId, browser).Ok);
        await hub.ParkBrowserForApprovalAsync(
            WorkerId, browser, "cmd\n", new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 0 });

        await Task.Delay(150);

        Assert.True(hub.IsBrowserParked(browser));
    }

    /// <summary>Records raw terminal input, ignoring lease control frames.</summary>
    private sealed class RecordingWorkerWs : IWorkerWs
    {
        private readonly List<string> _sent = new();

        public IReadOnlyList<string> Inputs
        {
            get
            {
                lock (_sent) return _sent.Where(p => !ControlChannelCodec.IsControlFrame(p)).ToArray();
            }
        }

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            lock (_sent) _sent.Add(payload);
            return Task.CompletedTask;
        }
    }

    private sealed class CountingHoldGate : IInputPolicyGate
    {
        private int _calls;

        public int Calls => Volatile.Read(ref _calls);

        public Task<PolicyDecision> InterceptInputAsync(
            string data,
            PolicyContext context,
            CancellationToken cancellationToken = default)
        {
            Interlocked.Increment(ref _calls);
            return Task.FromResult(new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 60 });
        }
    }

    /// <summary>Moves the dashboard lease out from under the submitter mid-decision.</summary>
    private sealed class LeaseStealingGate : IInputPolicyGate
    {
        public TermHub Hub { get; set; } = null!;

        public async Task<PolicyDecision> InterceptInputAsync(
            string data,
            PolicyContext context,
            CancellationToken cancellationToken = default)
        {
            await Hub.Lease.ForceReleaseAsync(context.WorkerId, cancellationToken);
            return new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 60 };
        }
    }

    private sealed class FailingGate : IInputPolicyGate
    {
        public Task<PolicyDecision> InterceptInputAsync(
            string data,
            PolicyContext context,
            CancellationToken cancellationToken = default) =>
            Task.FromException<PolicyDecision>(new TimeoutException("governance service unreachable"));
    }
}
