//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.WebSockets;
using System.Text;

namespace Provide.Uterm.TunnelClient;

/// <summary>
/// Async WebSocket tunnel client. Port of packages/provide-uterm-go/tunnelclient Client.
/// </summary>
public sealed class Client : IAsyncDisposable
{
    private readonly string _wsUrl;
    private readonly string _token;
    private readonly object _gate = new();
    private ClientWebSocket? _conn;
    private readonly SemaphoreSlim _writeLock = new(1, 1);

    public Client(string wsUrl, string token = "")
    {
        _wsUrl = wsUrl;
        _token = token;
    }

    public bool Connected
    {
        get
        {
            lock (_gate)
            {
                return _conn is { State: WebSocketState.Open };
            }
        }
    }

    public async Task ConnectAsync(CancellationToken cancellationToken = default)
    {
        var ws = new ClientWebSocket();
        if (!string.IsNullOrEmpty(_token))
        {
            ws.Options.SetRequestHeader("Authorization", "Bearer " + _token);
        }

        await ws.ConnectAsync(new Uri(_wsUrl), cancellationToken).ConfigureAwait(false);
        lock (_gate)
        {
            _conn = ws;
        }
    }

    public async Task CloseAsync()
    {
        ClientWebSocket? conn;
        lock (_gate)
        {
            conn = _conn;
            _conn = null;
        }

        if (conn is null)
        {
            return;
        }

        try
        {
            if (conn.State == WebSocketState.Open)
            {
                await conn.CloseAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None)
                    .ConfigureAwait(false);
            }
        }
        catch
        {
        }
        finally
        {
            conn.Dispose();
        }
    }

    public async Task SendFrameAsync(byte[] frame, CancellationToken cancellationToken = default)
    {
        var conn = Current();
        await _writeLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await conn.SendAsync(frame, WebSocketMessageType.Binary, true, cancellationToken)
                .ConfigureAwait(false);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    public Task SendDataAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default) =>
        SendFrameAsync(TunnelCodec.EncodeFrame(TunnelProtocol.ChannelData, data.Span), cancellationToken);

    public Task SendControlAsync(IReadOnlyDictionary<string, object?> msg, CancellationToken cancellationToken = default) =>
        SendFrameAsync(TunnelCodec.EncodeControl(msg), cancellationToken);

    public async Task<TunnelFrame> RecvAsync(CancellationToken cancellationToken = default)
    {
        var conn = Current();
        var buffer = new ArraySegment<byte>(new byte[64 * 1024]);
        using var ms = new MemoryStream();
        WebSocketReceiveResult result;
        do
        {
            result = await conn.ReceiveAsync(buffer, cancellationToken).ConfigureAwait(false);
            if (result.MessageType == WebSocketMessageType.Close)
            {
                throw new InvalidOperationException("tunnelclient: connection closed");
            }

            ms.Write(buffer.Array!, buffer.Offset, result.Count);
        }
        while (!result.EndOfMessage);

        return TunnelCodec.DecodeFrame(ms.ToArray());
    }

    private ClientWebSocket Current()
    {
        lock (_gate)
        {
            if (_conn is null || _conn.State != WebSocketState.Open)
            {
                throw new InvalidOperationException("tunnelclient: not connected");
            }

            return _conn;
        }
    }

    public async ValueTask DisposeAsync() => await CloseAsync().ConfigureAwait(false);
}

/// <summary>
/// HTTP reverse-proxy intercept helper used by `uterm inspect`.
/// Port of packages/provide-uterm-go/tunnelclient/httpproxy.go (core surface).
/// </summary>
public sealed class HttpInspectProxy : IAsyncDisposable
{
    private readonly string _upstreamBase;
    private HttpListener? _listener;
    private CancellationTokenSource? _cts;
    private Task? _loop;
    public int Port { get; private set; }
    public List<(string Method, string Path, int Status)> Transactions { get; } = new();

    public HttpInspectProxy(string upstreamBase) => _upstreamBase = upstreamBase.TrimEnd('/');

    public Task StartAsync(string host = "127.0.0.1", int port = 0, CancellationToken ct = default)
    {
        var listener = new HttpListener();
        if (port == 0)
        {
            // Bind ephemeral via TcpListener then use that port for HttpListener.
            using var probe = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
            probe.Start();
            port = ((System.Net.IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
        }

        listener.Prefixes.Add($"http://{host}:{port}/");
        listener.Start();
        Port = port;
        _listener = listener;
        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _loop = AcceptLoopAsync(_cts.Token);
        return Task.CompletedTask;
    }

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && _listener is not null)
        {
            HttpListenerContext ctx;
            try
            {
                ctx = await _listener.GetContextAsync().WaitAsync(ct).ConfigureAwait(false);
            }
            catch
            {
                break;
            }

            _ = Task.Run(async () =>
            {
                try
                {
                    await HandleAsync(ctx, ct).ConfigureAwait(false);
                }
                catch
                {
                    try { ctx.Response.Abort(); } catch { /* ignore */ }
                }
            }, ct);
        }
    }

