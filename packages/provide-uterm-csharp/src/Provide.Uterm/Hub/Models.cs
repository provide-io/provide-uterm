//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Gui;

namespace Provide.Uterm.Hub;

/// <summary>Input modes for a worker. Mirrors provide.uterm.bridge.contracts.InputMode.</summary>
public static class InputModes
{
    public const string Hijack = "hijack";
    public const string Open = "open";
}

/// <summary>Worker-side transport surface used by the lease manager (send_text).</summary>
public interface IWorkerWs
{
    Task SendTextAsync(string payload, CancellationToken cancellationToken = default);
}

/// <summary>Browser transport that the hub can actively terminate after a failed send.</summary>
public interface IAbortableBrowserWs : IWorkerWs
{
    bool IsActive { get; }
    void Abort();
}

/// <summary>Live REST hijack lease. Port of HijackSession.</summary>
public sealed class HijackSession
{
    public required string HijackId { get; set; }
    public required string Owner { get; set; }
    public double LeaseExpiresAt { get; set; }
    public double AcquiredAt { get; set; }
    public double LastHeartbeat { get; set; }
    public string? AcquiredBy { get; set; }
}

/// <summary>View over dashboard-WS and REST hijack fields.</summary>
public sealed class HijackLease
{
    public object? Ws { get; set; }
    public double? WsExpiresAt { get; set; }
    public HijackSession? Session { get; set; }

    public bool IsIdle => Ws is null && Session is null;

    public bool IsDashboardActive(double now) =>
        Ws is not null && WsExpiresAt is { } exp && exp > now;

    public bool IsRestActive(double now) =>
        Session is not null && Session.LeaseExpiresAt > now;

    public bool IsActive(double now) => IsDashboardActive(now) || IsRestActive(now);

    public (bool RestExpired, bool DashExpired) Expire(double now)
    {
        var restExpired = Session is not null && Session.LeaseExpiresAt <= now;
        var dashExpired = Ws is not null && WsExpiresAt is { } exp && exp <= now;
        if (restExpired) Session = null;
        if (dashExpired)
        {
            Ws = null;
            WsExpiresAt = null;
        }

        return (restExpired, dashExpired);
    }
}

/// <summary>Per-worker connection state held by the registry.</summary>
public sealed class WorkerTermState
{
    public IWorkerWs? WorkerWs { get; set; }
    public Dictionary<object, string> Browsers { get; } = new();
    public object? HijackOwner { get; set; }
    public double? HijackOwnerExpiresAt { get; set; }
    /// <summary>Monotonic identity for dashboard ownership, used to reject stale resume tokens.</summary>
    public long HijackOwnershipVersion { get; set; }
    public HijackSession? HijackSession { get; set; }
    public string? HijackPending { get; set; }
    internal object? PendingDashboardBrowser { get; set; }
    internal long? PendingDashboardOwnershipVersion { get; set; }
    internal string? PendingPauseReservation { get; set; }
    internal string? PendingPauseObligation { get; set; }
    internal Task? DisconnectResumeCompletion { get; set; }
    internal long? DisconnectResumeOwnershipVersion { get; set; }
    internal PendingLifecycleTransition? ActiveLifecycleTransition { get; set; }
    internal readonly List<PendingLifecycleTransition> LifecycleTransitionQueue = [];
    internal PendingLifecycleTransition? PendingDisconnectTransition { get; set; }
    internal PendingInputSend? InputSendPending { get; set; }
    public string InputMode { get; set; } = InputModes.Hijack;

    /// <summary>
    /// Whether an authenticated caller has explicitly decided this session's
    /// input mode, as opposed to it merely holding the <c>hijack</c> default.
    /// </summary>
    /// <remarks>
    /// This tells two claims apart. A <c>worker_hello</c> announces what the
    /// worker process booted with; <c>SetInputMode</c> is a decision made
    /// through an authenticated route by somebody holding
    /// <c>session.control.mode</c>. Without the distinction the hub cannot
    /// refuse a hello that lowers <c>hijack</c> to <c>open</c>, because
    /// <see cref="InputMode"/> defaults to <c>hijack</c> and refusing every
    /// lowering would refuse every worker that legitimately announces
    /// <c>open</c>.
    ///
    /// Held on the worker state rather than the connection deliberately:
    /// registry state outlives a worker socket, so a decision survives a
    /// reconnect. Internal only — nothing serialises it onto the wire.
    /// </remarks>
    public bool InputModeSetByOperator { get; set; }
    public Dictionary<string, object?>? LastSnapshot { get; set; }
    public List<Dictionary<string, object?>> Events { get; } = new();
    public int EventSeq { get; set; }
    public int MinEventSeq { get; set; }
    public double LastActivityAt { get; set; }
    public int? ProtocolVersion { get; set; }
    public bool IsTunnelWorker { get; set; }

    /// <summary>Optional remote GUI session (memory fixture or RFB client).</summary>
    public IGraphicalSession? GraphicalSession { get; set; }

    public HijackLease Lease() => new()
    {
        Ws = HijackOwner,
        WsExpiresAt = HijackOwnerExpiresAt,
        Session = HijackSession,
    };

    public void ApplyLease(HijackLease l)
    {
        HijackOwner = l.Ws;
        HijackOwnerExpiresAt = l.WsExpiresAt;
        HijackSession = l.Session;
    }
}

