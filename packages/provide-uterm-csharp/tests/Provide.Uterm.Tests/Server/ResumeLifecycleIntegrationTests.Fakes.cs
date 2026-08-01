//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Reflection;
using System.Text;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Shell;
using Provide.Uterm.Tunnel;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Tests.Server;

public sealed partial class ResumeLifecycleIntegrationTests
{
    private sealed record Fixture(
        UtermServer Server,
        TermHub Hub,
        RecordingWorker Worker,
        string Token,
        Uri Uri);

    private enum FaultTarget
    {
        Resume,
        Input,
    }

    private sealed class ResumeFailureGate
    {
        private readonly TaskCompletionSource _attempted = NewSignal();
        private readonly TaskCompletionSource _release = NewSignal();
        private readonly TaskCompletionSource _completed = NewSignal();

        internal Task Attempted => _attempted.Task;
        internal Task Completed => _completed.Task;

        internal void ReleaseFault() => _release.TrySetResult();

        internal async Task SendAsync(
            string payload,
            CancellationToken cancellationToken = default)
        {
            _attempted.TrySetResult();
            try
            {
                await _release.Task.ConfigureAwait(false);
                throw new IOException("deterministic resume send failure");
            }
            finally
            {
                _completed.TrySetResult();
            }
        }

        private static TaskCompletionSource NewSignal() =>
            new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed class GatedResumeFailureWorker : IAbortableBrowserWs
    {
        private readonly ResumeFailureGate _failure;

        internal GatedResumeFailureWorker(ResumeFailureGate failure) => _failure = failure;

        public bool IsActive { get; private set; } = true;
        internal bool AbortedWhileAuthoritative { get; private set; }
        internal Func<bool>? IsAuthoritative { get; set; }

        public void Abort()
        {
            AbortedWhileAuthoritative = IsAuthoritative?.Invoke() is true;
            IsActive = false;
        }

        public Task SendTextAsync(
            string payload,
            CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            return action == "resume"
                ? _failure.SendAsync(payload, cancellationToken)
                : Task.CompletedTask;
        }
    }

    private sealed class FaultingWorker : IAbortableBrowserWs
    {
        private readonly FaultTarget _target;
        private readonly bool _hangs;
        private readonly TaskCompletionSource _failureAttempted = NewSignal();
        private readonly TaskCompletionSource _throwRelease = NewSignal();
        private readonly TaskCompletionSource _never = NewSignal();

        internal FaultingWorker(FaultTarget target, bool hangs)
        {
            _target = target;
            _hangs = hangs;
        }

        public bool IsActive { get; private set; } = true;

        internal bool Aborted { get; private set; }

        internal bool AbortedWhileAuthoritative { get; private set; }

        internal Func<bool>? IsAuthoritative { get; set; }

        internal Task FailureAttempted => _failureAttempted.Task;

        public void Abort()
        {
            AbortedWhileAuthoritative = IsAuthoritative?.Invoke() is true;
            Aborted = true;
            IsActive = false;
        }

        internal void ReleaseThrow() => _throwRelease.TrySetResult();

        public async Task SendTextAsync(
            string payload,
            CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            var shouldFail = _target == FaultTarget.Resume && action == "resume"
                || _target == FaultTarget.Input && action is null;
            if (!shouldFail) return;

            _failureAttempted.TrySetResult();
            if (_hangs)
            {
                await _never.Task.ConfigureAwait(false);
                return;
            }

            await _throwRelease.Task.ConfigureAwait(false);
            throw new IOException("deterministic worker send failure");
        }

