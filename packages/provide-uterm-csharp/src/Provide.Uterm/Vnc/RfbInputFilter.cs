//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Buffers.Binary;

namespace Provide.Uterm.Vnc;

/// <summary>
/// RFB client→server input filter (Go <c>filterRFBInput</c> / Python
/// <c>filter_rfb_client_input</c> parity). Used for human VNC relay: pass-through
/// handshake and non-input messages; gate Key/Pointer/CutText on <see cref="CanInject"/>.
/// Null policy fails closed (drops inject messages).
/// </summary>
public static class RfbInputFilter
{
    public const int MaxCutText = 1 << 20;

    public delegate bool CanInject(string sessionId, string leaseId, string principalId, string principalRole);

    public static async Task FilterClientInputAsync(
        Stream dst,
        Stream src,
        CanInject? canInject,
        string sessionId,
        string leaseId,
        string principalId,
        string principalRole,
        CancellationToken cancellationToken = default)
    {
        // ProtocolVersion
        await CopyExactAsync(dst, src, 12, cancellationToken).ConfigureAwait(false);
        // Security type None only
        var sec = await ReadExactAsync(src, 1, cancellationToken).ConfigureAwait(false);
        if (sec[0] != 1)
        {
            throw new InvalidOperationException($"unsupported security type {sec[0]}");
        }

        dst.Write(sec);
        await CopyExactAsync(dst, src, 1, cancellationToken).ConfigureAwait(false);

        while (true)
        {
            byte[] msgType;
            try
            {
                msgType = await ReadExactAsync(src, 1, cancellationToken).ConfigureAwait(false);
            }
            catch (EndOfStreamException)
            {
                return;
            }

            switch (msgType[0])
            {
                case 0: // SetPixelFormat
                    dst.Write(msgType);
                    await CopyExactAsync(dst, src, 19, cancellationToken).ConfigureAwait(false);
                    break;
                case 2: // SetEncodings
                {
                    var header = await ReadExactAsync(src, 3, cancellationToken).ConfigureAwait(false);
                    var num = BinaryPrimitives.ReadUInt16BigEndian(header.AsSpan(1, 2));
                    dst.Write(msgType);
                    dst.Write(header);
                    if (num > 0)
                    {
                        await CopyExactAsync(dst, src, num * 4, cancellationToken).ConfigureAwait(false);
                    }

                    break;
                }
                case 3: // FramebufferUpdateRequest
                    dst.Write(msgType);
                    await CopyExactAsync(dst, src, 9, cancellationToken).ConfigureAwait(false);
                    break;
                case 4: // KeyEvent
                {
                    var payload = await ReadExactAsync(src, 7, cancellationToken).ConfigureAwait(false);
                    if (Allowed(canInject, sessionId, leaseId, principalId, principalRole))
                    {
                        dst.Write(msgType);
                        dst.Write(payload);
                    }

                    break;
                }
                case 5: // PointerEvent
                {
                    var payload = await ReadExactAsync(src, 5, cancellationToken).ConfigureAwait(false);
                    if (Allowed(canInject, sessionId, leaseId, principalId, principalRole))
                    {
                        dst.Write(msgType);
                        dst.Write(payload);
                    }

                    break;
                }
                case 6: // ClientCutText
                {
                    var header = await ReadExactAsync(src, 7, cancellationToken).ConfigureAwait(false);
                    var length = BinaryPrimitives.ReadUInt32BigEndian(header.AsSpan(3, 4));
                    if (length > MaxCutText)
                    {
                        throw new InvalidOperationException("ClientCutText too large");
                    }

                    var payload = length > 0
                        ? await ReadExactAsync(src, (int)length, cancellationToken).ConfigureAwait(false)
                        : Array.Empty<byte>();
                    if (Allowed(canInject, sessionId, leaseId, principalId, principalRole))
                    {
                        dst.Write(msgType);
                        dst.Write(header);
                        if (payload.Length > 0)
                        {
                            dst.Write(payload);
                        }
                    }

                    break;
                }
                default:
                    throw new InvalidOperationException($"unknown RFB client message type: {msgType[0]}");
            }
        }
    }

    private static bool Allowed(
        CanInject? canInject,
        string sessionId,
        string leaseId,
        string principalId,
        string principalRole) =>
        canInject is not null && canInject(sessionId, leaseId, principalId, principalRole);

    private static async Task<byte[]> ReadExactAsync(Stream src, int n, CancellationToken ct)
    {
        var buf = new byte[n];
        var off = 0;
        while (off < n)
        {
            ct.ThrowIfCancellationRequested();
            var read = await src.ReadAsync(buf.AsMemory(off, n - off), ct).ConfigureAwait(false);
            if (read <= 0)
            {
                throw new EndOfStreamException($"short read: want {n}, got {off}");
            }

            off += read;
        }

        return buf;
    }

    private static async Task CopyExactAsync(Stream dst, Stream src, int n, CancellationToken ct)
    {
        var buf = await ReadExactAsync(src, n, ct).ConfigureAwait(false);
        dst.Write(buf);
    }
}
