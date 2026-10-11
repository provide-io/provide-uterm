//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using Provide.Uterm.Server;
using Provide.Uterm.Tests.Coverage100;
using Provide.Uterm.Vnc;
using Xunit;

namespace Provide.Uterm.Tests.Vnc;

/// <summary>
/// The relay's client-fault arms when both pumps end inside the drain window:
/// <see cref="HumanRelay.RelayAsync"/> rethrows the faulted client pump, and
/// <see cref="UtermServer.RunHumanVncRelayAsync"/> swallows that error and
/// closes. Each test sets an infinite, async-local drain timeout, so the drain
/// branch is the only one that can be taken: no timer can win the race.
/// </summary>
public sealed class HumanRelayClientFaultTests
{
    /// <summary>RFB 3.8 version followed by security type 2, which the filter refuses.</summary>
    private static byte[] BadSecurityHandshake()
    {
        var raw = new byte[13];
        System.Text.Encoding.ASCII.GetBytes("RFB 003.008\n").CopyTo(raw, 0);
        raw[12] = 2;
        return raw;
    }

    [Fact]
    public async Task A_client_filter_fault_is_rethrown_once_both_pumps_have_drained()
    {
        HumanRelay.DrainTimeout = Timeout.InfiniteTimeSpan;
        using var clientSrc = new MemoryStream(BadSecurityHandshake());
        using var upstreamDst = new MemoryStream();
        using var upstreamSrc = new MemoryStream(); // immediate EOF: the server pump ends cleanly
        using var clientDst = new MemoryStream();
        var announced = 0;

        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() => HumanRelay.RelayAsync(
                clientSrc, upstreamDst, upstreamSrc, clientDst,
                canInject: null, "s", "l", "p", "operator",
                onFirstCompletion: () =>
                {
                    announced++;
                    return Task.CompletedTask;
                }));

        Assert.Equal("unsupported security type 2", ex.Message);
        Assert.Equal(1, announced);
        // The version was forwarded before the security byte was refused.
        Assert.Equal(12, upstreamDst.Length);
    }

    [Fact]
    public async Task The_websocket_relay_swallows_a_client_filter_fault_and_closes()
    {
        HumanRelay.DrainTimeout = Timeout.InfiniteTimeSpan;
        var ws = new ScriptedWebSocket();
        ws.Enqueue(WebSocketMessageType.Binary, BadSecurityHandshake());
        var upstream = new ScriptedDuplexStream();

        var relay = UtermServer.RunHumanVncRelayAsync(ws, upstream, "s", "l", "p", "operator", CancellationToken.None);

        // The client pump's receive has been entered with the bad handshake already
        // queued, so it is delivered whatever the socket's state becomes; only now
        // let the server side end, so the drain completes with a faulted client pump.
        await ws.FirstReceivePending.Task;
        upstream.EnqueueEof();
        await relay;

        Assert.Equal(12, upstream.Written.Length);
        // One close announcement when the first pump ended; the socket was then
        // CloseSent, so the final close was skipped.
        Assert.Equal(1, ws.CloseCalls);
        Assert.Equal(WebSocketState.CloseSent, ws.State);
    }

    [Fact]
    public async Task The_drain_timeout_override_stays_in_the_flow_that_set_it()
    {
        Assert.Equal(TimeSpan.FromSeconds(2), HumanRelay.DrainTimeout);

        await SetInChildFlowAsync();

        Assert.Equal(TimeSpan.FromSeconds(2), HumanRelay.DrainTimeout);

        static async Task SetInChildFlowAsync()
        {
            HumanRelay.DrainTimeout = TimeSpan.FromMilliseconds(7);
            await Task.Yield();
            Assert.Equal(TimeSpan.FromMilliseconds(7), HumanRelay.DrainTimeout);
        }
    }
}
