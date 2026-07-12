//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Manager;

/// <summary>Entry point for the uterm-manager binary.</summary>
public static class ManagerHost
{
    public static int Run(string[] args)
    {
        if (args.Any(a => a is "-h" or "--help" or "help"))
        {
            Console.Out.WriteLine("uterm-manager — agent fleet External Management Tier");
            Console.Out.WriteLine("  --host  bind host (default 127.0.0.1)");
            Console.Out.WriteLine("  --port  bind port (default 8790)");
            Console.Out.WriteLine();
            Console.Out.WriteLine("Identity: provide-uterm-manager 0.0.0-dev");
            return 0;
        }

        if (args.Any(a => a is "-V" or "--version" or "version"))
        {
            Console.Out.WriteLine("0.0.0-dev");
            return 0;
        }

        var cfg = new ManagerConfig();
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i] == "--host" && i + 1 < args.Length)
            {
                cfg.Host = args[++i];
            }
            else if (args[i] == "--port" && i + 1 < args.Length && int.TryParse(args[i + 1], out var p))
            {
                cfg.Port = p;
                i++;
            }
        }

        var mgr = new AgentManager(cfg);
        Console.Out.WriteLine($"uterm-manager ready on http://{cfg.Host}:{cfg.Port}");
        Console.Out.WriteLine($"swarm: {mgr.GetSwarmStatus()["agents"]} agents");
        // Keep process alive until Ctrl-C when launched interactively without --once.
        if (!args.Contains("--once"))
        {
            var tcs = new TaskCompletionSource();
            Console.CancelKeyPress += (_, ev) =>
            {
                ev.Cancel = true;
                tcs.TrySetResult();
            };
            tcs.Task.GetAwaiter().GetResult();
        }

        return 0;
    }
}
