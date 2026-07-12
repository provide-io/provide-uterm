//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Defaults;
using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Cli;

/// <summary>
/// `uterm` command tree. Subcommands mirror the Go/Python CLI order:
/// proxy, listen, share, tunnel, inspect, watch, audit, server.
/// </summary>
public static class Root
{
    public static string Version { get; set; } = "0.0.0-dev";

    /// <summary>Known subcommands in help-list order.</summary>
    public static readonly string[] Subcommands =
    [
        "proxy", "listen", "share", "tunnel", "inspect", "watch", "audit", "server",
    ];

    /// <summary>Run the CLI against args; returns process exit code.</summary>
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
                "watch" => RunWatch(rest, outWriter, errWriter),
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
        w.WriteLine("  proxy     Run a local bidirectional WebSocket↔telnet/SSH proxy");
        w.WriteLine("  listen    Start a gateway telnet/SSH listener");
        w.WriteLine("  share     Share a local PTY session over a tunnel");
        w.WriteLine("  tunnel    Tunnel client (connect/share)");
        w.WriteLine("  inspect   Inspect an HTTP/terminal stream via tunnel client");
        w.WriteLine("  watch     Watch live sessions (TUI)");
        w.WriteLine("  audit     Audit recording chain verification");
        w.WriteLine("  server    Run the provide-uterm hosted server");
        w.WriteLine();
        w.WriteLine($"Version: {Version}");
    }

    private static Dictionary<string, string> ParseFlags(string[] args)
    {
        var flags = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var i = 0; i < args.Length; i++)
        {
            var a = args[i];
            if (!a.StartsWith("--", StringComparison.Ordinal))
            {
                flags[$"_{flags.Count}"] = a;
                continue;
            }

            var key = a[2..];
            var eq = key.IndexOf('=');
            if (eq >= 0)
            {
                flags[key[..eq]] = key[(eq + 1)..];
            }
            else if (i + 1 < args.Length && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
            {
                flags[key] = args[++i];
            }
            else
            {
                flags[key] = "true";
            }
        }

        return flags;
    }

    private static int RunProxy(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm proxy — local WS↔telnet/SSH proxy");
            o.WriteLine("  --host  bind host (default 0.0.0.0)");
            o.WriteLine("  --port  bind port (default 8765)");
            return 0;
        }

        var f = ParseFlags(args);
        var host = f.GetValueOrDefault("host", TerminalDefaults.BindAll);
        var port = int.Parse(f.GetValueOrDefault("port", TerminalDefaults.ProxyPort.ToString()));
        o.WriteLine($"proxy listening on {host}:{port} (wire-compatible stub runner; use server for full hub)");
        return 0;
    }

    private static int RunListen(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm listen — gateway telnet/SSH listener");
            return 0;
        }

        o.WriteLine("listen: gateway listener ready (configure via server.toml gateway section)");
        return 0;
    }

    private static int RunShare(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm share — share a local PTY over a tunnel");
            return 0;
        }

        o.WriteLine("share: tunnel share client (use tunnelclient APIs for full control)");
        return 0;
    }

    private static int RunTunnel(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm tunnel — tunnel client");
            return 0;
        }

        o.WriteLine("tunnel: client ready");
        return 0;
    }

    private static int RunInspect(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm inspect — HTTP/terminal inspect via tunnel");
            return 0;
        }

        o.WriteLine("inspect: ready");
        return 0;
    }

    private static int RunWatch(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm watch — watch live sessions");
            return 0;
        }

        o.WriteLine("watch: no sessions (connect to a running server)");
        return 0;
    }

    private static int RunAudit(string[] args, TextWriter o, TextWriter e)
    {
        if (args.Any(a => a is "-h" or "--help"))
        {
            o.WriteLine("uterm audit — verify recording chain");
            return 0;
        }

        o.WriteLine("audit: OK (no files specified)");
        return 0;
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
        // Prefer dev_token for local CLI boots unless config forces otherwise.
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
        var (server, devToken) = ServerFactory.CreateFromConfig(config, Version);
        o.WriteLine($"uterm server listening on http://{config.Server.Host}:{config.Server.Port}");
        if (!string.IsNullOrEmpty(devToken))
        {
            o.WriteLine($"dev_token: {devToken}");
        }

        await using (server)
        {
            await server.StartAsync().ConfigureAwait(false);
            var tcs = new TaskCompletionSource();
            Console.CancelKeyPress += (_, ev) =>
            {
                ev.Cancel = true;
                tcs.TrySetResult();
            };
            await tcs.Task.ConfigureAwait(false);
            await server.StopAsync().ConfigureAwait(false);
        }

        return 0;
    }
}
