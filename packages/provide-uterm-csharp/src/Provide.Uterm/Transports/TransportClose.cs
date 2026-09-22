//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Sockets;

namespace Provide.Uterm.Transports;

/// <summary>Which side ended a connection. Port of provide.uterm.transport_close.CloseInitiator.</summary>
public enum CloseInitiator
{
    Local,
    Remote,
    Unknown,
}

/// <summary>A WebSocket close frame: its status code and reason.</summary>
public sealed record CloseFrame(int Code, string Reason);

/// <summary>
/// Why a transport connection ended: the side that ended it, plus the protocol's
/// close code and reason when it has them, and a transport-specific detail.
/// Port of provide.uterm.transport_close.TransportClose.
/// </summary>
public sealed record TransportClose(
    CloseInitiator Initiator,
    int? Code = null,
    string Reason = "",
    string Detail = "")
{
    /// <summary>Wire spelling of <paramref name="initiator"/>: local, remote or unknown.</summary>
    public static string InitiatorName(CloseInitiator initiator) => initiator switch
    {
        CloseInitiator.Local => "local",
        CloseInitiator.Remote => "remote",
        _ => "unknown",
    };

    /// <summary>
    /// One line: the initiator, then code and reason, then any detail in parentheses.
    /// Byte-identical to Python's <c>TransportClose.summary()</c>.
    /// </summary>
    public string Summary()
    {
        var parts = new List<string> { $"{InitiatorName(Initiator)} close" };
        if (Code is { } code)
        {
            parts.Add(code.ToString(System.Globalization.CultureInfo.InvariantCulture));
        }

        if (Reason.Length > 0)
        {
            parts.Add(Reason);
        }

        var text = string.Join(" ", parts);
        return Detail.Length > 0 ? $"{text} ({Detail})" : text;
    }

    /// <summary>
    /// Describe a connection that ended with <paramref name="exception"/>; the detail is
    /// <c>"&lt;ExceptionType&gt;: &lt;message&gt;"</c>, as in Python's <c>close_from_exception</c>.
    /// </summary>
    public static TransportClose FromException(Exception exception, CloseInitiator initiator = CloseInitiator.Unknown) =>
        new(initiator, Detail: $"{exception.GetType().Name}: {exception.Message}");

    /// <summary>
    /// Attribute a WebSocket close to the side whose close frame came first: a received
    /// frame wins when nothing was sent or it was received before ours was sent; otherwise
    /// a sent frame makes it local; with neither frame the initiator is unknown.
    /// Mirrors <c>ws_transport._close_from_websockets</c>.
    /// </summary>
    public static TransportClose FromWebSocketFrames(
        CloseFrame? received, CloseFrame? sent, bool receivedThenSent, string detail = "")
    {
        if (received is not null && (sent is null || receivedThenSent))
        {
            return new TransportClose(CloseInitiator.Remote, received.Code, received.Reason, detail);
        }

        if (sent is not null)
        {
            return new TransportClose(CloseInitiator.Local, sent.Code, sent.Reason, detail);
        }

        return new TransportClose(CloseInitiator.Unknown, Detail: detail);
    }

    /// <summary>
    /// A socket error that ended a stream connection: a peer reset is the remote end
    /// closing; anything else (a broken pipe, an abort) cannot say who did. The detail
    /// names the underlying socket error when the stream wrapped one.
    /// </summary>
    public static TransportClose FromSocketError(Exception exception)
    {
        var socketError = exception as SocketException ?? exception.InnerException as SocketException;
        var cause = (Exception?)socketError ?? exception;
        var initiator = socketError?.SocketErrorCode == SocketError.ConnectionReset
            ? CloseInitiator.Remote
            : CloseInitiator.Unknown;
        return FromException(cause, initiator);
    }
}

/// <summary>
/// Thrown by a transport when its connection has ended, carrying who closed it.
/// Derives from <see cref="IOException"/> — the type of the shared
/// <see cref="TransportErrors.ConnectionClosed"/> it replaces — so existing
/// <c>catch (IOException)</c> sites keep catching it. The message is
/// <c>"&lt;prefix&gt; (&lt;summary&gt;)"</c>, so existing prefix matches keep working.
/// Port of provide.uterm.transport_close.TransportClosedError.
/// </summary>
public sealed class TransportClosedException : IOException
{
    public TransportClosedException(string message, TransportClose close, Exception? innerException = null)
        : base($"{message} ({close.Summary()})", innerException)
    {
        Close = close;
    }

    /// <summary>How the connection ended.</summary>
    public TransportClose Close { get; }
}
