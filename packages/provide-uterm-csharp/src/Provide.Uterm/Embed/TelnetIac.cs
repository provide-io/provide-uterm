//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Filters;

namespace Provide.Uterm.Embed;

/// <summary>Parsed telnet control event from the wire (not application payload).</summary>
public readonly struct TelnetControlEvent
{
    public bool IsSubnegotiation { get; init; }
    public byte Command { get; init; }
    public byte Option { get; init; }
    public byte[] SubPayload { get; init; }
}

/// <summary>
/// RFC 854 IAC parser aligned with Go/Python <c>parseTelnetBuffer</c>.
/// Separates application bytes from negotiation/subnegotiation events.
/// </summary>
public static class TelnetIac
{
    public const byte OptBinary = 0;
    public const byte OptEcho = 1;
    public const byte OptSga = 3;
    public const byte OptTtype = 24;
    public const byte OptNaws = 31;

    /// <summary>IAC-escape 0xFF in application payload for send.</summary>
    public static byte[] Escape(ReadOnlySpan<byte> data)
    {
        var list = new List<byte>(data.Length + 4);
        foreach (var b in data)
        {
            list.Add(b);
            if (b == InputFilters.Iac)
            {
                list.Add(InputFilters.Iac);
            }
        }

        return list.ToArray();
    }

    /// <summary>
    /// Parse complete sequences from <paramref name="buf"/>.
    /// Returns application payload, control events, and bytes consumed.
    /// Incomplete trailing sequences are left unconsumed unless <paramref name="final"/>.
    /// </summary>
    public static (byte[] Payload, IReadOnlyList<TelnetControlEvent> Events, int Consumed) Parse(
        ReadOnlySpan<byte> buf,
        bool final = false)
    {
        var payload = new List<byte>(buf.Length);
        var events = new List<TelnetControlEvent>();
        var i = 0;
        var consumed = 0;

        while (i < buf.Length)
        {
            if (buf[i] != InputFilters.Iac)
            {
                payload.Add(buf[i]);
                i++;
                consumed = i;
                continue;
            }

            if (i + 1 >= buf.Length)
            {
                if (final)
                {
                    payload.Add(InputFilters.Iac);
                    i++;
                    consumed = i;
                }

                break;
            }

            var cmd = buf[i + 1];
            if (cmd is InputFilters.Do or InputFilters.Dont or InputFilters.Will or InputFilters.Wont)
            {
                if (i + 2 >= buf.Length)
                {
                    if (final)
                    {
                        for (var k = i; k < buf.Length; k++)
                        {
                            payload.Add(buf[k]);
                        }

                        i = buf.Length;
                        consumed = i;
                    }

                    break;
                }

                events.Add(new TelnetControlEvent
                {
                    IsSubnegotiation = false,
                    Command = cmd,
                    Option = buf[i + 2],
                    SubPayload = Array.Empty<byte>(),
                });
                i += 3;
                consumed = i;
                continue;
            }

            if (cmd == InputFilters.Sb)
            {
                var end = FindSubnegEnd(buf, i + 2);
                if (end < 0)
                {
                    if (final)
                    {
                        for (var k = i; k < buf.Length; k++)
                        {
                            payload.Add(buf[k]);
                        }

                        i = buf.Length;
                        consumed = i;
                    }

                    break;
                }

                var bodyStart = i + 2;
                var bodyEnd = end - 2; // before IAC SE
                var body = bodyStart < bodyEnd
                    ? buf[bodyStart..bodyEnd].ToArray()
                    : Array.Empty<byte>();
                var opt = body.Length > 0 ? body[0] : (byte)0;
                var sub = body.Length > 1 ? body[1..] : Array.Empty<byte>();
                events.Add(new TelnetControlEvent
                {
                    IsSubnegotiation = true,
                    Command = InputFilters.Sb,
                    Option = opt,
                    SubPayload = sub,
                });
                i = end;
                consumed = i;
                continue;
            }

            if (cmd == InputFilters.Iac)
            {
                payload.Add(InputFilters.Iac);
                i += 2;
                consumed = i;
                continue;
            }

            // Unknown 2-byte IAC command — skip
            i += 2;
            consumed = i;
        }

        return (payload.ToArray(), events, consumed);
    }

    private static int FindSubnegEnd(ReadOnlySpan<byte> buf, int start)
    {
        for (var j = start; j < buf.Length - 1; j++)
        {
            if (buf[j] == InputFilters.Iac && buf[j + 1] == InputFilters.Se)
            {
                return j + 2;
            }
        }

        return -1;
    }
}
