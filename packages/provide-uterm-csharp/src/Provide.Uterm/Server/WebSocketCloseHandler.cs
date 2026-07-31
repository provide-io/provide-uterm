//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;

namespace Provide.Uterm.Server;

/// <summary>Sends a bounded close frame, acknowledges peer closes, and guarantees transport termination.</summary>
internal static class WebSocketCloseHandler
{
    private static readonly TimeSpan DefaultTimeout = TimeSpan.FromMilliseconds(250);

    public static async Task CloseAndTerminateAsync(
        WebSocket socket,
        WebSocketCloseStatus status,
        string? description,
        TimeSpan? timeout = null)
    {
        ArgumentNullException.ThrowIfNull(socket);
        var state = socket.State;
        if (state is WebSocketState.Open or WebSocketState.CloseReceived)
        {
            Task? closeTask = null;
            try
            {
                closeTask = socket.CloseOutputAsync(status, description, CancellationToken.None);
                await closeTask.WaitAsync(timeout ?? DefaultTimeout).ConfigureAwait(false);
            }
            catch
            {
                ObserveEventualFault(closeTask);
            }
        }

        if (socket.State is not (WebSocketState.Closed or WebSocketState.Aborted))
        {
            socket.Abort();
        }
    }

    private static void ObserveEventualFault(Task? task)
    {
        if (task is null) return;
        _ = task.ContinueWith(
            static completed => _ = completed.Exception,
            CancellationToken.None,
            TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously,
            TaskScheduler.Default);
    }
}
