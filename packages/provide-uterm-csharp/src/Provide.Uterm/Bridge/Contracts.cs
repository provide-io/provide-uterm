//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Bridge;

/// <summary>
/// Protocol-version range negotiation for the worker bridge hello handshake.
/// Port of packages/provide-uterm-go/bridge/contracts.go.
/// </summary>
public static class ProtocolContracts
{
    public const int MinProtocolVersion = 1;
    public const int MaxProtocolVersion = 1;
    public const int PreferredProtocolVersion = 1;
    public const int CurrentProtocolVersion = PreferredProtocolVersion;

    public static (int Version, bool Ok) NegotiateProtocolVersion(int clientMin, int clientMax)
    {
        var lo = Math.Max(clientMin, MinProtocolVersion);
        var hi = Math.Min(clientMax, MaxProtocolVersion);
        if (lo > hi)
        {
            return (0, false);
        }

        return (hi, true);
    }

    public static int Negotiate(int clientMin, int clientMax)
    {
        var (selected, ok) = NegotiateProtocolVersion(clientMin, clientMax);
        if (!ok)
        {
            throw new ProtocolMismatchException(clientMin, clientMax, MinProtocolVersion, MaxProtocolVersion);
        }

        return selected;
    }

    public static (int ClientMin, int ClientMax) ParseClientRange(IReadOnlyDictionary<string, object?> msg)
    {
        if (msg.TryGetValue("protocol", out var protoObj) &&
            protoObj is IDictionary<string, object?> proto)
        {
            var minObj = proto.TryGetValue("min", out var mn) ? mn : null;
            var maxObj = proto.TryGetValue("max", out var mx) ? mx : null;
            return (SafeInt(minObj, MinProtocolVersion, 1),
                SafeInt(maxObj, MaxProtocolVersion, 1));
        }

        if (msg.TryGetValue("protocol_version", out var raw))
        {
            var v = SafeInt(raw, 0, 0);
            if (v < 1)
            {
                v = 1;
            }

            return (v, v);
        }

        return (1, 1);
    }

    private static int SafeInt(object? raw, int fallback, int floor)
    {
        var v = raw switch
        {
            int i => i,
            long l => (int)l,
            double d => (int)d,
            _ => fallback,
        };
        return Math.Max(v, floor);
    }
}

public sealed class ProtocolMismatchException : Exception
{
    public int ClientMin { get; }
    public int ClientMax { get; }
    public int ServerMin { get; }
    public int ServerMax { get; }

    public ProtocolMismatchException(int clientMin, int clientMax, int serverMin, int serverMax)
        : base($"protocol_mismatch: client=[{clientMin},{clientMax}] server=[{serverMin},{serverMax}]")
    {
        ClientMin = clientMin;
        ClientMax = clientMax;
        ServerMin = serverMin;
        ServerMax = serverMax;
    }
}
