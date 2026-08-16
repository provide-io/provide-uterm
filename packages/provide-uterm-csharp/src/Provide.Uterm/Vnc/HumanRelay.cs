//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vnc;

/// <summary>
/// Pure stream dual-pump for human VNC relay (Go <c>ServeHumanRelay</c> semantics
/// without gRPC/WebSocket). Client→server is RFB-filtered; server→client is raw copy.
/// </summary>
public static class HumanRelay
{
    /// <summary>
    /// Client→server RFB input filter. Null <paramref name="canInject"/> fails closed
    /// (drops Key/Pointer/CutText). Useful for unit tests without ASP.NET.
    /// </summary>
    public static Task PumpClientToServer(
        Stream serverDst,
        Stream clientSrc,
        RfbInputFilter.CanInject? canInject,
        string sessionId,
        string leaseId,
        string principalId,
        string principalRole,
        CancellationToken cancellationToken = default) =>
        RfbInputFilter.FilterClientInputAsync(
            serverDst,
            clientSrc,
            canInject,
            sessionId,
            leaseId,
            principalId,
            principalRole,
            cancellationToken);

    /// <summary>Server→client raw byte copy (video/framebuffer path).</summary>
    public static Task PumpServerToClientAsync(
        Stream clientDst,
        Stream serverSrc,
        CancellationToken cancellationToken = default) =>
        serverSrc.CopyToAsync(clientDst, cancellationToken);

    /// <summary>
    /// Concurrent dual pump until either direction ends or faults; cancels the peer.
    /// Filter errors (e.g. bad security type) are rethrown.
    /// </summary>
    public static async Task RelayAsync(
        Stream clientSrc,
        Stream serverDst,
        Stream serverSrc,
        Stream clientDst,
        RfbInputFilter.CanInject? canInject,
        string sessionId,
        string leaseId,
        string principalId,
        string principalRole,
        CancellationToken cancellationToken = default)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        var token = linked.Token;

        var clientPump = Task.Run(
            async () =>
            {
                try
                {
                    await PumpClientToServer(
                        serverDst,
                        clientSrc,
                        canInject,
                        sessionId,
                        leaseId,
                        principalId,
                        principalRole,
                        token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (token.IsCancellationRequested)
                {
                    // peer finished first
                }
                catch (EndOfStreamException)
                {
                    // clean client EOF mid-message treated as end-of-relay
                }
                finally
                {
                    try
                    {
                        linked.Cancel();
                    }
                    catch (ObjectDisposedException)
                    {
                        // already torn down
                    }
                }
            },
            CancellationToken.None);

        var serverPump = Task.Run(
            async () =>
            {
                try
                {
                    await PumpServerToClientAsync(clientDst, serverSrc, token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (token.IsCancellationRequested)
                {
                    // peer finished first
                }
                catch (IOException)
                {
                    // upstream closed
                }
                finally
                {
                    try
                    {
                        linked.Cancel();
                    }
                    catch (ObjectDisposedException)
                    {
                        // already torn down
                    }
                }
            },
            CancellationToken.None);

        // Wait for first completion, then drain the other (bounded).
        var first = await Task.WhenAny(clientPump, serverPump).ConfigureAwait(false);
        try
        {
            linked.Cancel();
        }
        catch (ObjectDisposedException)
        {
            // ignore
        }

        var drain = Task.WhenAll(clientPump, serverPump);
        var finished = await Task.WhenAny(drain, Task.Delay(TimeSpan.FromSeconds(2), CancellationToken.None))
            .ConfigureAwait(false);
        if (ReferenceEquals(finished, drain))
        {
            // Observe exceptions from both; prefer client-pump filter errors.
            if (clientPump.IsFaulted)
            {
                clientPump.GetAwaiter().GetResult();
            }

            if (serverPump.IsFaulted)
            {
                serverPump.GetAwaiter().GetResult();
            }
        }
        else if (first.IsFaulted)
        {
            first.GetAwaiter().GetResult();
        }
    }
}