/// <summary>One input delivery linearized against lease and worker lifecycle transitions.</summary>
internal sealed class PendingInputSend
{
    internal required string Reservation { get; init; }
    internal required IWorkerWs Worker { get; init; }
    internal string? RestHijackId { get; init; }
    internal object? DashboardOwner { get; init; }
    internal long? DashboardOwnershipVersion { get; init; }
    internal required TaskCompletionSource Completion { get; init; }
}

/// <summary>A lifecycle successor waiting for atomic promotion to the active fence.</summary>
internal enum LifecycleTransitionState
{
    Queued,
    Active,
    Completed,
    Cleared,
}

internal sealed class PendingLifecycleTransition
{
    internal required string Reservation { get; init; }
    internal long? OwnershipVersion { get; init; }
    internal object? DisconnectOwner { get; init; }
    internal bool PreserveOnWorkerClear { get; init; }
    internal required TaskCompletionSource Activated { get; init; }
    internal required TaskCompletionSource Completion { get; init; }
    internal LifecycleTransitionState State { get; set; }

    internal bool IsTerminal =>
        State is LifecycleTransitionState.Completed or LifecycleTransitionState.Cleared;
}

/// <summary>FIFO handoff for the active lifecycle fields retained for compatibility.</summary>
internal static class LifecycleTransitionCoordinator
{
    internal static PendingLifecycleTransition ReserveActive(
        WorkerTermState st,
        string reservation,
        long? ownershipVersion = null,
        object? disconnectOwner = null,
        bool preserveOnWorkerClear = false)
    {
        var transition = NewTransition(
            reservation, ownershipVersion, disconnectOwner, preserveOnWorkerClear);
        Activate(st, transition);
        return transition;
    }

    internal static PendingLifecycleTransition EnqueueSuccessor(
        WorkerTermState st,
        string reservation,
        long? ownershipVersion = null,
        object? disconnectOwner = null,
        bool preserveOnWorkerClear = false)
    {
        var transition = NewTransition(
            reservation, ownershipVersion, disconnectOwner, preserveOnWorkerClear);
        st.LifecycleTransitionQueue.Add(transition);
        return transition;
    }

    internal static void Complete(WorkerTermState st, PendingLifecycleTransition transition)
    {
        if (ReferenceEquals(st.ActiveLifecycleTransition, transition))
        {
            if (st.LifecycleTransitionQueue.Count == 0)
            {
                st.ActiveLifecycleTransition = null;
                st.HijackPending = null;
                st.DisconnectResumeCompletion = null;
                st.DisconnectResumeOwnershipVersion = null;
            }
            else
            {
                var successor = st.LifecycleTransitionQueue[0];
                st.LifecycleTransitionQueue.RemoveAt(0);
                Activate(st, successor);
            }
        }
        else
        {
            st.LifecycleTransitionQueue.Remove(transition);
        }

        if (ReferenceEquals(st.PendingDisconnectTransition, transition))
        {
            st.PendingDisconnectTransition = null;
        }
        transition.State = LifecycleTransitionState.Completed;
        transition.Activated.TrySetResult();
        transition.Completion.TrySetResult();
    }

    internal static void Clear(WorkerTermState st)
    {
        var active = st.ActiveLifecycleTransition;
        var ordered = active is null
            ? st.LifecycleTransitionQueue.ToArray()
            : new[] { active }.Concat(st.LifecycleTransitionQueue).ToArray();
        var preserved = ordered.Where(transition => transition.PreserveOnWorkerClear).ToArray();
        var cleared = ordered.Where(transition => !transition.PreserveOnWorkerClear).ToArray();
        st.LifecycleTransitionQueue.Clear();
        st.ActiveLifecycleTransition = null;
        st.HijackPending = null;
        st.DisconnectResumeCompletion = null;
        st.DisconnectResumeOwnershipVersion = null;
        st.PendingDisconnectTransition = null;
        if (preserved.Length > 0)
        {
            Activate(st, preserved[0]);
            foreach (var transition in preserved.Skip(1))
            {
                transition.State = LifecycleTransitionState.Queued;
                st.LifecycleTransitionQueue.Add(transition);
            }
        }
        foreach (var transition in cleared)
        {
            transition.State = LifecycleTransitionState.Cleared;
            transition.Activated.TrySetResult();
            transition.Completion.TrySetResult();
        }
    }

    private static PendingLifecycleTransition NewTransition(
        string reservation,
        long? ownershipVersion,
        object? disconnectOwner,
        bool preserveOnWorkerClear) => new()
        {
            Reservation = reservation,
            OwnershipVersion = ownershipVersion,
            DisconnectOwner = disconnectOwner,
            PreserveOnWorkerClear = preserveOnWorkerClear,
            Activated = NewSignal(),
            Completion = NewSignal(),
            State = LifecycleTransitionState.Queued,
        };

    private static void Activate(WorkerTermState st, PendingLifecycleTransition transition)
    {
        transition.State = LifecycleTransitionState.Active;
        st.ActiveLifecycleTransition = transition;
        st.HijackPending = transition.Reservation;
        st.DisconnectResumeCompletion = transition.Completion.Task;
        st.DisconnectResumeOwnershipVersion = transition.OwnershipVersion;
        transition.Activated.TrySetResult();
    }

    private static TaskCompletionSource NewSignal() =>
        new(TaskCreationOptions.RunContinuationsAsynchronously);
}
