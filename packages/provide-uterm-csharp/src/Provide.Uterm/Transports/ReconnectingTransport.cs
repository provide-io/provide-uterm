//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Sockets;
using System.Net.WebSockets;
using Provide.Uterm.Defaults;

namespace Provide.Uterm.Transports;

/// <summary>
/// Retry budget and backoff for reconnect attempts. Port of Python's
/// <c>ReconnectPolicy</c> dataclass and Go's <c>transports.ReconnectPolicy</c>.
/// </summary>
public sealed record ReconnectPolicy(int MaxRetries, TimeSpan BaseBackoff, TimeSpan MaxBackoff)
{
    /// <summary>The policy seeded from <see cref="TerminalDefaults"/> (RECONNECT_* constants).</summary>
    public static ReconnectPolicy Default { get; } = new(
        TerminalDefaults.ReconnectMaxRetries,
        TimeSpan.FromSeconds(TerminalDefaults.ReconnectBaseBackoffS),
        TimeSpan.FromSeconds(TerminalDefaults.ReconnectMaxBackoffS));

    /// <summary>
    /// Bounded exponential backoff for a one-based attempt number: the base doubled per
    /// attempt after the first, capped at <see cref="MaxBackoff"/>. Attempt zero (or less)
    /// is clamped to the first attempt. Direct port of Python's <c>_policy_delay</c>.
    /// </summary>
    public TimeSpan Delay(int attempt)
    {
        if (BaseBackoff <= TimeSpan.Zero)
        {
            // Python multiplies an int power, so a zero base stays zero however far the
            // exponent runs; in doubles 0 * infinity would be NaN.
            return TimeSpan.Zero;
        }

        var power = Math.Max(attempt - 1, 0);
        var seconds = Math.Min(BaseBackoff.TotalSeconds * Math.Pow(2, power), MaxBackoff.TotalSeconds);
        return TimeSpan.FromSeconds(seconds);
    }
}

/// <summary>
/// The reconnect budget ran out. An <see cref="IOException"/>, as Python's is a
/// <c>ConnectionError</c>; the failure that exhausted it is the inner exception.
/// </summary>
public sealed class RetriesExhaustedException : IOException
{
    /// <summary>The first connect never succeeded (Python <c>connect_with_retries</c>).</summary>
    public const string ConnectMessage = "connect retries exhausted";

    /// <summary>A dropped connection could not be brought back (Python <c>ReconnectingSession</c>).</summary>
    public const string ReconnectMessage = "reconnect retries exhausted";

    public RetriesExhaustedException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}

/// <summary>Options for <see cref="ReconnectingTransport"/>. Every member is optional.</summary>
public sealed class ReconnectingOptions
{
    /// <summary>Retry budget and backoff; null uses <see cref="ReconnectPolicy.Default"/>.</summary>
    public ReconnectPolicy? Policy { get; init; }

    /// <summary>Backoff sleeper; null uses <see cref="Task.Delay(TimeSpan, CancellationToken)"/>. Injectable so tests never sleep.</summary>
    public Func<TimeSpan, CancellationToken, Task>? Sleep { get; init; }

    /// <summary>Which failures reconnect; null uses <see cref="ReconnectingTransport.IsRetryable"/>.</summary>
    public Func<Exception, bool>? IsRetryable { get; init; }

    /// <summary>Called with the freshly connected inner transport after each successful reconnect.</summary>
    public Func<IConnectionTransport, CancellationToken, Task>? OnReconnect { get; init; }
}

/// <summary>
/// Wraps a connection transport with automatic reconnection on transport drops, and keeps
/// the close that triggered the last reconnect. Port of Go's
/// <c>transports.ReconnectingTransport</c> (itself the port of Python's
/// <c>ReconnectingSession</c>); it implements <see cref="IConnectionTransport"/>, so it is a
/// drop-in replacement for the transport it wraps.
/// </summary>
public sealed class ReconnectingTransport : IConnectionTransport
{
    private readonly Func<IConnectionTransport> _factory;
    private readonly ReconnectPolicy _policy;
    private readonly Func<TimeSpan, CancellationToken, Task> _sleep;
    private readonly Func<Exception, bool> _isRetryable;
    private readonly Func<IConnectionTransport, CancellationToken, Task>? _onReconnect;
    private readonly object _lock = new();

    private IConnectionTransport? _inner;
    private string _host = "";
    private int _port;
    private ConnectOptions? _options;
    private TransportClose? _lastClose;

    /// <param name="factory">Creates a fresh, unconnected inner transport for every attempt.</param>
    /// <param name="options">Policy, sleeper, classifier and reconnect hook; all optional.</param>
    public ReconnectingTransport(Func<IConnectionTransport> factory, ReconnectingOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(factory);
        options ??= new ReconnectingOptions();
        _factory = factory;
        _policy = options.Policy ?? ReconnectPolicy.Default;
        _sleep = options.Sleep ?? Task.Delay;
        _isRetryable = options.IsRetryable ?? IsRetryable;
        _onReconnect = options.OnReconnect;
    }

    /// <summary>
    /// How the connection that triggered the most recent reconnect ended — or the final
    /// attempt, when the retries ran out — or null if no retried failure reported a close.
    /// Port of Python's <c>ReconnectingSession.last_close</c> and Go's <c>LastClose()</c>.
    /// </summary>
    public TransportClose? LastClose
    {
        get
        {
            lock (_lock)
            {
                return _lastClose;
            }
        }
    }

    /// <summary>The currently active inner transport, or null before connecting / after disconnecting.</summary>
    public IConnectionTransport? Inner
    {
        get
        {
            lock (_lock)
            {
                return _inner;
            }
        }
    }