        private static TaskCompletionSource NewSignal() =>
            new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed class AcquisitionFaultWorker : IAbortableBrowserWs
    {
        private readonly string _failedAction;
        private readonly bool _hangs;
        private readonly TaskCompletionSource _pauseAttempted = NewSignal();
        private readonly TaskCompletionSource _pauseRelease = NewSignal();
        private readonly TaskCompletionSource _failureAttempted = NewSignal();
        private readonly TaskCompletionSource _throwRelease = NewSignal();
        private readonly TaskCompletionSource _never = NewSignal();

        internal AcquisitionFaultWorker(string failedAction, bool hangs)
        {
            _failedAction = failedAction;
            _hangs = hangs;
        }

        public bool IsActive { get; private set; } = true;
        internal bool Aborted { get; private set; }
        internal Func<bool>? IsAuthoritative { get; set; }
        internal Task PauseAttempted => _pauseAttempted.Task;
        internal Task FailureAttempted => _failureAttempted.Task;

        public void Abort()
        {
            Aborted = true;
            IsActive = false;
        }

        internal void ReleasePause() => _pauseRelease.TrySetResult();
        internal void ReleaseThrow() => _throwRelease.TrySetResult();

        public async Task SendTextAsync(
            string payload,
            CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            if (action == "pause")
            {
                _pauseAttempted.TrySetResult();
                if (_failedAction == "resume")
                {
                    await _pauseRelease.Task.ConfigureAwait(false);
                    return;
                }
            }
            if (action != _failedAction) return;

            _failureAttempted.TrySetResult();
            if (_hangs)
            {
                await _never.Task.ConfigureAwait(false);
                return;
            }

            await _throwRelease.Task.ConfigureAwait(false);
            throw new IOException($"deterministic {action} failure");
        }

        private static TaskCompletionSource NewSignal() =>
            new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed class RecordingWorker : IAbortableBrowserWs
    {
        private readonly object _gate = new();
        private readonly List<string> _actions = [];
        private readonly Queue<PauseGate> _pauseGates = new();
        private PauseGate? _lastPauseGate;
        private TaskCompletionSource? _resumeAttempted;
        private TaskCompletionSource? _resumeRelease;
        private TaskCompletionSource? _inputAttempted;
        private TaskCompletionSource? _inputRelease;
        private ActiveCheckGate? _nextActiveCheck;
        private bool _isActive = true;
        private readonly List<string> _inputs = [];

        public Func<Task>? AfterResume { get; set; }

        public bool IsActive
        {
            get
            {
                ActiveCheckGate? activeCheck;
                lock (_gate)
                {
                    activeCheck = _nextActiveCheck;
                    _nextActiveCheck = null;
                    if (activeCheck is null) return _isActive;
                }

                activeCheck.MarkAttempted();
                activeCheck.Wait();
                lock (_gate) return _isActive;
            }
        }

        public IReadOnlyList<string> Actions
        {
            get { lock (_gate) return _actions.ToArray(); }
        }

        public IReadOnlyList<string> Inputs
        {
            get { lock (_gate) return _inputs.ToArray(); }
        }

        public Task InputAttempted
        {
            get { lock (_gate) return (_inputAttempted ??= NewSignal()).Task; }
        }

        public Task PauseAttempted
        {
            get { lock (_gate) return (_lastPauseGate ??= EnqueuePauseGate()).Attempted; }
        }

        public Task ResumeAttempted
        {
            get { lock (_gate) return (_resumeAttempted ??= NewSignal()).Task; }
        }

        public void FailNextPause()
        {
            lock (_gate) _lastPauseGate = EnqueuePauseGate(fail: true, delayed: false);
        }

        public void ThrowAfterNextPause(bool cancel)
        {
            lock (_gate)
            {
                _lastPauseGate = EnqueuePauseGate(
                    fail: !cancel,
                    cancel: cancel,
                    delayed: false,
                    landBeforeFailure: true);
            }
        }

        public PauseGate DelayNextPause(bool fail = false, bool cancel = false)
        {
            lock (_gate)
            {
                return _lastPauseGate = EnqueuePauseGate(fail, cancel, delayed: true);
            }
        }

        public void ReleasePause()
        {
            lock (_gate) _lastPauseGate?.Release();
        }

        public void DelayNextResume()
        {
            lock (_gate)
            {
                _resumeAttempted = NewSignal();
                _resumeRelease = NewSignal();
            }
        }

        public void ReleaseResume()
        {
            lock (_gate) _resumeRelease?.TrySetResult();
        }

        public void DelayNextInput()
        {
            lock (_gate)
            {
                _inputAttempted = NewSignal();
                _inputRelease = NewSignal();
            }
        }

        public void ReleaseInput()
        {
            lock (_gate) _inputRelease?.TrySetResult();
        }

        public ActiveCheckGate BlockNextActiveCheck()
        {
            lock (_gate)
            {
                if (_nextActiveCheck is not null)
                {
                    throw new InvalidOperationException("an active check is already blocked");
                }

                return _nextActiveCheck = new ActiveCheckGate();
            }
        }

        public void Deactivate()
        {
            lock (_gate) _isActive = false;
        }

        public void Abort()
        {
            ActiveCheckGate? activeCheck;
            lock (_gate)
            {
                _isActive = false;
                activeCheck = _nextActiveCheck;
                _nextActiveCheck = null;
            }
            activeCheck?.Release();
        }

        public async Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            if (action is not null)
            {
                PauseGate? pauseGate = null;
                Task? release = null;
                TaskCompletionSource? releaseSignal = null;
                lock (_gate)
                {
                    if (action == "pause")
                    {
                        if (_pauseGates.Count > 0) pauseGate = _pauseGates.Dequeue();
                        pauseGate?.MarkAttempted();
                    }
                    else if (action == "resume")
                    {
                        _resumeAttempted?.TrySetResult();
                        releaseSignal = _resumeRelease;
                        release = releaseSignal?.Task;
                    }
                }

                if (pauseGate is not null) await pauseGate.WaitAsync(cancellationToken);
                if (release is not null) await release.WaitAsync(cancellationToken);
                lock (_gate)
                {
                    if (action == "resume" && ReferenceEquals(_resumeRelease, releaseSignal))
                    {
                        _resumeRelease = null;
                    }
                }
                var recorded = false;
                if (pauseGate?.LandBeforeFailure is true)
                {
                    lock (_gate) _actions.Add(action);
                    recorded = true;
                }
                if (pauseGate?.Cancel is true)
                {
                    throw new OperationCanceledException("deterministic pause cancellation", cancellationToken);
                }
                if (pauseGate?.Fail is true) throw new IOException("deterministic pause failure");
                if (!recorded)
                {
                    lock (_gate) _actions.Add(action);
                }
                if (action == "resume" && AfterResume is not null) await AfterResume();
            }
            else
            {
                Task? release;
                TaskCompletionSource? releaseSignal;
                lock (_gate)
                {
                    _inputAttempted?.TrySetResult();
                    releaseSignal = _inputRelease;
                    release = releaseSignal?.Task;
                }

                if (release is not null) await release.WaitAsync(cancellationToken);
                lock (_gate)
                {
                    _inputs.Add(payload);
                    if (ReferenceEquals(_inputRelease, releaseSignal)) _inputRelease = null;
                }
            }
        }

        private static TaskCompletionSource NewSignal() =>
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        private PauseGate EnqueuePauseGate(
            bool fail = false,
            bool cancel = false,
            bool delayed = true,
            bool landBeforeFailure = false)
        {
            var gate = new PauseGate(fail, cancel, delayed, landBeforeFailure);
            _pauseGates.Enqueue(gate);
            return gate;
        }

        public sealed class PauseGate
        {
            private readonly TaskCompletionSource _attempted = NewSignal();
            private readonly TaskCompletionSource? _release;

            internal PauseGate(bool fail, bool cancel, bool delayed, bool landBeforeFailure)
            {
                Fail = fail;
                Cancel = cancel;
                LandBeforeFailure = landBeforeFailure;
                if (delayed) _release = NewSignal();
            }

            public bool Fail { get; }
            public bool Cancel { get; }
            public bool LandBeforeFailure { get; }
            public Task Attempted => _attempted.Task;
            internal void MarkAttempted() => _attempted.TrySetResult();
            public void Release() => _release?.TrySetResult();

            internal Task WaitAsync(CancellationToken cancellationToken) =>
                _release?.Task.WaitAsync(cancellationToken) ?? Task.CompletedTask;
        }

        public sealed class ActiveCheckGate
        {
            private readonly TaskCompletionSource _attempted = NewSignal();
            private readonly TaskCompletionSource _release = NewSignal();

            public Task Attempted => _attempted.Task;
            internal void MarkAttempted() => _attempted.TrySetResult();
            internal void Wait() => _release.Task.GetAwaiter().GetResult();
            public void Release() => _release.TrySetResult();
        }
    }

