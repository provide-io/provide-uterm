//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

public class HubServicesTests
{
    private sealed class FakeWorker : IWorkerWs
    {
        public List<string> Sent { get; } = new();

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            Sent.Add(payload);
            return Task.CompletedTask;
        }
    }

    private static TermHub NewHub(ManualClock clock) =>
        new(new TermHubConfig
        {
            Clock = clock,
            MaxWorkers = 100,
            EventDequeMaxlen = 10,
            MaxEventDataChars = 256,
            RestAcquireRateLimitPerSec = 1000,
            RestSendRateLimitPerSec = 1000,
        });

    [Fact]
    public void Registry_PutGetPopDiscard()
    {
        var reg = new WorkerRegistry();
        Assert.Equal(0, reg.Count);
        var st = new WorkerTermState();
        reg.Put("w1", st);
        Assert.True(reg.Contains("w1"));
        Assert.Same(st, reg.Get("w1"));
        Assert.Same(st, reg.Require("w1"));
        Assert.Equal(new[] { "w1" }, reg.Keys());
        Assert.Single(reg.All());

        var existing = reg.SetDefault("w1", new WorkerTermState());
        Assert.Same(st, existing);
        var created = reg.SetDefault("w2", new WorkerTermState());
        Assert.Same(created, reg.Get("w2"));

        Assert.Same(st, reg.Pop("w1"));
        Assert.Null(reg.Pop("missing"));
        Assert.True(reg.Discard("w2"));
        Assert.False(reg.Discard("w2"));
        Assert.Throws<WorkerNotFoundException>(() => reg.Require("gone"));
    }

    [Fact]
    public void Router_AppendEvents_And_InputMode()
    {
        var clock = new ManualClock(100);
        clock.SetMonotonic(1);
        var hub = NewHub(clock);
        var worker = new FakeWorker();
        Assert.True(hub.Conn.RegisterWorker("w1", worker));

        var e1 = hub.AppendEventData("w1", "term", new Dictionary<string, object?> { ["data"] = "hello" });
        Assert.Equal(1, Convert.ToInt32(e1["seq"]));
        Assert.Equal("term", e1["type"]?.ToString());

        // Cap term data at MaxEventDataChars (clamped to at least 256)
        var longData = new string('x', 500);
        var e2 = hub.Router.AppendEvent("w1", "term", new Dictionary<string, object?> { ["data"] = longData });
        var data = Assert.IsType<Dictionary<string, object?>>(e2["data"]);
        Assert.Equal(256, ((string)data["data"]!).Length);

        var recent = hub.Router.GetRecentEvents("w1", 10);
        Assert.True(recent.Count >= 2);

        var (ok, reason) = hub.Router.SetInputMode("w1", "open");
        Assert.True(ok, reason);
        Assert.Equal(InputModes.Open, hub.Registry.Get("w1")!.InputMode);
        Assert.False(hub.Router.SetInputMode("w1", "bogus").Ok);
        Assert.False(hub.Router.SetInputMode("missing", "hijack").Ok);

        var missing = hub.Router.AppendEvent("nope", "x", null);
        Assert.Equal(0, Convert.ToInt32(missing["seq"]));
    }

    [Fact]
    public void Presence_And_BrowserRegistration()
    {
        var clock = new ManualClock(50);
        clock.SetMonotonic(5);
        var hub = NewHub(clock);
        var worker = new FakeWorker();
        hub.Conn.RegisterWorker("w1", worker);

        var browser = new object();
        var reg = hub.Conn.RegisterBrowser("w1", browser, "operator");
        Assert.Equal(true, reg["worker_online"]);
        Assert.Equal("operator", reg["role"]?.ToString());

        var snap = hub.Presence.RegisterBrowserStateSnapshot("w1", browser);
        Assert.Equal(true, snap["worker_online"]);
        Assert.False(hub.Presence.CanSendInput(hub.Registry.Get("w1")!, browser)); // hijack mode, not owner

        hub.Router.SetInputMode("w1", InputModes.Open);
        Assert.True(hub.Presence.CanSendInput(hub.Registry.Get("w1")!, browser));

        var offline = hub.Presence.RegisterBrowserStateSnapshot("missing", browser);
        Assert.Equal(false, offline["worker_online"]);

        hub.Conn.CleanupBrowser("w1", browser);
    }

    [Fact]
    public async Task Lease_Acquire_Conflict_And_Cleanup()
    {
        var clock = new ManualClock(1000);
        clock.SetMonotonic(10);
        var hub = NewHub(clock);
        var worker = new FakeWorker();
        hub.Conn.RegisterWorker("w1", worker);

        var (ok, reason) = await hub.TryAcquireRestHijackAsync("w1", "operator", 30, "h1", 10);
        Assert.True(ok, reason);

        var (ok2, reason2) = await hub.TryAcquireRestHijackAsync("w1", "other", 30, "h2", 11);
        Assert.False(ok2);
        Assert.Equal("already_hijacked", reason2);

        var st = hub.Router.HijackStateMsgFor("w1", null);
        Assert.Equal(true, st["hijacked"]);

        clock.SetMonotonic(1000);
        var (browserExpired, restExpired) = hub.CleanupExpiredHijack("w1");
        Assert.True(restExpired || browserExpired || hub.GetRestSession("w1", "h1") is null);

        Assert.Equal(0, hub.Shutdown());
    }

    [Fact]
    public void Conn_Deregister_Disconnect_ForceRelease()
    {
        var clock = new ManualClock(1);
        clock.SetMonotonic(1);
        var hub = NewHub(clock);
        var worker = new FakeWorker();
        hub.Conn.RegisterWorker("w1", worker);
        Assert.True(hub.Conn.IsActiveWorker("w1", worker));

        hub.Conn.UpdateLastSnapshot("w1", new Dictionary<string, object?> { ["screen"] = "x" });
        Assert.Equal("x", hub.Registry.Get("w1")!.LastSnapshot!["screen"]?.ToString());

        var (should, wasH) = hub.Conn.DeregisterWorker("w1", worker);
        Assert.True(should);
        Assert.False(wasH);

        hub.Conn.RegisterWorker("w1", worker);
        Assert.True(hub.Conn.DisconnectWorker("w1"));
        Assert.False(hub.Conn.DisconnectWorker("w1"));

        hub.Conn.RegisterWorker("w1", worker);
        Assert.False(hub.Conn.ForceReleaseHijack("w1")); // nothing held
    }

    [Fact]
    public void Limiter_And_TokenBucket()
    {
        var clock = new ManualClock(0);
        clock.SetMonotonic(0);
        var lim = new RateLimiter(100, 100, clock);
        Assert.True(lim.AllowRestAcquire("c1"));
        Assert.True(lim.AllowRestSend("c1"));
        Assert.Equal(100, lim.AcquireRate);
        Assert.Equal(100, lim.SendRate);

        var bucket = new TokenBucket(10, 1, clock);
        Assert.True(bucket.Allow());
        // burst exhausted at t=0
        Assert.False(bucket.Allow());
        clock.SetMonotonic(1);
        Assert.True(bucket.Allow());
    }

    [Fact]
    public void Lease_ClampDashboard()
    {
        Assert.Equal(1, HijackLeaseManager.ClampDashboardLease(0));
        Assert.Equal(600, HijackLeaseManager.ClampDashboardLease(9999));
        Assert.Equal(45, HijackLeaseManager.ClampDashboardLease(45));

        var pause = HijackLeaseManager.PauseFrame("op", "h1", 1.0);
        Assert.Equal("pause", pause["action"]?.ToString());
        var resume = HijackLeaseManager.ResumeFrame("op", 2.0);
        Assert.Equal("resume", resume["action"]?.ToString());
    }

    [Fact]
    public void Store_ClampLease_And_Buffer()
    {
        Assert.Equal(1, StateStore.ClampLease(0));
        Assert.Equal(14400, StateStore.ClampLease(99999));
        Assert.Equal(999, StateStore.ClampLease(999));

        var clock = new ManualClock(1);
        var hub = NewHub(clock);
        var st = hub.State.GetOrCreate("w1");
        Assert.NotNull(st);
        hub.State.TouchActivity("w1");
        hub.State.Metric("m", 2);
        hub.State.NotifyHijackChanged("w1", true, "op");

        var ws = new object();
        var (cmd, ok) = hub.State.BufferAndGetCommand(ws, "echo hi\n");
        Assert.True(ok);
        Assert.Contains("echo hi", cmd, StringComparison.Ordinal);
    }

    [Fact]
    public void RateLimits_OnHubFacade()
    {
        var clock = new ManualClock(0);
        clock.SetMonotonic(0);
        var hub = NewHub(clock);
        Assert.True(hub.AllowRestAcquireFor("x"));
        Assert.True(hub.AllowRestSendFor("x"));
    }

    [Fact]
    public async Task Presence_RequestSnapshot_And_Analysis()
    {
        var clock = new ManualClock(1);
        clock.SetMonotonic(1);
        var hub = NewHub(clock);
        var worker = new FakeWorker();
        hub.Conn.RegisterWorker("w1", worker);
        await hub.Presence.RequestSnapshotAsync("w1");
        await hub.Presence.RequestAnalysisAsync("w1");
        Assert.True(worker.Sent.Count >= 2);
    }

    [Fact]
    public void Router_PruneIfIdle()
    {
        var clock = new ManualClock(1);
        var hub = NewHub(clock);
        hub.State.GetOrCreate("idle");
        hub.Router.PruneIfIdle("idle");
        Assert.False(hub.Registry.Contains("idle"));
    }

    [Fact]
    public void Clock_Real_And_Manual()
    {
        var real = new RealClock();
        Assert.True(real.Wall() > 0);
        Assert.True(real.Monotonic() >= 0);
        real.SleepAsync(0).GetAwaiter().GetResult();

        var m = new ManualClock(42);
        m.SetMonotonic(3);
        m.SetWall(99);
        m.Step = 2;
        m.SleepAsync(1).GetAwaiter().GetResult();
        Assert.Equal(5, m.Monotonic());
        Assert.Equal(99, m.Wall());
        Assert.Equal(new[] { 1.0 }, m.Sleeps());
    }
}
