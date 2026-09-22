//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using Provide.Uterm.Defaults;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Tests;

/// <summary>
/// <see cref="ReconnectingTransport"/>: port of packages/provide-uterm-go/transports
/// reconnect_test.go and reconnect_close_test.go (the latter itself the port of Python's
/// test_reconnect_last_close.py). The shared corpus is in
/// <see cref="TermSessionReconnectGoldenTests"/>; these are the transport-level cases.
/// </summary>
public class TermSessionReconnectingTransportTests
{
    private static readonly TimeSpan Ms500 = TimeSpan.FromMilliseconds(500);
    private static readonly TimeSpan OneSecond = TimeSpan.FromSeconds(1);

    private sealed class StubTransport(params byte[][] receives) : IConnectionTransport
    {
        private readonly Queue<byte[]> _receives = new(receives);
        private bool _connected;

        public Exception? ConnectError { get; init; }
        public Exception? SendError { get; set; }
        public Exception? ReceiveError { get; init; }
        public Exception? DisconnectError { get; init; }
        public int DisconnectCount { get; private set; }
        public List<byte[]> Sent { get; } = [];

        public Task ConnectAsync(
            string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            if (ConnectError is not null)
            {
                throw ConnectError;
            }

            _connected = true;
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            DisconnectCount++;
            _connected = false;
            return DisconnectError is null ? Task.CompletedTask : throw DisconnectError;
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
        {
            if (SendError is not null)
            {
                throw SendError;
            }

            Sent.Add(data);
            return Task.CompletedTask;
        }

        public Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            if (ReceiveError is not null)
            {
                throw ReceiveError;
            }

            return Task.FromResult(_receives.Count > 0 ? _receives.Dequeue() : Array.Empty<byte>());
        }

        public bool IsConnected() => _connected;
    }

    private static Func<IConnectionTransport> Factory(params StubTransport[] stubs)
    {
        var index = 0;
        return () => stubs[index++];
    }

    private static (Func<TimeSpan, CancellationToken, Task> Sleep, List<TimeSpan> Calls) Recorder()
    {
        var calls = new List<TimeSpan>();
        return ((delay, _) =>
        {
            calls.Add(delay);
            return Task.CompletedTask;
        }, calls);
    }

    private static ReconnectingTransport Wrap(
        Func<IConnectionTransport> factory, ReconnectPolicy? policy = null, Func<TimeSpan, CancellationToken, Task>? sleep = null) =>
        new(factory, new ReconnectingOptions { Policy = policy, Sleep = sleep ?? Recorder().Sleep });

    private static StubTransport Failing(Exception send) => new() { SendError = send };

    private static readonly IOException Dropped = new("dropped");

    // ---- policy --------------------------------------------------------------

    [Fact]
    public void PolicyDelayIsBoundedExponential()
    {
        var p = new ReconnectPolicy(3, Ms500, OneSecond);
        Assert.Equal(new[] { Ms500, Ms500, OneSecond, OneSecond }, new[] { p.Delay(0), p.Delay(1), p.Delay(2), p.Delay(3) });
        Assert.Equal(Ms500, p.Delay(-4));
        Assert.Equal(OneSecond, p.Delay(5000));
    }

    [Fact]
    public void AZeroBaseNeverBacksOffHoweverFarTheExponentRuns()
    {
        var p = new ReconnectPolicy(3, TimeSpan.Zero, OneSecond);
        Assert.Equal(TimeSpan.Zero, p.Delay(1));
        Assert.Equal(TimeSpan.Zero, p.Delay(5000));
    }

    [Fact]
    public void TheDefaultPolicyTracksTerminalDefaults()
    {
        var p = ReconnectPolicy.Default;
        Assert.Equal(TerminalDefaults.ReconnectMaxRetries, p.MaxRetries);
        Assert.Equal(TimeSpan.FromSeconds(TerminalDefaults.ReconnectBaseBackoffS), p.BaseBackoff);
        Assert.Equal(TimeSpan.FromSeconds(TerminalDefaults.ReconnectMaxBackoffS), p.MaxBackoff);
    }