    private sealed class RecordingBrowser : IWorkerWs
    {
        private readonly object _gate = new();
        private readonly List<string> _payloads = [];

        public IReadOnlyList<string> Payloads
        {
            get { lock (_gate) return _payloads.ToArray(); }
        }

        public void Clear()
        {
            lock (_gate) _payloads.Clear();
        }

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            lock (_gate) _payloads.Add(payload);
            return Task.CompletedTask;
        }
    }

    private sealed class DelayedDisconnectBrowser : IWorkerWs
    {
        private readonly object _gate = new();
        private readonly List<string> _payloads = [];
        private readonly TaskCompletionSource _disconnectAttempted =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _disconnectRelease =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public Task DisconnectAttempted => _disconnectAttempted.Task;

        public IReadOnlyList<string> Payloads
        {
            get { lock (_gate) return _payloads.ToArray(); }
        }

        public void ReleaseDisconnect() => _disconnectRelease.TrySetResult();

        public async Task SendTextAsync(
            string payload,
            CancellationToken cancellationToken = default)
        {
            lock (_gate) _payloads.Add(payload);
            var isDisconnect = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Any(chunk => Type(chunk.Control) == "worker_disconnected");
            if (!isDisconnect) return;
            _disconnectAttempted.TrySetResult();
            await _disconnectRelease.Task.WaitAsync(cancellationToken);
        }
    }

