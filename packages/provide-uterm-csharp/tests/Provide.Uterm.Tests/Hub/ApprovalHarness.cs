//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

/// <summary>
/// Shared doubles for the browser-input approval tests (Go
/// <c>hub/approvals_resolve_test.go</c>'s <c>holdGate</c>, <c>fakeWorkerWS</c>
/// and <c>registerActiveBrowser</c>). Runs in the ~Hub gate batch.
/// </summary>
internal sealed class HoldGate : IInputPolicyGate
{
    public Task<PolicyDecision> InterceptInputAsync(
        string data,
        PolicyContext context,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 60 });
}

/// <summary>A gate that allows, but is not the no-op gate — so the gated path still runs.</summary>
internal sealed class AllowGate : IInputPolicyGate
{
    public Task<PolicyDecision> InterceptInputAsync(
        string data,
        PolicyContext context,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(PolicyDecision.Allow());
}

internal sealed class DenyGate : IInputPolicyGate
{
    public Task<PolicyDecision> InterceptInputAsync(
        string data,
        PolicyContext context,
        CancellationToken cancellationToken = default) =>
        Task.FromResult(new PolicyDecision { Action = PolicyActions.Deny, TimeoutS = 60 });
}

/// <summary>Records every payload the hub forwarded to the worker.</summary>
internal class RecordingWorker : IWorkerWs
{
    private readonly List<string> _sent = new();

    /// <summary>
    /// Terminal input only. Lease lifecycle (pause/resume) arrives on the same
    /// socket as DLE/STX control frames, and an assertion about what a command
    /// injected must not depend on whether a lease moved.
    /// </summary>
    public IReadOnlyList<string> Inputs
    {
        get
        {
            lock (_sent) return _sent.Where(p => !ControlChannelCodec.IsControlFrame(p)).ToArray();
        }
    }

    public virtual Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
    {
        Record(payload);
        return Task.CompletedTask;
    }

    protected int InputCount => Inputs.Count;

    protected void Record(string payload)
    {
        lock (_sent) _sent.Add(payload);
    }
}

/// <summary>
/// Blocks inside its first terminal input so a concurrent send can be observed
/// waiting behind the approval's reservation. Go's equivalent is
/// <c>orderedApprovalWorker</c>.
/// </summary>
internal sealed class GatedWorker : RecordingWorker
{
    public TaskCompletionSource Entered { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public TaskCompletionSource Release { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public override async Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
    {
        Record(payload);
        if (InputCount == 1 && !ControlChannelCodec.IsControlFrame(payload))
        {
            Entered.TrySetResult();
            await Release.Task.WaitAsync(TimeSpan.FromSeconds(30), cancellationToken).ConfigureAwait(false);
        }
    }
}

/// <summary>A worker whose second input fails — the approved command lands, the replay does not.</summary>
internal sealed class ReplayFailingWorker : RecordingWorker
{
    public override Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
    {
        Record(payload);
        return InputCount == 2
            ? Task.FromException(new TimeoutException("replay send failed"))
            : Task.CompletedTask;
    }
}

/// <summary>A browser connection that records the control frames broadcast to it.</summary>
internal sealed class RecordingBrowser : IWorkerWs
{
    private readonly List<string> _payloads = new();

    public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
    {
        lock (_payloads) _payloads.Add(payload);
        return Task.CompletedTask;
    }

    /// <summary>Every decoded control frame this browser received, in order.</summary>
    public List<Dictionary<string, object?>> Frames()
    {
        string[] payloads;
        lock (_payloads) payloads = _payloads.ToArray();
        var frames = new List<Dictionary<string, object?>>();
        foreach (var payload in payloads)
        {
            var decoder = new ControlFrameDecoder();
            foreach (var chunk in decoder.Feed(payload))
            {
                if (chunk is ControlChunk ctrl) frames.Add(ctrl.Control);
            }
        }

        return frames;
    }

    public Dictionary<string, object?>? Frame(string type) =>
        Frames().FirstOrDefault(f => f.TryGetValue("type", out var t) && t?.ToString() == type);
}

/// <summary>A hub with a worker, an owning browser, and whatever gate the test wants.</summary>
internal sealed class ApprovalHarness
{
    public const string WorkerId = "w";

    public required TermHub Hub { get; init; }
    public required ManualClock Clock { get; init; }
    public required RecordingWorker Worker { get; init; }
    public required RecordingBrowser Browser { get; init; }

    public static ApprovalHarness Create(
        IInputPolicyGate? gate = null,
        RecordingWorker? worker = null,
        int maxInputChars = 0,
        int maxBufferChars = 0,
        Action<string, string>? onLog = null)
    {
        var clock = new ManualClock(5000);
        clock.SetMonotonic(10);
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            PolicyGate = gate,
            MaxInputChars = maxInputChars,
            MaxBufferChars = maxBufferChars,
            OnLog = onLog,
        });
        var workerWs = worker ?? new RecordingWorker();
        Assert.True(hub.Conn.RegisterWorker(WorkerId, workerWs));
        var browser = new RecordingBrowser();
        hub.Conn.RegisterBrowser(WorkerId, browser, "admin", principalSubjectId: "submitter");
        // Sync harness factory. The browser above registers without
        // deferBroadcast, so there is no backlog to flush and this only
        // takes the lock — blocking here cannot wait on real I/O.
        hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser).GetAwaiter().GetResult();
        var (acquired, reason) = hub.Lease.TryAcquireWs(WorkerId, browser);
        Assert.True(acquired, reason);
        return new ApprovalHarness
        {
            Hub = hub,
            Clock = clock,
            Worker = workerWs,
            Browser = browser,
        };
    }

    public Task<ApprovalParkResult> ParkAsync(string command, PolicyDecision? decision = null) =>
        Hub.ParkBrowserForApprovalAsync(
            WorkerId,
            Browser,
            command,
            decision ?? new PolicyDecision { Action = PolicyActions.Hold, TimeoutS = 60 });

    public async Task<string> ParkIdAsync(string command, PolicyDecision? decision = null)
    {
        var parked = await ParkAsync(command, decision);
        Assert.Equal(string.Empty, parked.Reason);
        Assert.NotNull(parked.RequestId);
        return parked.RequestId!;
    }
}
