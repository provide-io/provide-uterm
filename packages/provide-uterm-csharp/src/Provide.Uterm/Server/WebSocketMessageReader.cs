//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Buffers;
using System.Net.WebSockets;

namespace Provide.Uterm.Server;

/// <summary>A complete bounded WebSocket message assembled from one or more receive fragments.</summary>
internal sealed record WebSocketMessage(
    WebSocketMessageType MessageType,
    byte[] Payload,
    WebSocketCloseStatus? CloseStatus = null,
    string? CloseStatusDescription = null)
{
    public bool IsClose => MessageType == WebSocketMessageType.Close;
}

/// <summary>A protocol refusal that carries the close status the endpoint must send.</summary>
internal sealed class WebSocketMessageException : Exception
{
    public WebSocketMessageException(WebSocketCloseStatus closeStatus, string message) : base(message) =>
        CloseStatus = closeStatus;

    public WebSocketCloseStatus CloseStatus { get; }
}

/// <summary>Reads one complete WebSocket message while enforcing a total payload cap.</summary>
internal static class WebSocketMessageReader
{
    public static async Task<WebSocketMessage> ReadAsync(
        WebSocket socket,
        int maxMessageBytes,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(socket);
        if (maxMessageBytes < 1) throw new ArgumentOutOfRangeException(nameof(maxMessageBytes));

        var rented = ArrayPool<byte>.Shared.Rent(Math.Min(8192, maxMessageBytes));
        try
        {
            using var payload = new MemoryStream(Math.Min(8192, maxMessageBytes));
            WebSocketMessageType? messageType = null;
            while (true)
            {
                var result = await socket.ReceiveAsync(
                    new ArraySegment<byte>(rented), cancellationToken).ConfigureAwait(false);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    return new WebSocketMessage(
                        WebSocketMessageType.Close,
                        [],
                        result.CloseStatus ?? socket.CloseStatus,
                        result.CloseStatusDescription ?? socket.CloseStatusDescription);
                }

                if (messageType is null)
                {
                    messageType = result.MessageType;
                }
                else if (messageType != result.MessageType)
                {
                    throw new WebSocketMessageException(
                        WebSocketCloseStatus.InvalidMessageType,
                        "WebSocket message type changed between fragments.");
                }

                if (result.Count > maxMessageBytes - payload.Length)
                {
                    throw new WebSocketMessageException(
                        WebSocketCloseStatus.MessageTooBig,
                        $"WebSocket message exceeds the {maxMessageBytes}-byte limit.");
                }

                payload.Write(rented, 0, result.Count);
                if (result.EndOfMessage)
                {
                    return new WebSocketMessage(messageType.Value, payload.ToArray());
                }
            }
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(rented);
        }
    }
}
