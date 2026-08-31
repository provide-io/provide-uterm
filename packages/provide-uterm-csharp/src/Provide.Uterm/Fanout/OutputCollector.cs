//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using System.Text;

namespace Provide.Uterm.Fanout;

public sealed record FanoutOutputEvent(string Type, string Text);

public interface IFanoutOutputSubscription : IAsyncDisposable
{
    ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct);

    /// <summary>Events already buffered and not yet handed back by <see cref="ReadAsync"/>.</summary>
    /// <remarks>
    /// Required rather than defaulted to zero: it is the only signal that
    /// separates a member still talking from one that finished, and a
    /// subscription allowed to stay silent about it would have every truncated
    /// response reported as complete. Every bus this models has it -- a channel
    /// reader's Count here, a queue length in Go, qsize in Python.
    /// </remarks>
    int Pending { get; }
}

internal sealed class EmptyFanoutOutputSubscription : IFanoutOutputSubscription
{
    internal static readonly EmptyFanoutOutputSubscription Instance = new();
    public ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct) => ValueTask.FromResult<FanoutOutputEvent?>(null);
    public int Pending => 0;
    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

internal static class OutputCollector
{
    internal static async Task<(string Output, int ElapsedMs, bool DeadlineExceeded)> CollectAsync(
        IFanoutOutputSubscription subscription,
        int quiesceMs,
        int maxResponseMs,
        CancellationToken ct)
    {
        var clock = Stopwatch.StartNew();
        var term = new StringBuilder();
        var snapshot = "";
        quiesceMs = Math.Max(1, quiesceMs);
        maxResponseMs = Math.Max(1, maxResponseMs);

        while (clock.ElapsedMilliseconds < maxResponseMs)
        {
            var remaining = maxResponseMs - (int)clock.ElapsedMilliseconds;
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            FanoutOutputEvent? item;
            try
            {
                // Start the read before arming the window, and arm it only if
                // the read did not already have output to hand back. A reader
                // checks its token before its buffer -- ChannelReader.ReadAsync
                // does exactly that -- so arming first means an expired window
                // can throw away output that was sitting there the whole time,
                // and the collector reports a silence that never happened.
                //
                // Output that is already available has not been quiet for any
                // length of time, so no deadline should be able to outrank it.
                // Only a read that must actually wait gets a window.
                var read = subscription.ReadAsync(timeout.Token);
                if (!read.IsCompleted) timeout.CancelAfter(Math.Min(quiesceMs, remaining));
                item = await read.ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                // Which of the two bounds cut the read decides what the silence
                // meant, and the timers cannot answer that: when the budget is
                // the tighter one this linked source and ct are set for the same
                // instant, so whichever fired first used to decide whether the
                // member was reported as collected or as failed. Read off the
                // bounds themselves instead.
                //
                // A read that got its full quiesce window and heard nothing has
                // quiesced, which is a collected — if empty — response. One cut
                // short by what was left of the response budget has not: the
                // caller is owed the cancellation, because a member starved of
                // budget by the members ahead of it did not answer.
                //
                // Only when it said nothing at all, though. A member that
                // answered and then went quiet HAS answered, whatever the
                // budget did to a window it no longer needed. A group whose
                // quiesce is longer than its cap cuts every window short by
                // construction, and throwing there reported every one of its
                // members as failed. Go's collector pins exactly that case.
                if (remaining < quiesceMs && term.Length == 0 && snapshot.Length == 0) throw;
                break;
            }

            if (item is null) break;
            if (string.Equals(item.Type, "term", StringComparison.Ordinal))
            {
                term.Append(item.Text);
            }
            else if (string.Equals(item.Type, "snapshot", StringComparison.Ordinal))
            {
                snapshot = item.Text;
            }
        }

        // Truncated means we stopped with more still queued -- the member had
        // not finished talking. Deriving it from what is LEFT, rather than from
        // which exit fired, is what makes it reliable: the loop reaches its own
        // budget condition only if an event happens to land in the final
        // moments, so keying on that lost the truncation whenever the producer
        // stalled near the end.
        var deadlineExceeded = subscription.Pending > 0;

        return (term.Length > 0 ? term.ToString() : snapshot, (int)clock.ElapsedMilliseconds, deadlineExceeded);
    }
}
