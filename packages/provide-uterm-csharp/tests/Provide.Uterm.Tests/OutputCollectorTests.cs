//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using System.Threading.Channels;
using Provide.Uterm.Fanout;

namespace Provide.Uterm.Tests;

/// <summary>
/// Direct tests for the fan-out output collector.
/// </summary>
/// <remarks>
/// The collector was reached only through the controller until the quiesce
/// window was found to be able to outrank output that had already arrived, so
/// the boundary between "quiet" and "cut short" is pinned here rather than
/// inferred from a scenario several layers up.
/// </remarks>
public sealed class OutputCollectorTests
{
    /// <summary>A channel-backed subscription, which is what production reads from.</summary>
    /// <remarks>
    /// Deliberately not a hand-rolled double that ignores its token: a real
    /// reader checks cancellation before its buffer, and that ordering is the
    /// whole subject of these tests.
    /// </remarks>
    private sealed class QueuedSubscription : IFanoutOutputSubscription
    {
        private readonly Channel<FanoutOutputEvent?> _events = Channel.CreateUnbounded<FanoutOutputEvent?>();

        public QueuedSubscription(params FanoutOutputEvent?[] items)
        {
            foreach (var item in items) _events.Writer.TryWrite(item);
        }

        public int Pending => _events.Reader.Count;

        public ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken cancellationToken) =>
            _events.Reader.ReadAsync(cancellationToken);

        public ValueTask DisposeAsync()
        {
            _events.Writer.TryComplete();
            return ValueTask.CompletedTask;
        }
    }

    /// <summary>A member that never stops talking, so its queue never runs dry.</summary>
    private sealed class NeverQuietSubscription : IFanoutOutputSubscription
    {
        private readonly Channel<FanoutOutputEvent?> _events = Channel.CreateUnbounded<FanoutOutputEvent?>();

        // Refilled on the way past rather than from a racing producer thread:
        // what this models is a stream with more to come at every instant, and
        // a background writer would only sometimes manage that.
        public int Pending => Math.Max(1, _events.Reader.Count);

        public ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken cancellationToken)
        {
            _events.Writer.TryWrite(new FanoutOutputEvent("term", "."));
            return _events.Reader.ReadAsync(cancellationToken);
        }

        public ValueTask DisposeAsync()
        {
            _events.Writer.TryComplete();
            return ValueTask.CompletedTask;
        }
    }

    [Fact]
    public async Task Reports_A_Response_That_Was_Still_Arriving_When_The_Budget_Ran_Out()
    {
        // The one thing a caller cannot otherwise tell from a short but
        // complete response: what came back is a prefix, not the answer.
        var (output, _, deadlineExceeded) = await OutputCollector.CollectAsync(
            new NeverQuietSubscription(), quiesceMs: 1, maxResponseMs: 20, CancellationToken.None);

        Assert.NotEqual("", output);
        Assert.True(deadlineExceeded);
    }

    [Fact]
    public async Task Collects_A_Member_That_Answered_Then_Went_Quiet_Under_A_Quiesce_Longer_Than_The_Cap()
    {
        // Such a group cuts EVERY quiesce window short by construction, so
        // treating "the budget ended the wait" as a failure reported every one
        // of its members as failed even though they answered. Go's collector
        // pins this case; the truncation flag is read off what is still queued.
        var subscription = new QueuedSubscription(new FanoutOutputEvent("term", "done"));

        var (output, _, deadlineExceeded) = await OutputCollector.CollectAsync(
            subscription, quiesceMs: 1_000, maxResponseMs: 40, CancellationToken.None);

        Assert.Equal("done", output);
        Assert.False(deadlineExceeded);
    }

    [Fact]
    public async Task Collects_Output_That_Was_Already_Waiting()
    {
        var subscription = new QueuedSubscription(
            new FanoutOutputEvent("term", "imm"), new FanoutOutputEvent("term", "ediate"));

        var (output, _, _) = await OutputCollector.CollectAsync(
            subscription, quiesceMs: 1, maxResponseMs: 5_000, CancellationToken.None);

        Assert.Equal("immediate", output);
    }

    [Fact]
    public async Task Ends_On_Silence_Well_Inside_The_Response_Budget()
    {
        var clock = Stopwatch.StartNew();

        var (output, elapsed, _) = await OutputCollector.CollectAsync(
            new QueuedSubscription(), quiesceMs: 5, maxResponseMs: 4_000, CancellationToken.None);

        // Quiesce ends the collect; the budget is only the ceiling it never reaches.
        Assert.Equal("", output);
        Assert.True(clock.ElapsedMilliseconds < 1_000, $"waited {clock.ElapsedMilliseconds}ms for a 5ms quiesce");
        Assert.True(elapsed < 1_000, $"reported {elapsed}ms for a 5ms quiesce");
    }

    [Fact]
    public async Task Propagates_Cancellation_When_The_Budget_Cuts_The_Read_Short()
    {
        // remaining < quiesceMs: the read never got its window, so the silence
        // does not mean the member quiesced, and the caller is owed the throw.
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => OutputCollector.CollectAsync(
            new QueuedSubscription(), quiesceMs: 1_000, maxResponseMs: 1, CancellationToken.None));
    }

    [Fact]
    public async Task Returns_The_Last_Snapshot_When_No_Term_Output_Arrives()
    {
        var subscription = new QueuedSubscription(
            new FanoutOutputEvent("snapshot", "older"), new FanoutOutputEvent("snapshot", "newest"));

        var (output, _, _) = await OutputCollector.CollectAsync(
            subscription, quiesceMs: 1, maxResponseMs: 5_000, CancellationToken.None);

        Assert.Equal("newest", output);
    }

    [Fact]
    public async Task Prefers_Term_Output_Over_A_Snapshot()
    {
        var subscription = new QueuedSubscription(
            new FanoutOutputEvent("snapshot", "screen"), new FanoutOutputEvent("term", "delta"));

        var (output, _, _) = await OutputCollector.CollectAsync(
            subscription, quiesceMs: 1, maxResponseMs: 5_000, CancellationToken.None);

        Assert.Equal("delta", output);
    }

    [Fact]
    public async Task Stops_Immediately_On_The_Disconnect_Sentinel()
    {
        // A session that has gone is not going to quiesce, so waiting out the
        // window for it is pure latency.
        var subscription = new QueuedSubscription(new FanoutOutputEvent("term", "partial"), null);
        var clock = Stopwatch.StartNew();

        var (output, _, _) = await OutputCollector.CollectAsync(
            subscription, quiesceMs: 4_000, maxResponseMs: 8_000, CancellationToken.None);

        Assert.Equal("partial", output);
        Assert.True(clock.ElapsedMilliseconds < 1_000, $"waited {clock.ElapsedMilliseconds}ms after a disconnect");
    }

    [Fact]
    public async Task Reports_Nothing_For_A_Subscription_That_Never_Yields()
    {
        var (output, _, _) = await OutputCollector.CollectAsync(
            EmptyFanoutOutputSubscription.Instance, quiesceMs: 1, maxResponseMs: 5_000, CancellationToken.None);

        Assert.Equal("", output);
    }
}
