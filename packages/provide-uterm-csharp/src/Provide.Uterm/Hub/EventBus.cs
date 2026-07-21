//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;
using System.Threading.Channels;

namespace Provide.Uterm.Hub;

/// <summary>
/// Real-time event fanout (Python/Go EventBus port). Subscribers open a watch;
/// <see cref="Enqueue"/> delivers non-blocking with drop-oldest overflow.
/// </summary>
public sealed class EventBus
{
    private readonly int _maxQueueDepth;
    private readonly int _maxSubscribersPerWorker;
    private readonly int _maxPatternLength;
    private readonly int _maxMatchInputChars;
    private readonly Action<string, int>? _onMetric;
    private readonly object _gate = new();
    private readonly Dictionary<string, List<Subscription>> _subs = new(StringComparer.Ordinal);
    private long _nextId;

    public EventBus(
        int maxQueueDepth = 500,
        int maxSubscribersPerWorker = 100,
        int maxPatternLength = 512,
        int maxMatchInputChars = 8192,
        Action<string, int>? onMetric = null)
    {
        _maxQueueDepth = Math.Max(1, maxQueueDepth);
        _maxSubscribersPerWorker = Math.Max(1, maxSubscribersPerWorker);
        _maxPatternLength = Math.Max(1, maxPatternLength);
        _maxMatchInputChars = Math.Max(1, maxMatchInputChars);
        _onMetric = onMetric;
    }

    /// <summary>Deliver <paramref name="event"/> to all subscribers for <paramref name="workerId"/>.</summary>
    public void Enqueue(string workerId, Dictionary<string, object?> @event)
    {
        List<Subscription> targets;
        lock (_gate)
        {
            if (!_subs.TryGetValue(workerId, out var list) || list.Count == 0)
            {
                return;
            }

            targets = list.ToList();
        }

        foreach (var sub in targets)
        {
            Deliver(sub, workerId, @event);
        }
    }

    /// <summary>Signal end-of-stream to all watchers of <paramref name="workerId"/>.</summary>
    public void CloseWorker(string workerId)
    {
        List<Subscription> list;
        lock (_gate)
        {
            if (!_subs.Remove(workerId, out list!))
            {
                return;
            }
        }

        foreach (var sub in list)
        {
            PutSentinel(sub);
        }
    }