    // ---- connect -------------------------------------------------------------

    [Fact]
    public async Task ConnectBacksOffBetweenFailedAttempts()
    {
        StubTransport Down() => new() { ConnectError = new IOException("unavailable") };
        var ok = new StubTransport();
        var (sleep, calls) = Recorder();
        var rt = Wrap(Factory(Down(), Down(), Down(), ok), new ReconnectPolicy(3, Ms500, OneSecond), sleep);

        await rt.ConnectAsync("h", 1);

        Assert.Equal(new[] { Ms500, OneSecond, OneSecond }, calls);
        Assert.Same(ok, rt.Inner);
        Assert.True(rt.IsConnected());
    }

    [Fact]
    public async Task ConnectGivesUpWhenTheBudgetRunsOut()
    {
        var last = new IOException("second");
        var rt = Wrap(
            Factory(new StubTransport { ConnectError = new IOException("first") }, new StubTransport { ConnectError = last }),
            new ReconnectPolicy(1, TimeSpan.Zero, TimeSpan.Zero));

        var err = await Assert.ThrowsAsync<RetriesExhaustedException>(() => rt.ConnectAsync("h", 1));

        Assert.Equal(RetriesExhaustedException.ConnectMessage, err.Message);
        Assert.Same(last, err.InnerException);
        Assert.Null(rt.Inner);
    }

    [Fact]
    public async Task ConnectPassesTheTargetToEveryAttempt()
    {
        var seen = new List<(string, int, ConnectOptions?)>();
        var options = new ConnectOptions { Cols = 132 };
        var rt = new ReconnectingTransport(() => new TargetRecorder(seen));
        await rt.ConnectAsync("example.test", 2323, options);
        Assert.Equal(new[] { ("example.test", 2323, (ConnectOptions?)options) }, seen);
    }

    private sealed class TargetRecorder(List<(string, int, ConnectOptions?)> seen) : IConnectionTransport
    {
        public Task ConnectAsync(
            string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            seen.Add((host, port, options));
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default) => Task.CompletedTask;

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default) => Task.CompletedTask;