    private sealed class ClosedWorker : IAbortableBrowserWs
    {
        public bool IsActive => false;
        public int SendCount { get; private set; }
        public void Abort() { }

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            SendCount++;
            return Task.CompletedTask;
        }
    }

    private sealed class ThrowingResumeWorker : IAbortableBrowserWs
    {
        public bool IsActive => !Aborted;
        public bool Aborted { get; private set; }
        public void Abort() => Aborted = true;

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            return action == "resume"
                ? Task.FromException(new IOException("resume failed"))
                : Task.CompletedTask;
        }
    }

    private sealed class ThrowingResumeAndAbortWorker : IAbortableBrowserWs
    {
        public bool IsActive => true;
        public void Abort() => throw new IOException("abort failed");

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            var action = new ControlFrameDecoder().Feed(payload)
                .OfType<ControlChunk>()
                .Select(chunk => chunk.Control.GetValueOrDefault("action")?.ToString())
                .FirstOrDefault(value => value is not null);
            return action == "resume"
                ? Task.FromException(new IOException("resume failed"))
                : Task.CompletedTask;
        }
    }

    private sealed class DeactivateDuringSendWorker : IAbortableBrowserWs
    {
        public bool IsActive { get; private set; } = true;
        public void Abort() => IsActive = false;

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            IsActive = false;
            return Task.CompletedTask;
        }
    }

    private sealed class ThrowingPauseAndAbortWorker : IAbortableBrowserWs
    {
        public bool IsActive => true;
        public void Abort() => throw new IOException("abort failed");

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.FromException(new IOException("worker send failed"));
    }

    private sealed class ThrowingSleepClock : IClock
    {
        public TaskCompletionSource SleepAttempted { get; } =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        public double Monotonic() => 0;
        public double Wall() => 100;

        public Task SleepAsync(double seconds, CancellationToken cancellationToken = default)
        {
            SleepAttempted.TrySetResult();
            throw new IOException("timer scheduling failed");
        }
    }

    private sealed class GatedClock : IClock
    {
        private readonly TaskCompletionSource _sleepAttempted =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _sleepRelease =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private double _monotonic;

        public Task SleepAttempted => _sleepAttempted.Task;
        public double Monotonic() => _monotonic;
        public double Wall() => 100 + _monotonic;
        public void SetMonotonic(double value) => _monotonic = value;
        public void ReleaseSleep() => _sleepRelease.TrySetResult();

        public async Task SleepAsync(double seconds, CancellationToken cancellationToken = default)
        {
            _sleepAttempted.TrySetResult();
            await _sleepRelease.Task.WaitAsync(cancellationToken);
        }
    }
}
