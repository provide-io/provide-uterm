//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using Provide.Uterm.Fanout;
using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Tests;

public sealed partial class FanoutExecutionTests
{
    private static Controller NewController(
        IFanoutHub hub,
        string mode,
        List<string> workers,
        bool stopOnError = false,
        double threshold = 0.8,
        IFanoutAuthorizer? authorizer = default,
        Action<Exception>? lateFaultObserver = null)
    {
        authorizer ??= new TestAuthorizer();
        var controller = new Controller(hub, new ControllerConfig
        {
            IdGen = () => "send", Authorizer = authorizer, LateFaultObserver = lateFaultObserver,
        });
        controller.CreateGroup(new Group
        {
            GroupId = "g",
            WorkerIds = workers,
            Mode = mode,
            StopOnFirstError = stopOnError,
            ErrorPattern = "ERROR",
            QuiesceMs = 5,
            MaxResponseMs = 100,
            DivergenceThreshold = threshold,
        }, "alice");
        return controller;
    }

    private static Principal Admin(string subject) => new()
    {
        SubjectId = subject,
        Roles = StringSet.Of("admin"),
        Scopes = StringSet.Of("*"),
    };

    private sealed class TestAuthorizer : IFanoutAuthorizer
    {
        public HashSet<string> DeniedMembers { get; init; } = [];
        public List<string> CheckedMembers { get; } = [];

        public bool IsGlobalAdmin(Principal principal) =>
            principal.Roles.Has("admin") && principal.AdminSessionScope is null;

        public bool CanReadMember(Principal principal, string workerId)
        {
            CheckedMembers.Add(workerId);
            return !DeniedMembers.Contains(workerId);
        }
    }

    private sealed class ThrowingAuthorizer(string stage) : IFanoutAuthorizer
    {
        public bool IsGlobalAdmin(Principal principal) =>
            stage == "global" ? throw new InvalidOperationException("global auth unavailable") : true;

        public bool CanReadMember(Principal principal, string workerId) =>
            stage == "member" ? throw new InvalidOperationException("member auth unavailable") : true;
    }

    private sealed class TypedThrowingAuthorizer(
        string stage,
        FanoutAuthorizationException error) : IFanoutAuthorizer
    {
        public bool IsGlobalAdmin(Principal principal) =>
            stage == "global" ? throw error : true;

        public bool CanReadMember(Principal principal, string workerId) =>
            stage == "member" ? throw error : true;
    }

