//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Client;
using Provide.Uterm.Defaults;
using Provide.Uterm.Gateway;
using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;
using TunnelCli = Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Cli;

/// <summary>
/// `uterm` command tree. Subcommands mirror the Go/Python CLI order:
/// proxy, listen, share, tunnel, inspect, watch, audit, server.
/// Every subcommand runs real logic (not print-and-return stubs).
/// </summary>
public static class Root
{
    public static string Version { get; set; } = "0.0.0-dev";

    public static readonly string[] Subcommands =
    [
        "proxy", "listen", "share", "tunnel", "inspect", "watch", "audit", "server",
    ];

    public static int Execute(string[] args, TextWriter? outWriter = null, TextWriter? errWriter = null)
    {
        outWriter ??= Console.Out;
        errWriter ??= Console.Error;

        if (args.Length == 0 || args[0] is "-h" or "--help" or "help")
        {
            WriteHelp(outWriter);
            return 0;
        }

        if (args[0] is "-V" or "--version" or "version")
        {
            outWriter.WriteLine(Version);
            return 0;
        }

        var cmd = args[0];
        var rest = args.Skip(1).ToArray();
        try
        {
            return cmd switch
            {
                "proxy" => RunProxy(rest, outWriter, errWriter),
                "listen" => RunListen(rest, outWriter, errWriter),
                "share" => RunShare(rest, outWriter, errWriter),
                "tunnel" => RunTunnel(rest, outWriter, errWriter),
                "inspect" => RunInspect(rest, outWriter, errWriter),
                "watch" => RunWatch(rest, outWriter, errWriter).GetAwaiter().GetResult(),
                "audit" => RunAudit(rest, outWriter, errWriter),
                "server" => RunServer(rest, outWriter, errWriter).GetAwaiter().GetResult(),
                _ => Unknown(cmd, errWriter),
            };
        }
        catch (Exception ex)
        {
            errWriter.WriteLine("error: " + ex.Message);
            return 1;
        }
    }

    public static Task<int> ExecuteAsync(string[] args, TextWriter? outWriter = null, TextWriter? errWriter = null) =>
        Task.FromResult(Execute(args, outWriter, errWriter));

    private static int Unknown(string cmd, TextWriter err)
    {
        err.WriteLine($"error: unknown command {cmd}");
        return 1;
    }

    private static void WriteHelp(TextWriter w)
    {
        w.WriteLine("uterm — Bidirectional WebSocket terminal proxy for BBS/telnet servers.");
        w.WriteLine();
        w.WriteLine("Usage:");
        w.WriteLine("  uterm [command] [options]");
        w.WriteLine();
        w.WriteLine("Available Commands:");
        w.WriteLine("  proxy     browser WS → remote telnet/SSH/websocket");
        w.WriteLine("  listen    telnet client → remote WS terminal (gateway)");
        w.WriteLine("  share     Share a local process over a tunnel");
        w.WriteLine("  tunnel    Tunnel client (connect)");
        w.WriteLine("  inspect   HTTP inspect proxy");
        w.WriteLine("  watch     List sessions from a running server");
        w.WriteLine("  audit     Verify a tamper-evident WORM audit log");
        w.WriteLine("  server    Run the provide-uterm hosted server");
        w.WriteLine();
        w.WriteLine($"Version: {Version}");
    }

