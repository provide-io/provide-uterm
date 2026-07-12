//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;

namespace Provide.Uterm.TunnelClient;

/// <summary>
/// Control-message builders for tunnel open/resize frames.
/// Port of packages/provide-uterm-go/tunnelclient/control.go — wire-compact JSON
/// matching Python json.dumps(..., separators=(",", ":")).
/// </summary>
public static class TunnelControl
{
    /// <summary>Build the control frame that opens a terminal channel.</summary>
    public static byte[] OpenTerminalFrame(int cols, int rows)
    {
        var msg = new Dictionary<string, object?>
        {
            ["type"] = "open",
            ["channel"] = 1,
            ["tunnel_type"] = "terminal",
            ["term_size"] = new object[] { cols, rows },
        };
        return TunnelCodec.EncodeControl(msg);
    }

    /// <summary>Build the terminal-resize control frame.</summary>
    public static byte[] ResizeFrame(int cols, int rows)
    {
        var msg = new Dictionary<string, object?>
        {
            ["type"] = "resize",
            ["channel"] = 1,
            ["cols"] = cols,
            ["rows"] = rows,
        };
        return TunnelCodec.EncodeControl(msg);
    }

    /// <summary>Build the control frame that opens a TCP relay channel.</summary>
    public static byte[] OpenTcpFrame(int localPort)
    {
        var msg = new Dictionary<string, object?>
        {
            ["type"] = "open",
            ["channel"] = (int)TunnelProtocol.ChannelTcp,
            ["tunnel_type"] = "tcp",
            ["local_port"] = localPort,
        };
        return TunnelCodec.EncodeControl(msg);
    }

    /// <summary>Build the control frame that opens an HTTP inspection channel.</summary>
    public static byte[] OpenHttpFrame(int localPort)
    {
        var msg = new Dictionary<string, object?>
        {
            ["type"] = "open",
            ["channel"] = (int)TunnelProtocol.ChannelHttp,
            ["tunnel_type"] = "http",
            ["local_port"] = localPort,
        };
        return TunnelCodec.EncodeControl(msg);
    }

    /// <summary>Compact JSON bytes for a control payload (no spaces).</summary>
    public static byte[] MarshalCompact(IReadOnlyDictionary<string, object?> msg) =>
        JsonSerializer.SerializeToUtf8Bytes(msg);
}
