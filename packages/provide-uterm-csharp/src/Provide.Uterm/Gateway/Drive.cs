//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Gateway;

/// <summary>
/// Bidirectional pump: local stream (TCP/SSH channel) ↔ remote terminal WebSocket.
/// Port of packages/provide-uterm-go/gateway pumpOnce / drive (core path).
/// </summary>
public static class GatewayDrive
{
    /// <summary>
    /// Run one session for an accepted TCP client against <paramref name="wsUrl"/>.
    /// </summary>
    public static async Task RunAsync(
        TcpClient client,
        string wsUrl,
        CancellationToken cancellationToken = default)
    {
        using var _ = client;
        await using var network = client.GetStream();
        await RunAsync(network, wsUrl, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// Run one session for an arbitrary local duplex stream (TCP or SSH channel)
    /// against <paramref name="wsUrl"/>.
    /// </summary>
    public static async Task RunAsync(
        Stream local,
        string wsUrl,
        CancellationToken cancellationToken = default)
    {
        using var ws = new ClientWebSocket();
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);

        await ws.ConnectAsync(new Uri(wsUrl), cts.Token).ConfigureAwait(false);

        var hello = ControlChannelCodec.EncodeControlFrame(GatewayPump.HelloFrame());
        await ws.SendAsync(
            Encoding.UTF8.GetBytes(hello),
            WebSocketMessageType.Text,
            true,
            cts.Token).ConfigureAwait(false);

        var st = new GatewayPump.ControlState();
        var decoder = new ControlFrameDecoder();
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cts.Token);
        var token = linked.Token;

        var localToWs = Task.Run(async () =>
        {
            var buf = new byte[4096];
            try
            {
                while (!token.IsCancellationRequested && ws.State == WebSocketState.Open)
                {
                    var n = await local.ReadAsync(buf.AsMemory(0, buf.Length), token)
                        .ConfigureAwait(false);
                    if (n <= 0)
                    {
                        break;
                    }

                    var channel = WsBytes.WsBytesToChannelStr(buf.AsSpan(0, n));
                    var encoded = ControlChannelCodec.EncodeTerminalData(channel);
                    await ws.SendAsync(
                        Encoding.UTF8.GetBytes(encoded),
                        WebSocketMessageType.Text,
                        true,
                        token).ConfigureAwait(false);
                }
            }
            catch
            {
                // peer closed
            }
            finally
            {
                linked.Cancel();
            }
        }, token);

        var wsToLocal = Task.Run(async () =>
        {
            var buf = new byte[16 * 1024];
            try
            {
                while (!token.IsCancellationRequested && ws.State == WebSocketState.Open)
                {
                    var result = await ws.ReceiveAsync(buf, token).ConfigureAwait(false);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        break;
                    }

                    if (result.Count == 0)
                    {
                        continue;
                    }

                    if (result.MessageType == WebSocketMessageType.Binary)
                    {
                        var slice = buf.AsSpan(0, result.Count).ToArray();
                        await local.WriteAsync(slice, token).ConfigureAwait(false);
                        await local.FlushAsync(token).ConfigureAwait(false);
                        continue;
                    }

                    var text = Encoding.UTF8.GetString(buf, 0, result.Count);
                    IReadOnlyList<Chunk> events;
                    try
                    {
                        events = decoder.Feed(text);
                    }
                    catch
                    {
                        var raw = Encoding.UTF8.GetBytes(text);
                        await local.WriteAsync(raw, token).ConfigureAwait(false);
                        await local.FlushAsync(token).ConfigureAwait(false);
                        continue;
                    }

                    foreach (var ev in events)
                    {
                        switch (ev)
                        {
                            case ControlChunk ctrl:
                                GatewayPump.HandleControlFrame(ctrl.Control, st, null);
                                break;
                            case DataChunk data:
                            {
                                var bytes = WsBytes.ChannelStrToBytes(data.Data);
                                bytes = TelnetWriteTransform(bytes);
                                await local.WriteAsync(bytes, token).ConfigureAwait(false);
                                await local.FlushAsync(token).ConfigureAwait(false);
                                break;
                            }
                        }
                    }
                }
            }
            catch
            {
                // peer closed
            }
            finally
            {
                linked.Cancel();
            }
        }, token);

        try
        {
            await Task.WhenAny(localToWs, wsToLocal).ConfigureAwait(false);
        }
        finally
        {
            linked.Cancel();
            try
            {
                if (ws.State == WebSocketState.Open)
                {
                    await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "bye", CancellationToken.None)
                        .ConfigureAwait(false);
                }
            }
            catch
            {
                // ignore
            }
        }
    }

    /// <summary>Telnet-side output transforms (DEL→BS, CRLF normalize).</summary>
    internal static byte[] TelnetWriteTransform(byte[] raw)
    {
        for (var i = 0; i < raw.Length; i++)
        {
            if (raw[i] == 0x7f)
            {
                raw[i] = 0x08;
            }
        }

        var latin1 = Encoding.GetEncoding("ISO-8859-1");
        var s = latin1.GetString(raw);
        s = s.Replace("\r\n", "\n", StringComparison.Ordinal);
        s = s.Replace("\n", "\r\n", StringComparison.Ordinal);
        return latin1.GetBytes(s);
    }
}