    internal static Dictionary<string, string> ParseFlags(string[] args)
    {
        var flags = new Dictionary<string, string>(StringComparer.Ordinal);
        var positionals = new List<string>();
        for (var i = 0; i < args.Length; i++)
        {
            var a = args[i];
            if (!a.StartsWith("--", StringComparison.Ordinal) && !a.StartsWith('-'))
            {
                positionals.Add(a);
                continue;
            }

            if (a.StartsWith("--", StringComparison.Ordinal))
            {
                var key = a[2..];
                var eq = key.IndexOf('=');
                if (eq >= 0)
                {
                    flags[key[..eq]] = key[(eq + 1)..];
                }
                else if (i + 1 < args.Length && !args[i + 1].StartsWith('-'))
                {
                    flags[key] = args[++i];
                }
                else
                {
                    flags[key] = "true";
                }
            }
            else if (a is "-p" or "-h" or "-V")
            {
                var key = a switch { "-p" => "port", "-h" => "help", "-V" => "version", _ => a };
                if (key == "help" || key == "version")
                {
                    flags[key] = "true";
                }
                else if (i + 1 < args.Length)
                {
                    flags[key] = args[++i];
                }
            }
        }

        for (var i = 0; i < positionals.Count; i++)
        {
            flags[$"_{i}"] = positionals[i];
        }

        return flags;
    }

    private static int RunProxy(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm proxy HOST PORT — browser WS → remote telnet/SSH/websocket");
            o.WriteLine("  --port / -p   local listen port (default 8765)");
            o.WriteLine("  --bind        bind address (default 0.0.0.0)");
            o.WriteLine("  --path        WebSocket path (default /ws/terminal)");
            o.WriteLine("  --transport   telnet|ssh|websocket (default telnet)");
            o.WriteLine("  --url         upstream ws(s):// URL (required for --transport websocket)");
            o.WriteLine("  --ssh-user    SSH username (required for --transport ssh)");
            o.WriteLine("  --ssh-password  SSH password");
            o.WriteLine("  --known-hosts path to OpenSSH known_hosts (repeatable / required unless insecure)");
            o.WriteLine("  --insecure-ssh  skip host-key verification (explicit opt-in only)");
            o.WriteLine("  --once        start, hit /health, stop");
            return 0;
        }

        var f = ParseFlags(args);
        var transport = f.GetValueOrDefault("transport", "telnet");
        if (transport is not ("telnet" or "ssh" or "websocket" or "ws" or "wss"))
        {
            e.WriteLine($"error: --transport must be telnet, ssh, or websocket, got {transport}");
            return 1;
        }

        string host;
        int bbsPort;
        string? upstreamWs = f.GetValueOrDefault("url");
        if (transport is "websocket" or "ws" or "wss")
        {
            // Allow: proxy --transport websocket --url wss://...  (HOST PORT optional placeholders)
            if (string.IsNullOrEmpty(upstreamWs))
            {
                // Or: proxy wss://host/path  as single positional
                if (f.TryGetValue("_0", out var posUrl) &&
                    (posUrl.StartsWith("ws://", StringComparison.OrdinalIgnoreCase) ||
                     posUrl.StartsWith("wss://", StringComparison.OrdinalIgnoreCase)))
                {
                    upstreamWs = posUrl;
                }
                else
                {
                    e.WriteLine("error: websocket proxy requires --url wss://... (or pass URL as HOST)");
                    return 1;
                }
            }

            var uri = new Uri(upstreamWs);
            host = uri.Host;
            bbsPort = uri.IsDefaultPort ? (uri.Scheme == "wss" ? 443 : 80) : uri.Port;
            transport = "websocket";
        }
        else
        {
            if (!f.TryGetValue("_0", out host!) || !f.TryGetValue("_1", out var portStr))
            {
                e.WriteLine("error: proxy requires HOST PORT");
                return 1;
            }

            if (!int.TryParse(portStr, out bbsPort))
            {
                e.WriteLine($"error: PORT must be an integer: {portStr}");
                return 1;
            }
        }

        var knownHosts = new List<string>();
        if (f.TryGetValue("known-hosts", out var kh) && !string.IsNullOrEmpty(kh))
        {
            knownHosts.Add(kh);
        }

