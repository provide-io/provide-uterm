//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.ControlChannel;

/// <summary>
/// A <c>worker_hello</c> announces what the worker process booted with;
/// <c>SetInputMode</c> is a decision made through an authenticated route. The
/// hub has to tell them apart, because <c>InputMode</c> defaults to
/// <c>hijack</c>: a rule refusing every hello that lowers hijack to open would
/// refuse every worker that legitimately announces open, which is most of them.
///
/// Until this change the port had no inbound <c>worker_hello</c> handling at
/// all — the frame was parsed, fanned out to browsers, and dropped — so a
/// worker's announced mode never reached hub state in either direction.
/// </summary>
public sealed class WorkerHelloDecisionTests
{
    private static (TermHub Hub, string WorkerId) Undecided()
    {
        var hub = new TermHub(new TermHubConfig());
        const string workerId = "w-hello";
        hub.Registry.Put(workerId, new WorkerTermState());
        return (hub, workerId);
    }

    [Fact]
    public void AHelloAppliesWhenNobodyHasDecidedAMode()
    {
        var (hub, workerId) = Undecided();

        Assert.True(hub.Conn.SetWorkerHello(workerId, InputModes.Open));

        Assert.Equal(InputModes.Open, hub.Registry.Get(workerId)!.InputMode);
    }

    [Fact]
    public void AHelloCannotUndoADecisionEvenWithNoLeaseHeld()
    {
        // The window a lease-only guard leaves open: an operator sets hijack and
        // then acquires, and a hello landing between the two reverts the mode —
        // so the acquire is refused for being in open mode, which says nothing
        // about why.
        var (hub, workerId) = Undecided();
        var (ok, _) = hub.Router.SetInputMode(workerId, InputModes.Hijack);
        Assert.True(ok);

        Assert.False(hub.Conn.SetWorkerHello(workerId, InputModes.Open));

        Assert.Equal(InputModes.Hijack, hub.Registry.Get(workerId)!.InputMode);
    }

    [Fact]
    public void AHelloMayStillRaiseOverADecision()
    {
        // One-directional: a worker announcing hijack tells the hub something it
        // does not otherwise know, that automation is driving the session.
        var (hub, workerId) = Undecided();
        var (ok, _) = hub.Router.SetInputMode(workerId, InputModes.Open);
        Assert.True(ok);

        Assert.True(hub.Conn.SetWorkerHello(workerId, InputModes.Hijack));

        Assert.Equal(InputModes.Hijack, hub.Registry.Get(workerId)!.InputMode);
    }

    [Fact]
    public void AgreementWithADecidedOpenIsNotADowngrade()
    {
        var (hub, workerId) = Undecided();
        var (ok, _) = hub.Router.SetInputMode(workerId, InputModes.Open);
        Assert.True(ok);

        Assert.True(hub.Conn.SetWorkerHello(workerId, InputModes.Open));
    }

    [Fact]
    public void ADecisionHoldsAcrossRepeatedReconnects()
    {
        // Why the flag lives on the worker state rather than the connection:
        // registry state outlives a worker socket.
        var (hub, workerId) = Undecided();
        var (ok, _) = hub.Router.SetInputMode(workerId, InputModes.Hijack);
        Assert.True(ok);

        for (var attempt = 0; attempt < 3; attempt++)
        {
            Assert.False(hub.Conn.SetWorkerHello(workerId, InputModes.Open));
        }

        Assert.Equal(InputModes.Hijack, hub.Registry.Get(workerId)!.InputMode);
    }

    [Fact]
    public void AHelloForAnUnknownWorkerIsRefused()
    {
        var hub = new TermHub(new TermHubConfig());

        Assert.False(hub.Conn.SetWorkerHello("nobody", InputModes.Open));
    }

    [Fact]
    public void AHelloRecordsTheNegotiatedProtocolVersion()
    {
        var (hub, workerId) = Undecided();

        Assert.True(hub.Conn.SetWorkerHello(workerId, InputModes.Open, 3));

        Assert.Equal(3, hub.Registry.Get(workerId)!.ProtocolVersion);
    }

    [Fact]
    public void ARefusedHelloDoesNotRecordAProtocolVersion()
    {
        // The refusal is a refusal of the whole frame, not of its mode alone.
        var (hub, workerId) = Undecided();
        var (ok, _) = hub.Router.SetInputMode(workerId, InputModes.Hijack);
        Assert.True(ok);

        Assert.False(hub.Conn.SetWorkerHello(workerId, InputModes.Open, 3));

        Assert.Null(hub.Registry.Get(workerId)!.ProtocolVersion);
    }
}