    private sealed class SideEffectTrackingHub : IFanoutHub
    {
        public int SubscriptionCount { get; private set; }
        public int ObserverCount { get; private set; }
        public int SendCount { get; private set; }

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            SendCount++;
            return Task.FromResult(true);
        }

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            ObserverCount++;
            return Task.CompletedTask;
        }

        public IFanoutOutputSubscription SubscribeOutput(string workerId)
        {
            SubscriptionCount++;
            return new TrackingSubscription(workerId, new ConcurrentDictionary<string, int>());
        }
    }

    private sealed class EventHub : IFanoutHub
    {
        private readonly IReadOnlyDictionary<string, string> _outputs;
        private readonly ConcurrentDictionary<string, EventSubscription> _subscriptions = new();
        public List<string> Trace { get; } = new();

        public EventHub(IReadOnlyDictionary<string, string> outputs) => _outputs = outputs;

        public Task<bool> SendWorkerAsync(
            string workerId,
            IReadOnlyDictionary<string, object?> msg,
            CancellationToken ct = default)
        {
            lock (Trace) Trace.Add("send:" + workerId);
            if (_outputs.TryGetValue(workerId, out var output))
            {
                _subscriptions[workerId].Enqueue(new FanoutOutputEvent("term", output));
            }
            return Task.FromResult(true);
        }

        public Task BroadcastAsync(
            string workerId,
            IReadOnlyDictionary<string, object?> msg,
            CancellationToken ct = default) => Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId)
        {
            var sub = new EventSubscription(workerId, Trace);
            _subscriptions[workerId] = sub;
            return sub;
        }
    }

    private sealed class HangingStageHub : IFanoutHub
    {
        private readonly string _stage;
        public TaskCompletionSource Started { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource CancellationObserved { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<bool> _never = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public HangingStageHub(string stage) => _stage = stage;

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            if (_stage != "send") return Task.FromResult(true);
            ct.Register(() => CancellationObserved.TrySetResult());
            Started.TrySetResult();
            return _never.Task;
        }

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            if (_stage != "broadcast") return Task.CompletedTask;
            ct.Register(() => CancellationObserved.TrySetResult());
            Started.TrySetResult();
            return _never.Task;
        }

        public void Fault(Exception error) => _never.TrySetException(error);
    }

    private sealed class PreparationFailureHub : IFanoutHub
    {
        private readonly string _failedWorker;
        public List<string> SubscriptionAttempts { get; } = [];
        public List<string> SentWorkers { get; } = [];
        public ConcurrentDictionary<string, int> DisposeCounts { get; } = new();

        public PreparationFailureHub(string failedWorker) => _failedWorker = failedWorker;

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            SentWorkers.Add(workerId);
            return Task.FromResult(true);
        }

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId)
        {
            SubscriptionAttempts.Add(workerId);
            if (workerId == _failedWorker)
            {
                throw new InvalidOperationException("capture unavailable");
            }
            return new TrackingSubscription(workerId, DisposeCounts);
        }
    }

    private sealed class TrackingSubscription : IFanoutOutputSubscription
    {
        private readonly string _workerId;
        private readonly ConcurrentDictionary<string, int> _disposeCounts;

        public TrackingSubscription(string workerId, ConcurrentDictionary<string, int> disposeCounts)
        {
            _workerId = workerId;
            _disposeCounts = disposeCounts;
        }

        public int Pending => 0;

        public ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct) =>
            ValueTask.FromResult<FanoutOutputEvent?>(null);

        public ValueTask DisposeAsync()
        {
            _disposeCounts.AddOrUpdate(_workerId, 1, (_, count) => count + 1);
            return ValueTask.CompletedTask;
        }
    }

    private sealed class SlowReadHub : IFanoutHub
    {
        private readonly int _delayMs;
        public List<string> Trace { get; } = [];

        public SlowReadHub(int delayMs) => _delayMs = delayMs;

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            Trace.Add("send:" + workerId);
            return Task.FromResult(true);
        }

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId) => new SlowReadSubscription(_delayMs);
    }

    private sealed class SlowReadSubscription : IFanoutOutputSubscription
    {
        private readonly int _delayMs;
        public SlowReadSubscription(int delayMs) => _delayMs = delayMs;

        // The read deliberately ignores the token it is handed. A read that stops on
        // the collector's own per-read bound ends the member at whatever that bound's
        // timer decides, and .NET timers on Windows are tick-rounded and may fire up
        // to a tick early -- so the two nested bounds could sum to just under the
        // shared budget, leave RemainingMs at 1, and let the next member be sent.
        // With the bound ignored, the only thing that can end this read is the shared
        // OperationBudget deadline, which sets IsCancellationRequested rather than
        // relying on truncated elapsed-millisecond arithmetic. That is the invariant
        // this hub exists to exercise, and it holds whichever way the timers round.
        public int Pending => 0;

        public async ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct)
        {
            await Task.Delay(_delayMs, CancellationToken.None);
            return null;
        }

        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    private sealed class RefusingSendHub(string refusedWorker) : IFanoutHub
    {
        public List<string> SendAttempts { get; } = [];

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            SendAttempts.Add(workerId);
            return Task.FromResult(workerId != refusedWorker);
        }

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId) =>
            new TrackingSubscription(workerId, new ConcurrentDictionary<string, int>());
    }

    private sealed class CancelDuringCollectionHub : IFanoutHub
    {
        public TaskCompletionSource ReadStarted { get; } =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.FromResult(true);

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId) =>
            new CancelDuringCollectionSubscription(ReadStarted);
    }

    private sealed class CancelDuringCollectionSubscription(TaskCompletionSource started) : IFanoutOutputSubscription
    {
        public int Pending => 0;

        public async ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct)
        {
            started.TrySetResult();
            await Task.Delay(Timeout.InfiniteTimeSpan, ct);
            return null;
        }

        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    private sealed class DisposalFailureHub(string mode) : IFanoutHub
    {
        private readonly TaskCompletionSource _dispose =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int DisposeAttempts { get; private set; }

        public void FaultDisposal() => _dispose.TrySetException(new IOException("dispose failed"));

        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.FromResult(true);

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId) =>
            new DisposalFailureSubscription(mode, _dispose, () => DisposeAttempts++);
    }

    private sealed class DisposalFailureSubscription(
        string mode,
        TaskCompletionSource dispose,
        Action onDispose) : IFanoutOutputSubscription
    {
        public int Pending => 0;

        public async ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct)
        {
            if (mode == "expired_before_dispose") await Task.Delay(30, ct);
            return null;
        }

        public ValueTask DisposeAsync()
        {
            onDispose();
            return mode switch
            {
                "sync_throw" => throw new IOException("dispose failed"),
                "completed_fault" => ValueTask.FromException(new IOException("dispose failed")),
                "delayed_fault" => DelayedFailureAsync(),
                "delayed_success" => DelayedSuccessAsync(),
                _ => new ValueTask(dispose.Task),
            };
        }

        private static async ValueTask DelayedFailureAsync()
        {
            await Task.Yield();
            throw new IOException("dispose failed");
        }

        private static async ValueTask DelayedSuccessAsync() => await Task.Yield();
    }

    private sealed class ReadFailureHub : IFanoutHub
    {
        public Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.FromResult(true);

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default) =>
            Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId) => new ReadFailureSubscription();
    }

    private sealed class ReadFailureSubscription : IFanoutOutputSubscription
    {
        public int Pending => 0;

        public ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct) =>
            ValueTask.FromException<FanoutOutputEvent?>(new IOException("output failed"));

        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    private sealed class EventSubscription : IFanoutOutputSubscription
    {
        private readonly string _workerId;
        private readonly List<string> _trace;
        private readonly System.Threading.Channels.Channel<FanoutOutputEvent?> _events =
            System.Threading.Channels.Channel.CreateUnbounded<FanoutOutputEvent?>();

        public EventSubscription(string workerId, List<string> trace)
        {
            _workerId = workerId;
            _trace = trace;
        }

        public void Enqueue(FanoutOutputEvent item) => _events.Writer.TryWrite(item);

        public int Pending => _events.Reader.Count;

        public async ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct)
        {
            lock (_trace) _trace.Add("read:" + _workerId);
            return await _events.Reader.ReadAsync(ct);
        }

        public ValueTask DisposeAsync()
        {
            _events.Writer.TryComplete();
            return ValueTask.CompletedTask;
        }
    }
}
