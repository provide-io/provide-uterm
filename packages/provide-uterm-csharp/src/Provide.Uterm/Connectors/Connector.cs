//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Session;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Connectors;

/// <summary>
/// Session connector abstraction driven by the hosted session runtime.
/// Port of packages/provide-uterm-go/connectors.
/// </summary>
public interface IConnector
{
    Task StartAsync(CancellationToken cancellationToken = default);
    Task StopAsync(CancellationToken cancellationToken = default);
    bool IsConnected();
    Task HandleInputAsync(string data, CancellationToken cancellationToken = default);
    void HandleControl(string action);
    void SetMode(string mode);
    void Clear();
    Snapshot Snapshot();
    string Analysis();
    IReadOnlyList<Dictionary<string, object?>> Events();
    TransportSession? Session();
}

public abstract class BaseConnector : IConnector
{
    protected TransportSession? LiveSession;
    protected string Mode = "open";
    protected readonly List<Dictionary<string, object?>> EventBuffer = new();
    protected readonly object Lock = new();

    public abstract Task StartAsync(CancellationToken cancellationToken = default);

    public virtual async Task StopAsync(CancellationToken cancellationToken = default)
    {
        if (LiveSession is not null)
        {
            await LiveSession.CloseAsync(cancellationToken);
            LiveSession = null;
        }
    }

    public virtual bool IsConnected() => LiveSession?.IsConnected() == true;

    public virtual async Task HandleInputAsync(string data, CancellationToken cancellationToken = default)
    {
        if (LiveSession is null)
        {
            throw new InvalidOperationException("not started");
        }

        await LiveSession.SendAsync(data, cancellationToken);
    }

    public virtual void HandleControl(string action)
    {
        // pause/resume/step are no-ops at the base layer
        _ = action;
    }

    public virtual void SetMode(string mode)
    {
        if (mode is not ("open" or "hijack"))
        {
            throw new ArgumentException($"invalid mode: {mode}");
        }

        Mode = mode;
    }

    public virtual void Clear() => LiveSession?.Emulator().Reset();

    public virtual Snapshot Snapshot() => LiveSession?.Snapshot() ?? new Snapshot();

    public virtual string Analysis() => "mode=" + Mode;

    public virtual IReadOnlyList<Dictionary<string, object?>> Events()
    {
        lock (Lock)
        {
            var copy = EventBuffer.ToList();
            EventBuffer.Clear();
            return copy;
        }
    }

    public TransportSession? Session() => LiveSession;

    protected void AttachWatch(TransportSession session)
    {
        session.AddWatch((_, raw) =>
        {
            lock (Lock)
            {
                EventBuffer.Add(new Dictionary<string, object?>
                {
                    ["type"] = "output",
                    ["data"] = Convert.ToBase64String(raw),
                    ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
                });
            }
        });
    }
}

public sealed class TelnetConnector : BaseConnector
{
    private readonly string _host;
    private readonly int _port;
    private readonly TelnetOptions _opts;

    public TelnetConnector(string host, int port, TelnetOptions? opts = null)
    {
        _host = host;
        _port = port;
        _opts = opts ?? new TelnetOptions();
    }

    public override async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var session = Sessions.NewTelnetSession(_host, _port, _opts);
        AttachWatch(session);
        await session.ConnectAsync(cancellationToken);
        LiveSession = session;
    }
}

public sealed class SshConnector : BaseConnector
{
    private readonly string _host;
    private readonly int _port;
    private readonly ConnectOptions _opts;

    public SshConnector(string host, int port, ConnectOptions? opts = null)
    {
        _host = host;
        _port = port;
        _opts = opts ?? new ConnectOptions();
    }

    public override async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var transport = new SshTransport();
        var session = new TransportSession(
            transport,
            ct => transport.ConnectAsync(_host, _port, _opts, ct),
            new TransportSessionOptions
            {
                Cols = _opts.Cols <= 0 ? 80 : _opts.Cols,
                Rows = _opts.Rows <= 0 ? 25 : _opts.Rows,
            });
        AttachWatch(session);
        await session.ConnectAsync(cancellationToken);
        LiveSession = session;
    }
}

public sealed class WebSocketConnector : BaseConnector
{
    private readonly string _url;
    private readonly WsOptions _opts;

    public WebSocketConnector(string url, WsOptions? opts = null)
    {
        _url = url;
        _opts = opts ?? new WsOptions();
    }

    public override async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var session = Sessions.NewWSSession(_url, _opts);
        AttachWatch(session);
        await session.ConnectAsync(cancellationToken);
        LiveSession = session;
    }
}

public sealed class ShellConnector : BaseConnector
{
    private readonly string _shell;

    public ShellConnector(string? shell = null)
    {
        _shell = shell ?? Environment.GetEnvironmentVariable("SHELL") ?? "/bin/sh";
    }

    public override async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var transport = new Pty.PtyTransport(_shell);
        var session = new TransportSession(
            transport,
            ct => transport.ConnectAsync("local", 0, new ConnectOptions(), ct),
            new TransportSessionOptions());
        AttachWatch(session);
        await session.ConnectAsync(cancellationToken);
        LiveSession = session;
    }
}

public sealed class ConnectorRegistry
{
    private readonly Dictionary<string, Func<IReadOnlyDictionary<string, object?>, IConnector>> _factories = new(StringComparer.OrdinalIgnoreCase);

    public ConnectorRegistry()
    {
        Register("telnet", cfg => new TelnetConnector(
            GetString(cfg, "host", "127.0.0.1"),
            GetInt(cfg, "port", Defaults.TerminalDefaults.TelnetPort)));
        Register("ssh", cfg => new SshConnector(
            GetString(cfg, "host", "127.0.0.1"),
            GetInt(cfg, "port", Defaults.TerminalDefaults.SshPort)));
        Register("websocket", cfg => new WebSocketConnector(GetString(cfg, "url", "ws://127.0.0.1/")));
        Register("ws", cfg => new WebSocketConnector(GetString(cfg, "url", "ws://127.0.0.1/")));
        Register("shell", cfg => new ShellConnector(GetString(cfg, "shell", "")));
        Register("pty", cfg => new ShellConnector(GetString(cfg, "shell", "")));
    }

    public void Register(string type, Func<IReadOnlyDictionary<string, object?>, IConnector> factory) =>
        _factories[type] = factory;

    public IConnector Create(string type, IReadOnlyDictionary<string, object?> config)
    {
        if (!_factories.TryGetValue(type, out var factory))
        {
            throw new ArgumentException($"unknown connector type: {type}");
        }

        return factory(config);
    }

    public IReadOnlyCollection<string> Types() => _factories.Keys.ToList();

    private static string GetString(IReadOnlyDictionary<string, object?> cfg, string key, string fallback) =>
        cfg.TryGetValue(key, out var v) && v is string s && s.Length > 0 ? s : fallback;

    private static int GetInt(IReadOnlyDictionary<string, object?> cfg, string key, int fallback) =>
        cfg.TryGetValue(key, out var v) ? Convert.ToInt32(v) : fallback;
}