    /// <summary>
    /// Register a subscription. Returns the sub and an unsubscribe action.
    /// A null event on the channel is the worker-disconnected sentinel.
    /// </summary>
    public (Subscription Sub, Action Unsubscribe) Watch(
        string workerId,
        IReadOnlyCollection<string>? eventTypes = null,
        string? pattern = null)
    {
        lock (_gate)
        {
            if (_subs.TryGetValue(workerId, out var existing) && existing.Count >= _maxSubscribersPerWorker)
            {
                throw new InvalidOperationException(
                    $"EventBus: max subscribers ({_maxSubscribersPerWorker}) reached for worker '{workerId}'");
            }
        }

        Regex? compiled = null;
        if (!string.IsNullOrEmpty(pattern))
        {
            if (pattern.Length > _maxPatternLength)
            {
                throw new ArgumentException($"pattern longer than {_maxPatternLength}");
            }

            compiled = new Regex(pattern, RegexOptions.Compiled | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        }

        HashSet<string>? typeSet = null;
        if (eventTypes is not null)
        {
            typeSet = new HashSet<string>(eventTypes, StringComparer.Ordinal);
        }

        var channel = Channel.CreateBounded<Dictionary<string, object?>?>(new BoundedChannelOptions(_maxQueueDepth)
        {
            FullMode = BoundedChannelFullMode.Wait,
            SingleReader = false,
            SingleWriter = false,
        });

        var sub = new Subscription
        {
            Id = Interlocked.Increment(ref _nextId).ToString("x"),
            WorkerId = workerId,
            Channel = channel,
            EventTypes = typeSet,
            Pattern = compiled,
        };

        lock (_gate)
        {
            if (!_subs.TryGetValue(workerId, out var list))
            {
                list = new List<Subscription>();
                _subs[workerId] = list;
            }

            list.Add(sub);
        }

        var once = new object();
        var removed = false;
        void Unsubscribe()
        {
            lock (once)
            {
                if (removed) return;
                removed = true;
            }

            Remove(sub);
        }

        return (sub, Unsubscribe);
    }

    /// <summary>
    /// Long-poll: collect up to <paramref name="maxEvents"/> for <paramref name="workerId"/>
    /// until <paramref name="timeout"/> elapses or the worker disconnects.
    /// </summary>
    public async Task<WatchResult> WatchAsync(
        string workerId,
        TimeSpan timeout,
        int maxEvents = 50,
        IReadOnlyCollection<string>? eventTypes = null,
        string? pattern = null,
        CancellationToken cancellationToken = default)
    {
        maxEvents = Math.Clamp(maxEvents, 1, 200);
        var (sub, unsub) = Watch(workerId, eventTypes, pattern);
        try
        {
            var collected = new List<Dictionary<string, object?>>();
            var timedOut = false;
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            linked.CancelAfter(timeout);
            while (collected.Count < maxEvents)
            {
                Dictionary<string, object?>? item;
                try
                {
                    item = await sub.Channel.Reader.ReadAsync(linked.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
                {
                    timedOut = true;
                    break;
                }

                if (item is null)
                {
                    break; // worker disconnected
                }

                collected.Add(item);
            }

            return new WatchResult(collected, sub.Dropped, timedOut);
        }
        finally
        {
            unsub();
        }
    }

    private void Remove(Subscription sub)
    {
        lock (_gate)
        {
            if (_subs.TryGetValue(sub.WorkerId, out var list))
            {
                list.Remove(sub);
                if (list.Count == 0)
                {
                    _subs.Remove(sub.WorkerId);
                }
            }
        }

        sub.Channel.Writer.TryComplete();
    }

    private void Deliver(Subscription sub, string workerId, Dictionary<string, object?> @event)
    {
        if (sub.EventTypes is not null)
        {
            var t = @event.TryGetValue("type", out var tv) ? tv?.ToString() ?? "" : "";
            if (!sub.EventTypes.Contains(t))
            {
                return;
            }
        }

        if (sub.Pattern is not null)
        {
            var screen = ExtractScreen(@event);
            if (screen.Length > _maxMatchInputChars)
            {
                screen = screen[.._maxMatchInputChars];
            }

            if (!sub.Pattern.IsMatch(screen))
            {
                return;
            }
        }

        var item = new Dictionary<string, object?>(@event) { ["worker_id"] = workerId };
        if (sub.Channel.Writer.TryWrite(item))
        {
            return;
        }

        // Drop-oldest then write (bounded channel FullMode=Wait may still be full under contention).
        if (sub.Channel.Reader.TryRead(out _))
        {
            Interlocked.Increment(ref sub.Dropped);
            _onMetric?.Invoke("event_bus_subscriber_drop_total", 1);
        }

        if (!sub.Channel.Writer.TryWrite(item))
        {
            Interlocked.Increment(ref sub.Dropped);
            _onMetric?.Invoke("event_bus_subscriber_drop_total", 1);
        }
    }

    private void PutSentinel(Subscription sub)
    {
        // Prefer immediate write; if full, drop one and retry once.
        if (sub.Channel.Writer.TryWrite(null))
        {
            return;
        }

        if (sub.Channel.Reader.TryRead(out _))
        {
            Interlocked.Increment(ref sub.Dropped);
            _onMetric?.Invoke("event_bus_subscriber_drop_total", 1);
        }

        sub.Channel.Writer.TryWrite(null);
    }

    private static string ExtractScreen(Dictionary<string, object?> @event)
    {
        if (!@event.TryGetValue("data", out var data) || data is null)
        {
            return "";
        }

        if (data is Dictionary<string, object?> map && map.TryGetValue("screen", out var sv))
        {
            return sv?.ToString() ?? "";
        }

        return "";
    }

    public sealed class Subscription
    {
        public required string Id { get; init; }
        public required string WorkerId { get; init; }
        public required Channel<Dictionary<string, object?>?> Channel { get; init; }
        public HashSet<string>? EventTypes { get; init; }
        public Regex? Pattern { get; init; }
        public int Dropped;
    }

    public readonly record struct WatchResult(
        IReadOnlyList<Dictionary<string, object?>> Events,
        int DroppedCount,
        bool TimedOut);
}