        // Support repeated --known-hosts via space-separated list
        if (f.TryGetValue("known-hosts-list", out var khList) && !string.IsNullOrEmpty(khList))
        {
            knownHosts.AddRange(khList.Split(':', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        }

        if (transport == "ssh")
        {
            var sshUser = f.GetValueOrDefault("ssh-user", "");
            if (string.IsNullOrEmpty(sshUser))
            {
                e.WriteLine("error: --transport ssh requires --ssh-user");
                return 1;
            }

            var insecure = f.ContainsKey("insecure-ssh");
            if (!insecure && knownHosts.Count == 0)
            {
                e.WriteLine("error: --transport ssh requires --known-hosts PATH or --insecure-ssh");
                return 1;
            }
        }

        var opts = new ProxyCommand.Options
        {
            Host = host,
            BbsPort = bbsPort,
            Bind = f.GetValueOrDefault("bind", TerminalDefaults.BindAll),
            Port = int.Parse(f.GetValueOrDefault("port", TerminalDefaults.ProxyPort.ToString())),
            Path = f.GetValueOrDefault("path", TerminalDefaults.ProxyWsPath),
            Transport = transport,
            UpstreamWsUrl = upstreamWs,
            SshUser = f.GetValueOrDefault("ssh-user", ""),
            SshPassword = f.GetValueOrDefault("ssh-password", ""),
            KnownHostsFiles = knownHosts,
            InsecureSkipHostKeyVerify = f.ContainsKey("insecure-ssh"),
        };

        var target = transport == "websocket"
            ? opts.UpstreamWsUrl
            : $"{opts.Transport}://{opts.Host}:{opts.BbsPort}";
        o.WriteLine($"proxy listening on http://{opts.Bind}:{opts.Port}{opts.Path} → {target}");
        if (f.ContainsKey("once"))
        {
            // Start the real Kestrel bind, hit /health, then stop (same pattern as server --once).
            var urls = new[] { $"http://127.0.0.1:{opts.Port}" };
            using var app = ProxyCommand.Build(opts, urls);
            app.StartAsync().GetAwaiter().GetResult();
            try
            {
                using var http = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(3) };
                var body = http.GetStringAsync($"http://127.0.0.1:{opts.Port}/health").GetAwaiter().GetResult();
                o.WriteLine("proxy ready");
                o.WriteLine("proxy health: " + body.Trim());
            }
            finally
            {
                app.StopAsync().GetAwaiter().GetResult();
            }

            return 0;
        }

        using var cts = new CancellationTokenSource();
        // WaitForCancel blocks on Ctrl-C in production; tests inject a no-op then cancel.
        var waiter = Task.Run(() =>
        {
            WaitCancel();
            cts.Cancel();
        });
        try
        {
            ProxyCommand.RunAsync(opts, cts.Token).GetAwaiter().GetResult();
        }
        catch (OperationCanceledException)
        {
            // expected on cancel
        }

        waiter.GetAwaiter().GetResult();
        return 0;
    }

    private static int RunListen(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm listen WS_URL — telnet client → remote WS terminal (matches Go/Python)");
            o.WriteLine("  WS_URL    upstream terminal WebSocket (ws:// or wss://)");
            o.WriteLine("  --host    bind host (default 127.0.0.1)");
            o.WriteLine("  --port    telnet listen port (default gateway port; 0 = ephemeral)");
            o.WriteLine("  --protocol  telnet|ssh (default telnet)");
            o.WriteLine("  --allow-unauthenticated  allow non-loopback telnet/ssh bind");
            o.WriteLine("  --once    bind, print ready, stop (no accept wait)");
            return 0;
        }

        var f = ParseFlags(args);
        // Positional WS_URL (Go: ExactArgs(1)); also accept --url for tests.
        string? wsUrl = null;
        if (f.TryGetValue("_0", out var pos0) &&
            (pos0.StartsWith("ws://", StringComparison.OrdinalIgnoreCase) ||
             pos0.StartsWith("wss://", StringComparison.OrdinalIgnoreCase)))
        {
            wsUrl = pos0;
        }
        else if (f.TryGetValue("url", out var urlFlag) && !string.IsNullOrEmpty(urlFlag))
        {
            wsUrl = urlFlag;
        }

        if (string.IsNullOrEmpty(wsUrl))
        {
            e.WriteLine("error: listen requires WS_URL (e.g. wss://host/path/ws/terminal)");
            return 1;
        }