    private async Task HandleAsync(HttpListenerContext ctx, CancellationToken ct)
    {
        var req = ctx.Request;
        var path = req.Url?.PathAndQuery ?? "/";
        using var client = new HttpClient();
        using var upstream = new HttpRequestMessage(new HttpMethod(req.HttpMethod), _upstreamBase + path);
        if (req.HasEntityBody)
        {
            await using var body = req.InputStream;
            using var ms = new MemoryStream();
            await body.CopyToAsync(ms, ct).ConfigureAwait(false);
            upstream.Content = new ByteArrayContent(ms.ToArray());
            if (!string.IsNullOrEmpty(req.ContentType))
            {
                upstream.Content.Headers.TryAddWithoutValidation("Content-Type", req.ContentType);
            }
        }

        using var resp = await client.SendAsync(upstream, ct).ConfigureAwait(false);
        ctx.Response.StatusCode = (int)resp.StatusCode;
        var bytes = await resp.Content.ReadAsByteArrayAsync(ct).ConfigureAwait(false);
        lock (Transactions)
        {
            Transactions.Add((req.HttpMethod, path, (int)resp.StatusCode));
        }

        ctx.Response.ContentLength64 = bytes.Length;
        await ctx.Response.OutputStream.WriteAsync(bytes, ct).ConfigureAwait(false);
        ctx.Response.Close();
    }

    public async Task StopAsync()
    {
        if (_cts is not null)
        {
            await _cts.CancelAsync().ConfigureAwait(false);
        }

        _listener?.Stop();
        if (_loop is not null)
        {
            try { await _loop.ConfigureAwait(false); } catch { /* ignore */ }
        }

        _cts?.Dispose();
        _listener = null;
    }

    public ValueTask DisposeAsync() => new(StopAsync());
}

/// <summary>
/// Local PTY capture share session used by `uterm share`.
/// Captures a child process stdout/stderr and streams tunnel data frames.
/// </summary>
public sealed class PtyShareSession : IAsyncDisposable
{
    private System.Diagnostics.Process? _proc;
    public string Command { get; }
    public bool Running => _proc is { HasExited: false };

    public PtyShareSession(string command = "/bin/sh")
    {
        Command = command;
    }

    public async Task StartAsync(Client? tunnel = null, CancellationToken ct = default)
    {
        // Portable launch:
        // - "cmd /c exit 0" / multi-token → FileName + Arguments
        // - absolute/relative path → as-is
        // - bare name on Unix → /usr/bin/env (PATH lookup for true/sh/echo)
        // - bare name on Windows → PATH via ProcessStartInfo.FileName
        string file;
        string args;
        if (Command.Contains(' ', StringComparison.Ordinal))
        {
            var parts = Command.Split(' ', 2, StringSplitOptions.RemoveEmptyEntries);
            file = parts[0];
            args = parts.Length > 1 ? parts[1] : "";
        }
        else if (Path.IsPathRooted(Command)
                 || Command.Contains('/', StringComparison.Ordinal)
                 || Command.Contains('\\', StringComparison.Ordinal))
        {
            file = Command;
            args = "";
        }
        else if (OperatingSystem.IsWindows())
        {
            file = Command;
            args = "";
        }
        else
        {
            file = "/usr/bin/env";
            args = Command;
        }

        var psi = new System.Diagnostics.ProcessStartInfo
        {
            FileName = file,
            Arguments = args,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
        };
        var proc = System.Diagnostics.Process.Start(psi)
            ?? throw new InvalidOperationException("share: failed to start process");
        _proc = proc;
        _ = Task.Run(async () =>
        {
            var buf = new char[4096];
            while (!ct.IsCancellationRequested && !proc.HasExited)
            {
                var n = await proc.StandardOutput.ReadAsync(buf.AsMemory(0, buf.Length), ct)
                    .ConfigureAwait(false);
                if (n <= 0)
                {
                    break;
                }

                if (tunnel is { Connected: true })
                {
                    var bytes = Encoding.UTF8.GetBytes(buf.AsSpan(0, n).ToString());
                    await tunnel.SendDataAsync(bytes, ct).ConfigureAwait(false);
                }
            }
        }, ct);
    }

    public async Task WriteAsync(string data, CancellationToken ct = default)
    {
        if (_proc is null || _proc.HasExited)
        {
            throw new InvalidOperationException("share: process not running");
        }

        await _proc.StandardInput.WriteAsync(data.AsMemory(), ct).ConfigureAwait(false);
        await _proc.StandardInput.FlushAsync(ct).ConfigureAwait(false);
    }

    public async ValueTask DisposeAsync()
    {
        if (_proc is null)
        {
            return;
        }

        try
        {
            if (!_proc.HasExited)
            {
                _proc.Kill(entireProcessTree: true);
            }
        }
        catch
        {
        }

        _proc.Dispose();
        _proc = null;
        await Task.CompletedTask;
    }
}
