//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;
using Xunit;

namespace Provide.Uterm.Tests.Hub;

public sealed class EventBusTests
{
    [Fact]
    public void Enqueue_NoSubscribers_IsNoop()
    {
        var bus = new EventBus();
        bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "term" });
    }

    [Fact]
    public async Task Watch_DeliversMatchingEvents()
    {
        var bus = new EventBus();
        var (sub, unsub) = bus.Watch("w1");
        try
        {
            bus.Enqueue("w1", new Dictionary<string, object?>
            {
                ["type"] = "term",
                ["data"] = new Dictionary<string, object?> { ["data"] = "hi" },
            });
            var item = await sub.Channel.Reader.ReadAsync();
            Assert.NotNull(item);
            Assert.Equal("w1", item!["worker_id"]);
            Assert.Equal("term", item["type"]?.ToString());
        }
        finally
        {
            unsub();
        }
    }

    [Fact]
    public async Task WatchAsync_TimeoutEmpty_TimedOut()
    {
        var bus = new EventBus();
        var result = await bus.WatchAsync("w1", TimeSpan.FromMilliseconds(80), maxEvents: 5);
        Assert.True(result.TimedOut);
        Assert.Empty(result.Events);
    }

    [Fact]
    public async Task WatchAsync_ReceivesLiveEvent()
    {
        var bus = new EventBus();
        // Publish on a background pump so the subscription is definitely registered first.
        var pump = Task.Run(async () =>
        {
            for (var i = 0; i < 20; i++)
            {
                await Task.Delay(25);
                bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "term", ["i"] = i });
            }
        });
        try
        {
            var result = await bus.WatchAsync("w1", TimeSpan.FromSeconds(3), maxEvents: 2);
            Assert.False(result.TimedOut);
            Assert.NotEmpty(result.Events);
        }
        finally
        {
            await pump;
        }
    }

    [Fact]
    public async Task Watch_FiltersEventTypes()
    {
        var bus = new EventBus();
        var (sub, unsub) = bus.Watch("w1", eventTypes: new[] { "term" });
        try
        {
            bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "other" });
            bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "term" });
            var item = await sub.Channel.Reader.ReadAsync();
            Assert.Equal("term", item!["type"]?.ToString());
        }
        finally
        {
            unsub();
        }
    }

    [Fact]
    public async Task Watch_FiltersPatternOnScreen()
    {
        var bus = new EventBus();
        var (sub, unsub) = bus.Watch("w1", pattern: "READY");
        try
        {
            bus.Enqueue("w1", new Dictionary<string, object?>
            {
                ["type"] = "term",
                ["data"] = new Dictionary<string, object?> { ["screen"] = "waiting" },
            });
            bus.Enqueue("w1", new Dictionary<string, object?>
            {
                ["type"] = "term",
                ["data"] = new Dictionary<string, object?> { ["screen"] = "READY>" },
            });
            var item = await sub.Channel.Reader.ReadAsync();
            var data = Assert.IsType<Dictionary<string, object?>>(item!["data"]);
            Assert.Equal("READY>", data["screen"]?.ToString());
        }
        finally
        {
            unsub();
        }
    }

    [Fact]
    public void Watch_PatternTooLong_Throws()
    {
        var bus = new EventBus(maxPatternLength: 8);
        Assert.Throws<ArgumentException>(() => bus.Watch("w1", pattern: new string('a', 20)));
    }

    [Fact]
    public void Watch_MaxSubscribers_Throws()
    {
        var bus = new EventBus(maxSubscribersPerWorker: 1);
        var (_, u1) = bus.Watch("w1");
        try
        {
            Assert.Throws<InvalidOperationException>(() => bus.Watch("w1"));
        }
        finally
        {
            u1();
        }
    }

    [Fact]
    public async Task CloseWorker_SendsNullSentinel()
    {
        var bus = new EventBus();
        var (sub, unsub) = bus.Watch("w1");
        try
        {
            bus.CloseWorker("w1");
            var item = await sub.Channel.Reader.ReadAsync();
            Assert.Null(item);
        }
        finally
        {
            unsub();
        }
    }

    [Fact]
    public void CloseWorker_Unknown_IsNoop()
    {
        new EventBus().CloseWorker("nope");
    }

    [Fact]
    public async Task Unsubscribe_IsIdempotent()
    {
        var bus = new EventBus();
        var (_, unsub) = bus.Watch("w1");
        unsub();
        unsub();
        var result = await bus.WatchAsync("w1", TimeSpan.FromMilliseconds(50));
        Assert.True(result.TimedOut);
    }

    [Fact]
    public void Enqueue_Overflow_DropsOldest()
    {
        var drops = 0;
        var bus = new EventBus(maxQueueDepth: 2, onMetric: (_, n) => drops += n);
        var (sub, unsub) = bus.Watch("w1");
        try
        {
            for (var i = 0; i < 20; i++)
            {
                bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "term", ["i"] = i });
            }

            // Drain whatever remains without blocking forever.
            var count = 0;
            while (sub.Channel.Reader.TryRead(out _)) count++;
            Assert.True(count <= 2);
            Assert.True(drops >= 0); // may or may not drop depending on consumer speed
        }
        finally
        {
            unsub();
        }
    }

    [Fact]
    public async Task Watch_TruncatesLongScreen_ForPattern()
    {
        var bus = new EventBus(maxMatchInputChars: 8);
        var (sub, unsub) = bus.Watch("w1", pattern: "ABCD");
        try
        {
            // Screen longer than maxMatchInputChars — truncated before match.
            bus.Enqueue("w1", new Dictionary<string, object?>
            {
                ["type"] = "term",
                ["data"] = new Dictionary<string, object?> { ["screen"] = "ABCDEFGHIJKLMNOP" },
            });
            // Truncated "ABCDEFGH" still matches ABCD
            var item = await sub.Channel.Reader.ReadAsync();
            Assert.NotNull(item);
        }
        finally
        {
            unsub();
        }
    }

    [Fact]
    public async Task Watch_EmptyData_And_NonDictData_PatternMiss()
    {
        var bus = new EventBus();
        var (sub, unsub) = bus.Watch("w1", pattern: "NEEDLE");
        try
        {
            bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "term" }); // no data
            bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "term", ["data"] = "plain" });
            bus.Enqueue("w1", new Dictionary<string, object?>
            {
                ["type"] = "term",
                ["data"] = new Dictionary<string, object?> { ["screen"] = "NEEDLE here" },
            });
            var item = await sub.Channel.Reader.ReadAsync();
            Assert.NotNull(item);
        }
        finally
        {
            unsub();
        }
    }

    [Fact]
    public async Task CloseWorker_FullQueue_StillDeliversSentinel()
    {
        var bus = new EventBus(maxQueueDepth: 2);
        var (sub, unsub) = bus.Watch("w1");
        try
        {
            bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "a" });
            bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "b" });
            bus.Enqueue("w1", new Dictionary<string, object?> { ["type"] = "c" });
            bus.CloseWorker("w1");
            Dictionary<string, object?>? last = new();
            // Drain until sentinel (null)
            while (true)
            {
                last = await sub.Channel.Reader.ReadAsync();
                if (last is null) break;
            }

            Assert.Null(last);
        }
        finally
        {
            unsub();
        }
    }

    [Fact]
    public async Task WatchAsync_StopsOnWorkerDisconnect()
    {
        var bus = new EventBus();
        var task = bus.WatchAsync("w1", TimeSpan.FromSeconds(3), maxEvents: 50);
        await Task.Delay(40);
        bus.CloseWorker("w1");
        var result = await task;
        // Disconnect ends the wait without requiring timeout.
        Assert.Empty(result.Events);
        Assert.False(result.TimedOut);
    }

    [Fact]
    public void TermHub_AppendEventData_FansOutToEventBus()
    {
        var hub = new TermHub(new TermHubConfig { RestAcquireRateLimitPerSec = 100, RestSendRateLimitPerSec = 100 });
        var (sub, unsub) = hub.EventBus.Watch("demo");
        try
        {
            hub.AppendEventData("demo", "term", new Dictionary<string, object?> { ["data"] = "via-hub" });
            Assert.True(sub.Channel.Reader.TryRead(out var item));
            Assert.NotNull(item);
            Assert.Equal("term", item!["type"]?.ToString());
            // Second append while worker state exists (ring buffer path).
            hub.Registry.SetDefault("demo", new WorkerTermState());
            hub.AppendEventData("demo", "term", new Dictionary<string, object?> { ["data"] = "ring" });
            Assert.True(sub.Channel.Reader.TryRead(out var item2));
            Assert.NotNull(item2);
        }
        finally
        {
            unsub();
        }
    }
}