        var proto = f.GetValueOrDefault("protocol", "telnet");
        var host = f.GetValueOrDefault("host", "127.0.0.1");
        var port = f.ContainsKey("port")
            ? int.Parse(f["port"])
            : (proto.Equals("ssh", StringComparison.OrdinalIgnoreCase)
                ? TerminalDefaults.GatewaySshPort
                : TerminalDefaults.GatewayTelnetPort);
        var allowUnauth = f.ContainsKey("allow-unauthenticated");

        if (proto.Equals("ssh", StringComparison.OrdinalIgnoreCase))
        {
            // Ephemeral port when --port 0 (FxSsh needs a concrete bind port).
            if (port == 0)
            {
                var probe = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
                probe.Start();
                port = ((System.Net.IPEndPoint)probe.LocalEndpoint).Port;
                probe.Stop();
            }

            var sshGw = new SshWsGateway(wsUrl, allowUnauthenticated: allowUnauth);
            try
            {
                sshGw.Start(host, port);
                o.WriteLine($"listen: ssh gateway on {host}:{sshGw.Port} → {wsUrl}");
                if (f.ContainsKey("once"))
                {
                    o.WriteLine("listen ready");
                    return 0;
                }

                WaitCancel();
            }
            finally
            {
                sshGw.StopAsync().GetAwaiter().GetResult();
            }

            return 0;
        }

        if (!proto.Equals("telnet", StringComparison.OrdinalIgnoreCase))
        {
            e.WriteLine("error: --protocol must be telnet or ssh");
            return 1;
        }

        var telnet = new TelnetGateway
        {
            AllowUnauthenticated = allowUnauth,
            OnAccept = (client, ct) => GatewayDrive.RunAsync(client, wsUrl, ct),
        };
        try
        {
            telnet.StartAsync(host, port).GetAwaiter().GetResult();
            o.WriteLine($"listen: telnet gateway on {host}:{telnet.Port} → {wsUrl}");
            if (f.ContainsKey("once"))
            {
                o.WriteLine("listen ready");
                return 0;
            }

            WaitCancel();
        }
        finally
        {
            telnet.StopAsync().GetAwaiter().GetResult();
        }

