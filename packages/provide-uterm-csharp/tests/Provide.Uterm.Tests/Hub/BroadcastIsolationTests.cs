//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

public sealed class BroadcastIsolationTests
{
    [Fact]
    public async Task Broadcast_HealthySocketProceedsWhileTimedOutSocketIsPruned()
    {
        var hub = new TermHub(new TermHubConfig
        {
            BrowserSendTimeout = TimeSpan.FromMilliseconds(40),
        });
        var blocked = new BlockingSocket();
        var healthy = new CaptureSocket();
        hub.Conn.RegisterBrowser("w", blocked, "viewer");
        hub.Conn.RegisterBrowser("w", healthy, "viewer");

        var watch = Stopwatch.StartNew();
        using var safety = new CancellationTokenSource(TimeSpan.FromSeconds(1));
        await hub.Conn.BroadcastToBrowsersAsync(
            "w", new Dictionary<string, object?> { ["type"] = "term", ["data"] = "ok" }, safety.Token);

        Assert.True(watch.Elapsed < TimeSpan.FromMilliseconds(500), $"broadcast took {watch.Elapsed}");
        Assert.Single(healthy.Messages);
        Assert.DoesNotContain(blocked, hub.Registry.Get("w")!.Browsers.Keys);
        Assert.Contains(healthy, hub.Registry.Get("w")!.Browsers.Keys);
    }

    [Fact]
    public async Task HijackBroadcast_AlsoTimesOutAndPrunesIndependently()
    {
        var hub = new TermHub(new TermHubConfig
        {
            BrowserSendTimeout = TimeSpan.FromMilliseconds(40),
        });
        var blocked = new BlockingSocket();
        var healthy = new CaptureSocket();
        hub.Conn.RegisterBrowser("w", blocked, "viewer");
        hub.Conn.RegisterBrowser("w", healthy, "viewer");

        using var safety = new CancellationTokenSource(TimeSpan.FromSeconds(1));
        await hub.Conn.BroadcastHijackStateAsync("w", safety.Token);

        Assert.Single(healthy.Messages);
        Assert.DoesNotContain(blocked, hub.Registry.Get("w")!.Browsers.Keys);
    }

    [Fact]
    public async Task BroadcastTimeoutDoesNotDependOnSocketHonoringCancellation()
    {
        var hub = new TermHub(new TermHubConfig
        {
            BrowserSendTimeout = TimeSpan.FromMilliseconds(40),
        });
        var broken = new CancellationIgnoringSocket();
        hub.Conn.RegisterBrowser("w", broken, "viewer");

        var broadcast = hub.Conn.BroadcastToBrowsersAsync(
            "w", new Dictionary<string, object?> { ["type"] = "term" });
        var completed = await Task.WhenAny(broadcast, Task.Delay(TimeSpan.FromMilliseconds(500)));

        Assert.Same(broadcast, completed);
        await broadcast;
        Assert.DoesNotContain(broken, hub.Registry.Get("w")!.Browsers.Keys);
    }

    [Fact]
    public async Task CallerCancellationPropagatesWithoutPruningHealthyPeers()
    {
        var hub = new TermHub();
        var first = new CaptureSocket();
        var second = new CaptureSocket();
        hub.Conn.RegisterBrowser("w", first, "viewer");
        hub.Conn.RegisterBrowser("w", second, "viewer");
        using var cancelled = new CancellationTokenSource();
        cancelled.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            hub.Conn.BroadcastToBrowsersAsync(
                "w", new Dictionary<string, object?> { ["type"] = "term" }, cancelled.Token));

        Assert.Contains(first, hub.Registry.Get("w")!.Browsers.Keys);
        Assert.Contains(second, hub.Registry.Get("w")!.Browsers.Keys);
    }

    [Fact]
    public async Task TimedOutPeerIsAbortedAndItsEventualFaultIsContained()
    {
        var hub = new TermHub(new TermHubConfig
        {
            BrowserSendTimeout = TimeSpan.FromMilliseconds(40),
        });
        var broken = new AbortFaultSocket();
        hub.Conn.RegisterBrowser("w", broken, "viewer");

        await hub.Conn.BroadcastToBrowsersAsync(
            "w", new Dictionary<string, object?> { ["type"] = "term" });

        Assert.True(broken.Aborted);
        Assert.True(broken.SendTask.IsFaulted);
        Assert.DoesNotContain(broken, hub.Registry.Get("w")!.Browsers.Keys);
    }

    [Fact]
    public async Task TimedOutOwnerCleanupPreservesResumeGenerationForSocketHandler()
    {
        var hub = new TermHub(new TermHubConfig
        {
            BrowserSendTimeout = TimeSpan.FromMilliseconds(40),
        });
        var worker = new CaptureSocket();
        var owner = new AbortFaultSocket();
        hub.Conn.RegisterWorker("w", worker);
        hub.Conn.RegisterBrowser("w", owner, "admin");
        Assert.True(hub.Lease.TryAcquireWs("w", owner).Ok);
        var ownershipVersion = hub.Registry.Get("w")!.HijackOwnershipVersion;

        await hub.Conn.BroadcastToBrowsersAsync(
            "w", new Dictionary<string, object?> { ["type"] = "term" });

        Assert.Equal(ownershipVersion, hub.Conn.CleanupBrowser("w", owner));
    }

    private sealed class CaptureSocket : IWorkerWs
    {
        public List<string> Messages { get; } = new();

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            cancellationToken.ThrowIfCancellationRequested();
            Messages.Add(payload);
            return Task.CompletedTask;
        }
    }

    private sealed class BlockingSocket : IWorkerWs
    {
        public async Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
    }

    private sealed class CancellationIgnoringSocket : IWorkerWs
    {
        private readonly TaskCompletionSource _never = new(
            TaskCreationOptions.RunContinuationsAsynchronously);

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            _never.Task;
    }

    private sealed class AbortFaultSocket : IAbortableBrowserWs
    {
        private readonly TaskCompletionSource _send = new(
            TaskCreationOptions.RunContinuationsAsynchronously);

        public bool Aborted { get; private set; }
        public bool IsActive => !Aborted;
        public Task SendTask => _send.Task;

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            _send.Task;

        public void Abort()
        {
            Aborted = true;
            _send.TrySetException(new IOException("transport aborted"));
        }
    }
}