        public Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default) =>
            Task.FromResult(Array.Empty<byte>());

        public bool IsConnected() => true;
    }

    [Fact]
    public async Task ASleepFailureDuringConnectPropagates()
    {
        var sleepErr = new InvalidOperationException("cancelled");
        var rt = Wrap(
            Factory(new StubTransport { ConnectError = Dropped }, new StubTransport { ConnectError = Dropped }),
            new ReconnectPolicy(3, OneSecond, OneSecond),
            (_, _) => throw sleepErr);
        Assert.Same(sleepErr, await Assert.ThrowsAsync<InvalidOperationException>(() => rt.ConnectAsync("h", 1)));
    }

    [Fact]
    public async Task CancellingDuringTheConnectBackoffStopsWithTheDefaultSleeper()
    {
        using var cts = new CancellationTokenSource();
        await cts.CancelAsync();
        var attempts = 0;
        var rt = new ReconnectingTransport(
            () =>
            {
                attempts++;
                return new StubTransport { ConnectError = Dropped };
            },
            new ReconnectingOptions { Policy = new ReconnectPolicy(5, TimeSpan.FromHours(1), TimeSpan.FromHours(1)) });

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => rt.ConnectAsync("h", 1, null, cts.Token));
        Assert.Equal(1, attempts);
    }

    [Fact]
    public async Task ACancelledConnectIsNotRetried()
    {
        using var cts = new CancellationTokenSource();
        await cts.CancelAsync();
        var attempts = 0;
        var rt = new ReconnectingTransport(() =>
        {
            attempts++;
            return new StubTransport { ConnectError = new OperationCanceledException(cts.Token) };
        });

        await Assert.ThrowsAsync<OperationCanceledException>(() => rt.ConnectAsync("h", 1, null, cts.Token));
        Assert.Equal(1, attempts);
    }

    [Fact]
    public async Task AnOperationCanceledExceptionWithoutCancellationIsJustAFailedAttempt()
    {
        var ok = new StubTransport();
        var rt = Wrap(Factory(new StubTransport { ConnectError = new OperationCanceledException() }, ok));
        await rt.ConnectAsync("h", 1);
        Assert.Same(ok, rt.Inner);
    }

    [Fact]
    public void TheFactoryIsRequired() =>
        Assert.Throws<ArgumentNullException>(() => new ReconnectingTransport(null!));

    // ---- send / receive -------------------------------------------------------

    [Fact]
    public async Task ASendThatDropsReconnectsAndCallsTheHook()
    {
        var failing = Failing(Dropped);
        var recovered = new StubTransport();
        var hooked = new List<IConnectionTransport>();
        var rt = new ReconnectingTransport(
            Factory(failing, recovered),
            new ReconnectingOptions
            {
                Sleep = Recorder().Sleep,
                OnReconnect = (inner, _) =>
                {
                    hooked.Add(inner);
                    return Task.CompletedTask;
                },
            });
        await rt.ConnectAsync("h", 1);

        await rt.SendAsync("A"u8.ToArray());

        Assert.Equal(new IConnectionTransport[] { recovered }, hooked);
        Assert.Equal(1, failing.DisconnectCount);
        Assert.Equal("A"u8.ToArray(), Assert.Single(recovered.Sent));
        Assert.Same(recovered, rt.Inner);
    }

    [Fact]
    public async Task ASendThatKeepsDroppingExhaustsTheBudget()
    {
        var first = Failing(Dropped);
        var second = Failing(Dropped);
        var rt = Wrap(Factory(first, second), new ReconnectPolicy(1, TimeSpan.Zero, TimeSpan.Zero));
        await rt.ConnectAsync("h", 1);

        var err = await Assert.ThrowsAsync<RetriesExhaustedException>(() => rt.SendAsync([1]));

        Assert.Equal(RetriesExhaustedException.ReconnectMessage, err.Message);
        Assert.Same(Dropped, err.InnerException);
        Assert.Equal(1, second.DisconnectCount);
    }

    [Fact]
    public async Task GivingUpStillThrowsTheBudgetErrorWhenTheCloseFails()
    {
        var failing = new StubTransport { SendError = Dropped, DisconnectError = new IOException("already gone") };
        var rt = Wrap(Factory(failing), new ReconnectPolicy(0, TimeSpan.Zero, TimeSpan.Zero));
        await rt.ConnectAsync("h", 1);

        var err = await Assert.ThrowsAsync<RetriesExhaustedException>(() => rt.SendAsync([1]));

        Assert.Same(Dropped, err.InnerException);
        Assert.Equal(1, failing.DisconnectCount);
    }

    [Fact]
    public async Task ALogicErrorIsNotReconnected()
    {
        var logic = new ArgumentException("not a transport failure");
        var failing = Failing(logic);
        var rt = Wrap(Factory(failing));
        await rt.ConnectAsync("h", 1);

        Assert.Same(logic, await Assert.ThrowsAsync<ArgumentException>(() => rt.SendAsync([1])));
        Assert.Equal(0, failing.DisconnectCount);
    }

    [Fact]
    public async Task ACustomClassifierDecidesWhatReconnects()
    {
        var logic = new ArgumentException("retry me anyway");
        var recovered = new StubTransport();
        var rt = new ReconnectingTransport(
            Factory(Failing(logic), recovered),
            new ReconnectingOptions { Sleep = Recorder().Sleep, IsRetryable = ex => ex is ArgumentException });
        await rt.ConnectAsync("h", 1);

        await rt.SendAsync([7]);

        Assert.Single(recovered.Sent);
    }

    [Fact]
    public async Task AReceiveThatDropsReconnectsAndDelegates()
    {
        var failing = new StubTransport { ReceiveError = new WebSocketException("gone") };
        var recovered = new StubTransport("hello"u8.ToArray());
        var rt = Wrap(Factory(failing, recovered));
        await rt.ConnectAsync("h", 1);

        Assert.Equal("hello"u8.ToArray(), await rt.ReceiveAsync(128, TimeSpan.FromMilliseconds(10)));
    }

    [Fact]
    public async Task AReconnectWithAZeroDelayDoesNotSleep()
    {
        // BaseBackoff > 0 but MaxBackoff 0: the computed delay is zero, so no sleep.
        var (sleep, calls) = Recorder();
        var rt = Wrap(Factory(Failing(Dropped), new StubTransport()), new ReconnectPolicy(2, Ms500, TimeSpan.Zero), sleep);
        await rt.ConnectAsync("h", 1);

        await rt.SendAsync([1]);

        Assert.Empty(calls);
    }

    [Fact]
    public async Task ReconnectBacksOffByAttempt()
    {
        var (sleep, calls) = Recorder();
        var rt = Wrap(
            Factory(Failing(Dropped), Failing(Dropped), new StubTransport()), new ReconnectPolicy(5, Ms500, OneSecond), sleep);
        await rt.ConnectAsync("h", 1);

        await rt.SendAsync([1]);

        Assert.Equal(new[] { Ms500, OneSecond }, calls);
    }

    [Fact]
    public async Task ASleepFailureDuringTheReconnectBackoffPropagates()
    {
        var sleepErr = new InvalidOperationException("cancelled during backoff");
        var rt = Wrap(
            Factory(Failing(Dropped), new StubTransport()), new ReconnectPolicy(2, OneSecond, OneSecond), (_, _) => throw sleepErr);
        await rt.ConnectAsync("h", 1);

        Assert.Same(sleepErr, await Assert.ThrowsAsync<InvalidOperationException>(() => rt.SendAsync([1])));
    }

    [Fact]
    public async Task AServerThatWillNotComeBackExhaustsTheReconnect()
    {
        var rt = Wrap(
            Factory(
                Failing(Dropped),
                new StubTransport { ConnectError = new IOException("dial failed") },
                new StubTransport { ConnectError = new IOException("dial failed") }),
            new ReconnectPolicy(1, TimeSpan.Zero, TimeSpan.Zero));
        await rt.ConnectAsync("h", 1);

        var err = await Assert.ThrowsAsync<RetriesExhaustedException>(() => rt.SendAsync([1]));
        Assert.Equal(RetriesExhaustedException.ReconnectMessage, err.Message);
    }

    [Fact]
    public async Task ACancelledCloseOfTheDroppedTransportStopsTheReconnect()
    {
        using var cts = new CancellationTokenSource();
        await cts.CancelAsync();
        var failing = new StubTransport { SendError = Dropped, DisconnectError = new OperationCanceledException(cts.Token) };
        var attempts = 0;
        var rt = new ReconnectingTransport(
            () =>
            {
                attempts++;
                return failing;
            },
            new ReconnectingOptions { Sleep = Recorder().Sleep });
        await rt.ConnectAsync("h", 1);

        await Assert.ThrowsAsync<OperationCanceledException>(() => rt.SendAsync([1], cts.Token));
        Assert.Equal(1, attempts);
    }

    // ---- lifecycle -------------------------------------------------------------

    [Fact]
    public async Task TheLifecycleFollowsTheInnerTransport()
    {
        var stub = new StubTransport();
        var rt = new ReconnectingTransport(Factory(stub));

        Assert.False(rt.IsConnected());
        Assert.Same(TransportErrors.NotConnected, await Assert.ThrowsAsync<InvalidOperationException>(() => rt.SendAsync([1])));
        Assert.Same(
            TransportErrors.NotConnected,
            await Assert.ThrowsAsync<InvalidOperationException>(() => rt.ReceiveAsync(1, TimeSpan.Zero)));

        await rt.ConnectAsync("h", 1);
        Assert.True(rt.IsConnected());

        await rt.DisconnectAsync();
        Assert.False(rt.IsConnected());
        Assert.Null(rt.Inner);
        Assert.Equal(1, stub.DisconnectCount);

        await rt.DisconnectAsync();
        Assert.Equal(1, stub.DisconnectCount);
    }

    [Fact]
    public async Task ADisconnectedInnerTransportReadsAsDisconnected()
    {
        var stub = new StubTransport();
        var rt = new ReconnectingTransport(Factory(stub));
        await rt.ConnectAsync("h", 1);
        await stub.DisconnectAsync();
        Assert.False(rt.IsConnected());
    }

    // ---- classification --------------------------------------------------------

    [Fact]
    public void TransportFailuresAreRetryableAndLogicErrorsAreNot()
    {
        Assert.True(ReconnectingTransport.IsRetryable(new TransportClosedException("x", new TransportClose(CloseInitiator.Remote))));
        Assert.True(ReconnectingTransport.IsRetryable(TransportErrors.ConnectionClosed));
        Assert.True(ReconnectingTransport.IsRetryable(TransportErrors.NotConnected));
        Assert.True(ReconnectingTransport.IsRetryable(new WebSocketException("gone")));
        Assert.False(ReconnectingTransport.IsRetryable(new InvalidOperationException("not connected")));
        Assert.False(ReconnectingTransport.IsRetryable(new OperationCanceledException()));
    }

    // ---- LastClose (port of reconnect_close_test.go) --------------------------------

    [Fact]
    public async Task AReconnectKeepsTheCloseThatCausedIt()
    {
        var goingAway = new TransportClose(CloseInitiator.Remote, 1001, "going away");
        var rt = Wrap(Factory(Failing(new TransportClosedException("connection closed", goingAway)), new StubTransport()));
        await rt.ConnectAsync("h", 1);
        Assert.Null(rt.LastClose);

        await rt.SendAsync("hello"u8.ToArray());

        Assert.Equal("remote close 1001 going away", rt.LastClose?.Summary());
    }

    [Fact]
    public async Task AReconnectOnAnUntypedErrorLeavesNoLastClose()
    {
        var rt = Wrap(Factory(Failing(TransportErrors.ConnectionClosed), new StubTransport()));
        await rt.ConnectAsync("h", 1);

        await rt.SendAsync("hello"u8.ToArray());

        Assert.Null(rt.LastClose);
    }

    [Fact]
    public async Task AnUntypedDropKeepsTheEarlierTypedClose()
    {
        var typed = new TransportClose(CloseInitiator.Local, 1011, "keepalive ping timeout");
        var rt = Wrap(Factory(
            Failing(new TransportClosedException("connection closed", typed)), Failing(Dropped), new StubTransport()));
        await rt.ConnectAsync("h", 1);

        await rt.SendAsync([1]);

        Assert.Same(typed, rt.LastClose);
    }

    [Fact]
    public async Task TheLatestTypedCloseWins()
    {
        var first = new TransportClose(CloseInitiator.Remote, 1001, "going away");
        var second = new TransportClose(CloseInitiator.Unknown, Detail: "reset");
        var rt = Wrap(Factory(
            Failing(new TransportClosedException("a", first)),
            Failing(new TransportClosedException("b", second)),
            new StubTransport()));
        await rt.ConnectAsync("h", 1);

        await rt.SendAsync([1]);

        Assert.Same(second, rt.LastClose);
    }

    [Fact]
    public async Task AnExhaustedReconnectStillRecordsTheClose()
    {
        var lost = new TransportClose(CloseInitiator.Unknown, Detail: "reset");
        var rt = Wrap(
            Factory(new StubTransport { ReceiveError = new TransportClosedException("connection lost", lost) }),
            new ReconnectPolicy(0, TimeSpan.Zero, TimeSpan.Zero));
        await rt.ConnectAsync("h", 1);

        await Assert.ThrowsAsync<RetriesExhaustedException>(() => rt.ReceiveAsync(16, TimeSpan.FromMilliseconds(1)));

        Assert.Equal("unknown close (reset)", rt.LastClose?.Summary());
    }
}