        return 0;
    }

    private static int RunShare(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm share — share a local process over a tunnel");
            o.WriteLine("  --command   process to run (default /bin/sh)");
            o.WriteLine("  --url       tunnel WebSocket URL (optional)");
            o.WriteLine("  --token     bearer token");
            o.WriteLine("  --once      start process, report, stop");
            return 0;
        }

        var f = ParseFlags(args);
        var cmd = f.GetValueOrDefault("command", "/bin/sh");
        TunnelCli.Client? tunnel = null;
        if (f.TryGetValue("url", out var url) && !string.IsNullOrEmpty(url))
        {
            tunnel = new TunnelCli.Client(url, f.GetValueOrDefault("token", ""));
            try
            {
                tunnel.ConnectAsync().GetAwaiter().GetResult();
                o.WriteLine($"share: tunnel connected to {url}");
            }
            catch (Exception ex)
            {
                e.WriteLine($"error: tunnel connect failed: {ex.Message}");
                return 1;
            }
        }

        var share = new TunnelCli.PtyShareSession(cmd);
        try
        {
            share.StartAsync(tunnel).GetAwaiter().GetResult();
            o.WriteLine($"share: process started ({cmd}) running={share.Running}");
            if (!f.ContainsKey("once"))
            {
                WaitCancel();
            }
        }
        finally
        {
            share.DisposeAsync().AsTask().GetAwaiter().GetResult();
            tunnel?.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }

        return 0;
    }

    private static int RunTunnel(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm tunnel — tunnel client connect");
            o.WriteLine("  --url    tunnel WebSocket URL (required)");
            o.WriteLine("  --token  bearer token");
            o.WriteLine("  --once   connect, send hello control frame, close");
            return 0;
        }

        var f = ParseFlags(args);
        if (!f.TryGetValue("url", out var url) || string.IsNullOrEmpty(url))
        {
            e.WriteLine("error: --url is required");
            return 1;
        }

        var client = new TunnelCli.Client(url, f.GetValueOrDefault("token", ""));
        try
        {
            client.ConnectAsync().GetAwaiter().GetResult();
        }
        catch (Exception ex)
        {
            e.WriteLine($"error: connect failed: {ex.Message}");
            return 1;
        }

        try
        {
            o.WriteLine($"tunnel: connected to {url}");
            client.SendControlAsync(new Dictionary<string, object?>
            {
                ["type"] = "hello",
                ["version"] = Version,
            }).GetAwaiter().GetResult();
            o.WriteLine("tunnel: sent hello control frame");
            if (!f.ContainsKey("once"))
            {
                WaitCancel();
            }
        }
        finally
        {
            client.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }

        return 0;
    }

    private static int RunInspect(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm inspect — HTTP reverse-proxy inspector");
            o.WriteLine("  --upstream  upstream base URL (required)");
            o.WriteLine("  --host      bind host (default 127.0.0.1)");
            o.WriteLine("  --port      bind port (default 0 = ephemeral)");
            o.WriteLine("  --once      bind, report port, stop");
            return 0;
        }

        var f = ParseFlags(args);
        if (!f.TryGetValue("upstream", out var upstream) || string.IsNullOrEmpty(upstream))
        {
            e.WriteLine("error: --upstream is required");
            return 1;
        }

        var host = f.GetValueOrDefault("host", "127.0.0.1");
        var port = int.Parse(f.GetValueOrDefault("port", "0"));
        var proxy = new TunnelCli.HttpInspectProxy(upstream);
        try
        {
            proxy.StartAsync(host, port).GetAwaiter().GetResult();
            o.WriteLine($"inspect: proxying {upstream} on http://{host}:{proxy.Port}/");
            if (!f.ContainsKey("once"))
            {
                WaitCancel();
            }
        }
        finally
        {
            proxy.StopAsync().GetAwaiter().GetResult();
        }

        return 0;
    }

    private static async Task<int> RunWatch(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm watch — list sessions from a running server");
            o.WriteLine("  --url    server base URL (default http://127.0.0.1:8780)");
            o.WriteLine("  --token  bearer/dev token");
            return 0;
        }

        var f = ParseFlags(args);
        var url = f.GetValueOrDefault("url", $"http://{TerminalDefaults.ServerHost}:{TerminalDefaults.ServerPort}");
        using var client = string.IsNullOrEmpty(f.GetValueOrDefault("token"))
            ? new HijackClient(url)
            : HijackClient.WithBearer(url, f["token"]);
        try
        {
            var sessions = await client.ListSessions().ConfigureAwait(false);
            o.WriteLine(System.Text.Json.JsonSerializer.Serialize(sessions));
            return 0;
        }
        catch (Exception ex)
        {
            e.WriteLine($"error: {ex.Message}");
            return 1;
        }
    }

    private static int RunAudit(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help") || args.Length == 0)
        {
            o.WriteLine("uterm audit verify PATH — verify a tamper-evident WORM audit log");
            o.WriteLine("  --expected-seq   expected head sequence (with --expected-hash)");
            o.WriteLine("  --expected-hash  expected head record hash");
            return args.Length == 0 ? 1 : 0;
        }

        if (args[0] != "verify")
        {
            e.WriteLine("error: audit requires subcommand 'verify'");
            return 1;
        }

        var f = ParseFlags(args.Skip(1).ToArray());
        if (!f.TryGetValue("_0", out var path))
        {
            e.WriteLine("error: audit verify requires PATH");
            return 1;
        }

        AuditChain.ExpectedHead? head = null;
        var seqSet = f.ContainsKey("expected-seq");
        var hashSet = f.ContainsKey("expected-hash");
        if (seqSet != hashSet)
        {
            e.WriteLine("error: --expected-seq and --expected-hash must be given together");
            return 1;
        }

        if (seqSet)
        {
            head = new AuditChain.ExpectedHead
            {
                Seq = long.Parse(f["expected-seq"]),
                Hash = f["expected-hash"],
            };
        }

        var result = AuditChain.VerifyAuditLog(path, head);
        if (result.Ok)
        {
            o.WriteLine($"OK: {result.Count} records, head seq={result.HeadSeq?.ToString() ?? "None"} hash={result.HeadHash ?? "None"}");
            return 0;
        }

        o.WriteLine($"TAMPERED: {result.Reason} at seq={result.FirstBadSeq?.ToString() ?? "None"}");
        return 1;
    }

    private static async Task<int> RunServer(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm server — hosted provide-uterm server");
            o.WriteLine("  --config  path to server.toml");
            o.WriteLine("  --host    bind host");
            o.WriteLine("  --port    bind port");
            return 0;
        }

        var f = ParseFlags(args);
        var cfgPath = f.GetValueOrDefault("config");
        var config = ConfigLoader.Load(cfgPath);
        if (string.IsNullOrEmpty(cfgPath))
        {
            config.Auth.Mode = "dev_token";
        }

        if (f.TryGetValue("host", out var host))
        {
            config.Server.Host = host;
        }

        if (f.TryGetValue("port", out var portStr) && int.TryParse(portStr, out var port))
        {
            config.Server.Port = port;
        }

        config.Server.DerivePublicBaseUrl();
        // The async factory owns a control-plane engine, so runtime graphical
        // targets follow control_plane.backend: sqlite keeps them across
        // restarts, memory behaves as before. The engine is ours to dispose.
        var (server, devToken, engine) = await ServerFactory
            .CreateFromConfigAsync(config, Version).ConfigureAwait(false);
        o.WriteLine($"uterm server listening on http://{config.Server.Host}:{config.Server.Port}");
        // A server with the websocket auth gate switched off must say so. The
        // variable mints an admin principal for browser sockets, and until now
        // a server running that way was indistinguishable in its output from a
        // normal one — the only safeguard was a comment telling operators not
        // to set it.
        if (string.Equals(Environment.GetEnvironmentVariable("UTERM_TEST_MODE"), "1", StringComparison.Ordinal))
        {
            o.WriteLine(TestModeBanner.Warning);
        }
        if (!string.IsNullOrEmpty(devToken))
        {
            o.WriteLine($"dev_token: {devToken}");
        }

        try
        {
            await using (server)
            {
                await server.StartAsync().ConfigureAwait(false);
                if (f.ContainsKey("once"))
                {
                    await server.StopAsync().ConfigureAwait(false);
                    return 0;
                }

                // Interactive: wait for Ctrl-C (or test WaitForCancel hook), then stop.
                await Task.Run(WaitCancel).ConfigureAwait(false);
                await server.StopAsync().ConfigureAwait(false);
            }
        }
        finally
        {
            // Closes the connection so SQLite checkpoints its WAL; without this
            // the database file is left incomplete for the next process.
            await engine.CloseAsync(CancellationToken.None).ConfigureAwait(false);
        }

        return 0;
    }

    /// <summary>
    /// Test hook for the interactive Ctrl-C wait. Production default blocks until
    /// SIGINT; tests assign a no-op to exercise non-<c>--once</c> command paths.
    /// </summary>
    internal static Action WaitForCancel { get; set; } = DefaultWaitCancel;

    private static void WaitCancel() => WaitForCancel();

    /// <summary>Production SIGINT wait — excluded from coverage (not unit-testable).</summary>
    [System.Diagnostics.CodeAnalysis.ExcludeFromCodeCoverage]
    private static void DefaultWaitCancel()
    {
        var tcs = new TaskCompletionSource();
        Console.CancelKeyPress += (_, ev) =>
        {
            ev.Cancel = true;
            tcs.TrySetResult();
        };
        tcs.Task.GetAwaiter().GetResult();
    }
}
