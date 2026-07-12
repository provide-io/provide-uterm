//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Mcp;

/// <summary>Entry point for the uterm-mcp binary.</summary>
public static class McpHost
{
    public static int Run(string[] args)
    {
        if (args.Any(a => a is "-h" or "--help" or "help"))
        {
            Console.Out.WriteLine("uterm-mcp — provide-uterm MCP server (stdio JSON-RPC)");
            Console.Out.WriteLine("  --url   base URL of a running uterm server");
            Console.Out.WriteLine("  --token bearer/dev token");
            Console.Out.WriteLine();
            Console.Out.WriteLine($"Tools ({McpServer.AllToolNames.Length}):");
            foreach (var name in McpServer.AllToolNames.OrderBy(n => n, StringComparer.Ordinal))
            {
                Console.Out.WriteLine("  - " + name);
            }

            return 0;
        }

        if (args.Any(a => a is "-V" or "--version" or "version"))
        {
            Console.Out.WriteLine("0.0.0-dev");
            return 0;
        }

        // Default: run stdio MCP server (blocks).
        var server = new McpServer();
        server.RunStdioAsync().GetAwaiter().GetResult();
        return 0;
    }
}