    /// <summary>
    /// The default retryable set, mirroring Python's (<c>ConnectionError</c>, <c>OSError</c>,
    /// websockets' <c>ConnectionClosed</c>): transport-layer failures reconnect — every
    /// <see cref="IOException"/> (which includes <see cref="TransportClosedException"/>),
    /// socket and WebSocket errors, timeouts, access denial (a Python <c>PermissionError</c>
    /// is an <c>OSError</c>) and the shared "not connected" a transport throws once it has
    /// dropped. Logic errors and cancellation do not.
    /// </summary>
    public static bool IsRetryable(Exception exception) =>
        exception is IOException
            or SocketException
            or WebSocketException
            or TimeoutException
            or UnauthorizedAccessException
        || ReferenceEquals(exception, TransportErrors.NotConnected);

    /// <summary>Connects (with the retry budget) and remembers the target for later reconnects.</summary>
    public async Task ConnectAsync(
        string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
    {
        lock (_lock)
        {
            _host = host;
            _port = port;
            _options = options;
        }

        var inner = await ConnectWithRetriesAsync(RetriesExhaustedException.ConnectMessage, cancellationToken)
            .ConfigureAwait(false);
        lock (_lock)
        {
            _inner = inner;
        }
    }

    /// <summary>Disconnects the active inner transport; a no-op when there is none.</summary>
    public Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        IConnectionTransport? inner;
        lock (_lock)
        {
            inner = _inner;
            _inner = null;
        }

        return inner is null ? Task.CompletedTask : inner.DisconnectAsync(cancellationToken);
    }

    /// <summary>Sends <paramref name="data"/>, reconnecting on retryable failures.</summary>
    public Task SendAsync(byte[] data, CancellationToken cancellationToken = default) =>
        RunWithReconnectAsync(
            async inner =>
            {
                await inner.SendAsync(data, cancellationToken).ConfigureAwait(false);
                return true;
            },
            cancellationToken);

    /// <summary>
    /// Receives bytes, reconnecting on retryable failures. A receive timeout returns an empty
    /// array from the inner transport and is not treated as a drop.
    /// </summary>
    public Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default) =>
        RunWithReconnectAsync(inner => inner.ReceiveAsync(maxBytes, timeout, cancellationToken), cancellationToken);

    /// <summary>Whether the active inner transport is connected.</summary>
    public bool IsConnected() => Inner?.IsConnected() == true;

    /// <summary>Dials via the factory with the backoff budget. Port of Python's <c>connect_with_retries</c>.</summary>
    private async Task<IConnectionTransport> ConnectWithRetriesAsync(string exhaustedMessage, CancellationToken ct)
    {
        string host;
        int port;
        ConnectOptions? options;
        lock (_lock)
        {
            (host, port, options) = (_host, _port, _options);
        }

        var retries = 0;
        while (true)
        {
            var inner = _factory();
            try
            {
                await inner.ConnectAsync(host, port, options, ct).ConfigureAwait(false);
                return inner;
            }
            catch (Exception ex) when (!IsCancellation(ex, ct))
            {
                if (retries >= _policy.MaxRetries)
                {
                    throw new RetriesExhaustedException(exhaustedMessage, ex);
                }
            }

            retries++;
            await _sleep(_policy.Delay(retries), ct).ConfigureAwait(false);
        }
    }

    /// <summary>Closes the dropped transport, backs off, and reconnects. Port of Python's <c>_reconnect</c>.</summary>
    private async Task ReconnectAsync(IConnectionTransport old, int attempt, CancellationToken ct)
    {
        try
        {
            await old.DisconnectAsync(ct).ConfigureAwait(false);
        }
        catch (Exception ex) when (!IsCancellation(ex, ct))
        {
            // A socket that is already gone may fail to close; the reconnect goes on.
        }

        var delay = _policy.Delay(attempt);
        if (delay > TimeSpan.Zero)
        {
            await _sleep(delay, ct).ConfigureAwait(false);
        }

        var inner = await ConnectWithRetriesAsync(RetriesExhaustedException.ReconnectMessage, ct)
            .ConfigureAwait(false);
        lock (_lock)
        {
            _inner = inner;
        }

        if (_onReconnect is not null)
        {
            await _onReconnect(inner, ct).ConfigureAwait(false);
        }
    }

    /// <summary>Runs <paramref name="op"/>, reconnecting on retryable failures. Port of Python's <c>_run_with_reconnect</c>.</summary>
    private async Task<T> RunWithReconnectAsync<T>(Func<IConnectionTransport, Task<T>> op, CancellationToken ct)
    {
        var retries = 0;
        while (true)
        {
            var inner = Inner ?? throw TransportErrors.NotConnected;
            try
            {
                return await op(inner).ConfigureAwait(false);
            }
            catch (Exception ex) when (_isRetryable(ex))
            {
                if (ex is TransportClosedException closed)
                {
                    lock (_lock)
                    {
                        _lastClose = closed.Close;
                    }
                }

                if (retries >= _policy.MaxRetries)
                {
                    await DisconnectQuietlyAsync(inner).ConfigureAwait(false);
                    throw new RetriesExhaustedException(RetriesExhaustedException.ReconnectMessage, ex);
                }
            }

            retries++;
            await ReconnectAsync(inner, retries, ct).ConfigureAwait(false);
        }
    }

    private static async Task DisconnectQuietlyAsync(IConnectionTransport inner)
    {
        try
        {
            await inner.DisconnectAsync(CancellationToken.None).ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Giving up anyway: the original failure is what the caller needs.
        }
    }

    private static bool IsCancellation(Exception ex, CancellationToken ct) =>
        ex is OperationCanceledException && ct.IsCancellationRequested;
}
