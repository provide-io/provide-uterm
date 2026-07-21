//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Embed;

/// <summary>
/// Upstream byte pipe for an embedded session. Implementations may wrap TCP/telnet/SSH
/// or a deterministic test duplex. Payload is always raw application bytes (IAC stripped
/// by the transport/policy layer when applicable).
/// </summary>
public interface IUpstreamPipe
{
    bool IsConnected { get; }
    Task ConnectAsync(CancellationToken cancellationToken = default);
    Task DisconnectAsync(CancellationToken cancellationToken = default);
    Task SendAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default);
    /// <summary>Read next application-byte chunk. Empty array means clean EOF.</summary>
    Task<byte[]> ReceiveAsync(CancellationToken cancellationToken = default);
}

/// <summary>
/// Host-supplied Telnet policy. Uterm owns RFC IAC mechanics; the host supplies
/// terminal type, NAWS, and option answers (including game-specific responses).
/// </summary>
public interface ITelnetPolicy
{
    string TerminalType { get; }
    (int Cols, int Rows) WindowSize { get; }

    /// <summary>Answer a DO/WILL/WONT/DONT option request. Return bytes to write on the wire (may be empty).</summary>
    ReadOnlyMemory<byte> OnOption(byte command, byte option);

    /// <summary>Answer a subnegotiation body (between SB and SE, option already known).</summary>
    ReadOnlyMemory<byte> OnSubnegotiation(byte option, ReadOnlySpan<byte> body);
}

/// <summary>Default ANSI 80×25 policy that accepts BINARY/SGA/NAWS/TTYPE.</summary>
public sealed class DefaultTelnetPolicy : ITelnetPolicy
{
    public string TerminalType { get; init; } = "ANSI";
    public (int Cols, int Rows) WindowSize { get; init; } = (80, 25);

    public ReadOnlyMemory<byte> OnOption(byte command, byte option)
    {
        // Minimal DO/WILL accept for common options; hosts override for custom behavior.
        const byte will = 251, wont = 252, doCmd = 253, dont = 254;
        const byte iac = 255;
        return command switch
        {
            doCmd => new byte[] { iac, will, option },
            will => new byte[] { iac, doCmd, option },
            wont => new byte[] { iac, dont, option },
            dont => new byte[] { iac, wont, option },
            _ => ReadOnlyMemory<byte>.Empty,
        };
    }

    public ReadOnlyMemory<byte> OnSubnegotiation(byte option, ReadOnlySpan<byte> body)
    {
        const byte iac = 255, sb = 250, se = 240, will = 251;
        // TTYPE SEND (24) → IS <term>
        if (option == 24 && body.Length > 0 && body[0] == 1)
        {
            var term = System.Text.Encoding.ASCII.GetBytes(TerminalType);
            var buf = new byte[4 + term.Length + 2];
            buf[0] = iac;
            buf[1] = sb;
            buf[2] = 24;
            buf[3] = 0; // IS
            term.CopyTo(buf, 4);
            buf[^2] = iac;
            buf[^1] = se;
            return buf;
        }

        // NAWS — host may push size; empty ack is fine
        if (option == 31)
        {
            var (cols, rows) = WindowSize;
            return new byte[]
            {
                iac, sb, 31,
                (byte)(cols >> 8), (byte)(cols & 0xff),
                (byte)(rows >> 8), (byte)(rows & 0xff),
                iac, se,
            };
        }

        _ = will;
        return ReadOnlyMemory<byte>.Empty;
    }
}

/// <summary>Byte interceptor for both directions. Must be re-entrancy-safe via session queue.</summary>
public interface IByteInterceptor
{
    ValueTask<InterceptResult> OnUpstreamAsync(InterceptContext context, CancellationToken cancellationToken = default);
    ValueTask<InterceptResult> OnClientAsync(InterceptContext context, CancellationToken cancellationToken = default);
}

/// <summary>Pass-through interceptor (default).</summary>
public sealed class PassThroughInterceptor : IByteInterceptor
{
    public static PassThroughInterceptor Instance { get; } = new();

    public ValueTask<InterceptResult> OnUpstreamAsync(InterceptContext context, CancellationToken cancellationToken = default) =>
        ValueTask.FromResult(InterceptResult.Pass());

    public ValueTask<InterceptResult> OnClientAsync(InterceptContext context, CancellationToken cancellationToken = default) =>
        ValueTask.FromResult(InterceptResult.Pass());
}

public sealed class InterceptContext
{
    public required IUtermSession Session { get; init; }
    public required ByteDirection Direction { get; init; }
    public required byte[] Data { get; init; }
    public string? ClientId { get; init; }
}

public interface IClientHandle : IAsyncDisposable
{
    string ClientId { get; }
    ClientMetadata Metadata { get; }
    bool IsAttached { get; }

    /// <summary>Dequeue next fan-out chunk for this client (application bytes).</summary>
    Task<byte[]> ReceiveAsync(CancellationToken cancellationToken = default);
}

/// <summary>
/// First-class embedded multi-client proxy session.
/// TWX and other hosts attach interceptors/clients without CLI or loopback HTTP.
/// </summary>
public interface IUtermSession : IAsyncDisposable
{
    string SessionId { get; }
    SessionLifecycle Lifecycle { get; }
    IReadOnlyDictionary<string, object?> Services { get; }

    event EventHandler<ByteChunkEventArgs>? ApplicationDataReceived;
    event EventHandler<ByteChunkEventArgs>? ClientDataReceived;
    event EventHandler<WireEventArgs>? WireEvents;
    event EventHandler<SessionLifecycleEventArgs>? LifecycleChanged;

    Task ConnectUpstreamAsync(IUpstreamPipe upstream, CancellationToken cancellationToken = default);
    Task ReplaceUpstreamAsync(IUpstreamPipe upstream, CancellationToken cancellationToken = default);
    Task SendToUpstreamAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default);
    Task SendToClientsAsync(ReadOnlyMemory<byte> data, ClientFilter? filter = null, CancellationToken cancellationToken = default);
    Task<IClientHandle> AttachClientAsync(ClientAttachOptions options, CancellationToken cancellationToken = default);
    /// <summary>Idempotently detach a client; its handle becomes unattached (EOF on receive).</summary>
    Task DetachClientAsync(string clientId, CancellationToken cancellationToken = default);
    Task FlushDeferredAsync(CancellationToken cancellationToken = default);
    Task RaiseWireEventAsync(WireEventKind kind, ReadOnlyMemory<byte> data, string detail = "", CancellationToken cancellationToken = default);
    Task MarkNegotiatedAsync(CancellationToken cancellationToken = default);
}

public interface IEmbedHub
{
    Task<IUtermSession> CreateSessionAsync(EmbedSessionOptions? options = null, CancellationToken cancellationToken = default);
    IUtermSession? GetSession(string sessionId);
    IReadOnlyCollection<string> SessionIds { get; }
}
