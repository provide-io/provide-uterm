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
}
