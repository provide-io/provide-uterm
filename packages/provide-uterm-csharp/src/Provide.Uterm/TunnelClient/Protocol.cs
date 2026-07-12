//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;

namespace Provide.Uterm.TunnelClient;

/// <summary>
/// Tunnel framed binary WebSocket protocol: [channel][flags][payload...].
/// Port of packages/provide-uterm-go/tunnelclient/protocol.go.
/// </summary>
public static class TunnelProtocol
{
    public const byte ChannelControl = 0x00;
    public const byte ChannelData = 0x01;
    public const byte ChannelTcp = 0x02;
    public const byte ChannelHttp = 0x03;

    public const byte FlagData = 0x00;
    public const byte FlagEof = 0x01;
}

/// <summary>Decoded tunnel frame.</summary>
public readonly struct TunnelFrame
{
    public byte Channel { get; init; }
    public byte Flags { get; init; }
    public byte[] Payload { get; init; }

    public bool IsEof => (Flags & TunnelProtocol.FlagEof) != 0;
    public bool IsControl => Channel == TunnelProtocol.ChannelControl;
}

/// <summary>Frame encode/decode helpers.</summary>
public static class TunnelCodec
{
    public static byte[] EncodeFrame(byte channel, ReadOnlySpan<byte> payload, byte flags = TunnelProtocol.FlagData)
    {
        var outBuf = new byte[2 + payload.Length];
        outBuf[0] = channel;
        outBuf[1] = flags;
        payload.CopyTo(outBuf.AsSpan(2));
        return outBuf;
    }

    public static TunnelFrame DecodeFrame(ReadOnlySpan<byte> data)
    {
        if (data.Length < 2)
        {
            throw new ArgumentException("tunnelclient: frame too short");
        }

        return new TunnelFrame
        {
            Channel = data[0],
            Flags = data[1],
            Payload = data[2..].ToArray(),
        };
    }

    public static byte[] EncodeControl(IReadOnlyDictionary<string, object?> msg)
    {
        if (!msg.ContainsKey("type"))
        {
            throw new ArgumentException("tunnelclient: control message must have a 'type' key");
        }

        var payload = JsonSerializer.SerializeToUtf8Bytes(msg);
        return EncodeFrame(TunnelProtocol.ChannelControl, payload);
    }

    public static byte[] EncodeControlBytes(ReadOnlySpan<byte> payload) =>
        EncodeFrame(TunnelProtocol.ChannelControl, payload);

    public static Dictionary<string, object?> DecodeControl(ReadOnlySpan<byte> payload)
    {
        using var doc = JsonDocument.Parse(payload.ToArray());
        if (doc.RootElement.ValueKind != JsonValueKind.Object)
        {
            throw new FormatException("tunnelclient: invalid control payload");
        }

        return ControlChannel.ControlChannelCodec.JsonElementToDictionary(doc.RootElement);
    }
}
