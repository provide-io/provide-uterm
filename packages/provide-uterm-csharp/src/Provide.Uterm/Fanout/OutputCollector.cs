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
}

internal sealed class EmptyFanoutOutputSubscription : IFanoutOutputSubscription
{
    internal static readonly EmptyFanoutOutputSubscription Instance = new();
    public ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct) => ValueTask.FromResult<FanoutOutputEvent?>(null);
    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

internal static class OutputCollector
{
    internal static async Task<(string Output, int ElapsedMs)> CollectAsync(
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
            timeout.CancelAfter(Math.Min(quiesceMs, remaining));
            FanoutOutputEvent? item;
            try
            {
                item = await subscription.ReadAsync(timeout.Token).ConfigureAwait(false);
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
                if (remaining < quiesceMs) throw;
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

        return (term.Length > 0 ? term.ToString() : snapshot, (int)clock.ElapsedMilliseconds);
    }
}
