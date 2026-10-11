//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>Small hub arms: event-deque trimming, expired REST sessions, drop accounting.</summary>
public sealed class Coverage100HubSmallTests
{
    private sealed class OkWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) => Task.CompletedTask;
    }

    [Fact]
    public void Event_deque_keeps_only_the_newest_events_and_tracks_the_minimum_seq()
    {
        var hub = new TermHub(new TermHubConfig { Clock = new ManualClock(), EventDequeMaxlen = 2 });
        Assert.True(hub.Conn.RegisterWorker("w", new OkWorker()));
        for (var i = 0; i < 3; i++)
        {
            hub.Router.AppendEvent("w", "term", new Dictionary<string, object?> { ["data"] = i.ToString() });
        }

        var st = hub.Registry.Get("w")!;
        Assert.Equal(2, st.Events.Count);
        Assert.Equal(2, st.MinEventSeq);
    }

    [Fact]
    public void Hijack_state_for_an_unknown_worker_is_unowned()
    {
        var hub = new TermHub(new TermHubConfig { Clock = new ManualClock() });
        var msg = hub.Router.HijackStateMsgFor("ghost", ws: null);
        Assert.Equal(false, msg["hijacked"]);
    }

    [Fact]
    public async Task Rest_session_lookup_returns_nothing_once_the_lease_has_expired()
    {
        var clock = new ManualClock(100);
        clock.SetMonotonic(0);
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        Assert.True(hub.Conn.RegisterWorker("w", new OkWorker()));
        Assert.True((await hub.Lease.TryAcquireRestAsync("w", "op", 5, "hid", clock.Monotonic())).Ok);
        Assert.NotNull(hub.GetRestSession("w", "hid"));
        clock.SetMonotonic(10);
        Assert.Null(hub.GetRestSession("w", "hid"));
    }

    [Fact]
    public void A_subscriber_whose_queue_was_closed_counts_both_dropped_events()
    {
        var metrics = new List<string>();
        var bus = new EventBus(maxQueueDepth: 1, onMetric: (name, _) => metrics.Add(name));
        var (sub, unsubscribe) = bus.Watch("w");
        bus.Enqueue("w", new Dictionary<string, object?> { ["type"] = "term" });
        // A completed queue refuses writes but still holds its last event: the
        // drop-oldest retry removes it and the write is refused again.
        sub.Channel.Writer.TryComplete();
        bus.Enqueue("w", new Dictionary<string, object?> { ["type"] = "term" });
        Assert.Equal(2, sub.Dropped);
        Assert.Equal(2, metrics.Count(m => m == "event_bus_subscriber_drop_total"));
        unsubscribe();
    }

    [Fact]
    public void Manual_clock_step_and_worker_not_found_carry_their_values()
    {
        var clock = new ManualClock { Step = 0.25 };
        Assert.Equal(0.25, clock.Step);
        Assert.Equal("w9", new WorkerNotFoundException("w9").WorkerId);
    }
}
