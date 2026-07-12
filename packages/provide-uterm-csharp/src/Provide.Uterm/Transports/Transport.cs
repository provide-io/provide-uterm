//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Transports;

public static class TransportDefaults
{
    public const int DefaultCols = 80;
    public const int DefaultRows = 25;
    public const string DefaultTerm = "ANSI";
    public static readonly TimeSpan DefaultConnectTimeout = TimeSpan.FromSeconds(30);
}

public sealed class WsOptions
{
    public string Url { get; set; } = "";
    public string Origin { get; set; } = "";
    public Dictionary<string, string> Headers { get; set; } = new();
    public bool SendBinary { get; set; }
}

public sealed class SshKeyAuth
{
    public byte[] PrivateKeyPem { get; set; } = Array.Empty<byte>();
    public byte[] Passphrase { get; set; } = Array.Empty<byte>();
}

public sealed class SshOptions
{
    public string User { get; set; } = "";
    public string Password { get; set; } = "";
    public SshKeyAuth Key { get; set; } = new();
    public List<string> KnownHostsFiles { get; set; } = new();
    public bool InsecureSkipHostKeyVerify { get; set; }
}

public sealed class ConnectOptions
{
    public int Cols { get; set; }
    public int Rows { get; set; }
    public string Term { get; set; } = "";
    public TimeSpan Timeout { get; set; }
    public WsOptions Ws { get; set; } = new();
    public SshOptions Ssh { get; set; } = new();

    public ConnectOptions WithDefaults()
    {
        if (Cols == 0)
        {
            Cols = TransportDefaults.DefaultCols;
        }

        if (Rows == 0)
        {
            Rows = TransportDefaults.DefaultRows;
        }

        if (string.IsNullOrEmpty(Term))
        {
            Term = TransportDefaults.DefaultTerm;
        }

        if (Timeout <= TimeSpan.Zero)
        {
            Timeout = TransportDefaults.DefaultConnectTimeout;
        }

        return this;
    }
}

/// <summary>
/// Connection transport interface implemented by telnet, WebSocket, and SSH.
/// Port of packages/provide-uterm-go/transports.
/// </summary>
public interface IConnectionTransport
{
    Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default);
    Task DisconnectAsync(CancellationToken cancellationToken = default);
    Task SendAsync(byte[] data, CancellationToken cancellationToken = default);
    Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default);
    bool IsConnected();
}

public static class TransportErrors
{
    public static readonly Exception NotConnected = new InvalidOperationException("not connected");
    public static readonly Exception ConnectionClosed = new IOException("connection closed by remote");
}
