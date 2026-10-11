//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading.Channels;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// A one-request-per-connection loopback HTTP responder: answers each request
/// with the status <see cref="StatusFor"/> picks and reports each request
/// (start line + body) on <see cref="Requests"/>.
/// </summary>
internal sealed class TinyHttpServer : IAsyncDisposable
{
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource _cts = new();
    private readonly Task _loop;
    private int _count;

    public TinyHttpServer(Func<int, int>? statusFor = null)
    {
        StatusFor = statusFor ?? (_ => 200);
        _listener.Start();
        Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
        _loop = Task.Run(LoopAsync);
    }

    public int Port { get; }
    public string Url(string path = "/hook") => $"http://127.0.0.1:{Port}{path}";
    public Func<int, int> StatusFor { get; }
    public Channel<string> Requests { get; } = Channel.CreateUnbounded<string>();

    private async Task LoopAsync()
    {
        while (!_cts.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(_cts.Token);
            }
            catch (Exception)
            {
                return;
            }

            using (client)
            {
                try
                {
                    var stream = client.GetStream();
                    var head = new StringBuilder();
                    var buf = new byte[1];
                    while (!head.ToString().EndsWith("\r\n\r\n", StringComparison.Ordinal))
                    {
                        if (await stream.ReadAsync(buf, _cts.Token) == 0)
                        {
                            break;
                        }

                        head.Append((char)buf[0]);
                    }

                    var length = 0;
                    foreach (var line in head.ToString().Split("\r\n"))
                    {
                        if (line.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                        {
                            length = int.Parse(line["Content-Length:".Length..].Trim(), System.Globalization.CultureInfo.InvariantCulture);
                        }
                    }

                    var body = await FakeRfbServer.ReadAsync(stream, length, _cts.Token);
                    var status = StatusFor(Interlocked.Increment(ref _count));
                    var resp = Encoding.ASCII.GetBytes($"HTTP/1.1 {status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                    await stream.WriteAsync(resp, _cts.Token);
                    Requests.Writer.TryWrite(head.ToString().Split("\r\n")[0] + "\n" + Encoding.UTF8.GetString(body));
                }
                catch (Exception)
                {
                    // client went away
                }
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        await _cts.CancelAsync();
        _listener.Stop();
        await _loop;
        _cts.Dispose();
    }
}
