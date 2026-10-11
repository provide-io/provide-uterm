//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A subscription whose dispose is still running when the response budget is
// already spent: the controller must not wait for it, and a fault it raises
// later must still be observed rather than left unobserved.

using Provide.Uterm.Fanout;

namespace Provide.Uterm.Tests;

public sealed partial class FanoutExecutionTests
{
    private sealed class SlowDisposeSubscription : IFanoutOutputSubscription
    {
        public TaskCompletionSource Dispose { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource DisposeStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int Pending => 0;

        public ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct) =>
            ValueTask.FromResult<FanoutOutputEvent?>(null);

        public ValueTask DisposeAsync()
        {
            DisposeStarted.TrySetResult();
            return new ValueTask(Dispose.Task);
        }
    }

    /// <summary>Sends hang until the budget's token fires; subscriptions dispose slowly.</summary>
    private sealed class BudgetEatingHub : IFanoutHub
    {
        public SlowDisposeSubscription Subscription { get; } = new();

        public async Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            await Task.Delay(Timeout.Infinite, ct).ConfigureAwait(false);
            return true;
        }

        public Task BroadcastAsync(string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId) => Subscription;
    }

    /// <summary>Sends succeed at once; subscriptions dispose slowly.</summary>
    private sealed class PromptHub : IFanoutHub
    {
        public SlowDisposeSubscription Subscription { get; } = new();

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.FromResult(true);

        public Task BroadcastAsync(string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId) => Subscription;
    }

    [Theory]
    [InlineData("parallel")]
    [InlineData("sequential")]
    public async Task Dispose_Still_Running_Within_The_Budget_Is_Awaited_To_Completion(string mode)
    {
        // The dispose is pending when the controller looks at it and finishes
        // while the budget still has time left, so the controller waits for it
        // rather than abandoning it. Driven explicitly: left to timing, the
        // dispose almost always completes before the controller looks.
        var hub = new PromptHub();
        var lateFaults = 0;
        var controller = NewController(hub, mode, ["w1"], lateFaultObserver: _ => Interlocked.Increment(ref lateFaults));

        var pending = controller.SendAsync("g", "id", Admin("alice"), 0, 30_000);
        await hub.Subscription.DisposeStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.False(pending.IsCompleted);

        hub.Subscription.Dispose.TrySetResult();
        var result = await pending.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Empty(result.FailedSessions);
        Assert.Equal(0, lateFaults);
    }

    [Theory]
    [InlineData("parallel")]
    [InlineData("sequential")]
    public async Task Dispose_Still_Running_After_The_Budget_Is_Abandoned_And_Its_Late_Fault_Observed(string mode)
    {
        var hub = new BudgetEatingHub();
        var observed = new TaskCompletionSource<Exception>(TaskCreationOptions.RunContinuationsAsynchronously);
        var controller = NewController(hub, mode, ["w1"], lateFaultObserver: error => observed.TrySetResult(error));

        var result = await controller.SendAsync("g", "id", Admin("alice"), 5, 30)
            .WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(["w1"], result.FailedSessions);
        await hub.Subscription.DisposeStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));

        hub.Subscription.Dispose.TrySetException(new InvalidOperationException("late dispose"));
        var error = await observed.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("late dispose", error.Message);
    }
}
