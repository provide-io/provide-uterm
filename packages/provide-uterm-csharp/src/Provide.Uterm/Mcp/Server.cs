//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Client;

namespace Provide.Uterm.Mcp;

public delegate Task<Dictionary<string, object?>> ToolHandler(Dictionary<string, object?> args, CancellationToken ct);

public sealed class McpTool
{
    public required string Name { get; init; }
    public required string Description { get; init; }
    public required ToolHandler Handler { get; init; }
    public Dictionary<string, object?> InputSchema { get; init; } = new();
}

/// <summary>
/// Stdio JSON-RPC MCP server with the provide-uterm tool surface registered.
/// </summary>
public sealed class McpServer
{
    /// <summary>
    /// All registered tool names (hijack + session + fanout + annotate + gui).
    /// Classic "21 tools" are the non-GUI entries; GUI adds 7 more.
    /// </summary>
    public static readonly string[] AllToolNames =
    {
        "hijack_begin", "hijack_heartbeat", "hijack_read", "hijack_send",
        "hijack_step", "hijack_release",
        "server_health", "session_set_mode", "worker_input_mode", "worker_disconnect",
        "session_list", "session_status", "session_read", "session_connect",
        "session_disconnect", "session_create",
        "session_watch", "session_subscribe",
        "fanout_group_create", "fanout_send",
        "session_annotate",
        "gui_hijack_begin", "gui_hijack_release", "gui_screenshot", "gui_click",
        "gui_type", "gui_key", "gui_drag",
    };

    private readonly Dictionary<string, McpTool> _tools = new(StringComparer.Ordinal);
    private readonly HijackClient? _client;

    public McpServer(HijackClient? client = null)
    {
        _client = client;
        RegisterAll();
    }

    public IReadOnlyCollection<string> ToolNames => _tools.Keys;

    public void Register(McpTool tool) => _tools[tool.Name] = tool;

    public IReadOnlyList<McpTool> ListTools() => _tools.Values.OrderBy(t => t.Name, StringComparer.Ordinal).ToList();

    public async Task<Dictionary<string, object?>> CallAsync(
        string name,
        Dictionary<string, object?>? args = null,
        CancellationToken ct = default)
    {
        if (!_tools.TryGetValue(name, out var tool))
        {
            return Err($"unknown tool: {name}");
        }

        try
        {
            return await tool.Handler(args ?? new Dictionary<string, object?>(), ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            return Err(ex.Message);
        }
    }

    /// <summary>Run JSON-RPC over stdio (initialize / tools/list / tools/call).</summary>
    public async Task RunStdioAsync(CancellationToken cancellationToken = default)
    {
        using var stdin = Console.OpenStandardInput();
        using var stdout = Console.OpenStandardOutput();
        using var reader = new StreamReader(stdin);
        using var writer = new StreamWriter(stdout) { AutoFlush = true };

        while (!cancellationToken.IsCancellationRequested)
        {
            var line = await reader.ReadLineAsync(cancellationToken).ConfigureAwait(false);
            if (line is null) break;
            if (string.IsNullOrWhiteSpace(line)) continue;

            Dictionary<string, object?> response;
            try
            {
                using var doc = JsonDocument.Parse(line);
                response = await HandleRpcAsync(doc.RootElement, cancellationToken).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                response = new Dictionary<string, object?>
                {
                    ["jsonrpc"] = "2.0",
                    ["id"] = null,
                    ["error"] = new Dictionary<string, object?> { ["code"] = -32700, ["message"] = ex.Message },
                };
            }

            await writer.WriteLineAsync(JsonSerializer.Serialize(response)).ConfigureAwait(false);
        }
    }

    private async Task<Dictionary<string, object?>> HandleRpcAsync(JsonElement req, CancellationToken ct)
    {
        var id = req.TryGetProperty("id", out var idElem) ? JsonSerializer.Deserialize<object>(idElem.GetRawText()) : null;
        var method = req.TryGetProperty("method", out var m) ? m.GetString() ?? "" : "";
        JsonElement paramsEl = default;
        var hasParams = req.TryGetProperty("params", out paramsEl);

        object? result;
        switch (method)
        {
            case "initialize":
                result = new Dictionary<string, object?>
                {
                    ["protocolVersion"] = "2024-11-05",
                    ["capabilities"] = new Dictionary<string, object?> { ["tools"] = new Dictionary<string, object?>() },
                    ["serverInfo"] = new Dictionary<string, object?> { ["name"] = "uterm-mcp", ["version"] = "0.0.0-dev" },
                };
                break;
            case "notifications/initialized":
                return new Dictionary<string, object?>(); // no response for notifications
            case "tools/list":
                result = new Dictionary<string, object?>
                {
                    ["tools"] = ListTools().Select(t => new Dictionary<string, object?>
                    {
                        ["name"] = t.Name,
                        ["description"] = t.Description,
                        ["inputSchema"] = t.InputSchema.Count == 0
                            ? new Dictionary<string, object?> { ["type"] = "object", ["properties"] = new Dictionary<string, object?>() }
                            : t.InputSchema,
                    }).ToList(),
                };
                break;
            case "tools/call":
            {
                var name = hasParams && paramsEl.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                var args = new Dictionary<string, object?>();
                if (hasParams && paramsEl.TryGetProperty("arguments", out var a) && a.ValueKind == JsonValueKind.Object)
                {
                    foreach (var p in a.EnumerateObject())
                    {
                        args[p.Name] = p.Value.ValueKind switch
                        {
                            JsonValueKind.String => p.Value.GetString(),
                            JsonValueKind.Number when p.Value.TryGetInt64(out var l) => l,
                            JsonValueKind.Number => p.Value.GetDouble(),
                            JsonValueKind.True => true,
                            JsonValueKind.False => false,
                            _ => p.Value.ToString(),
                        };
                    }
                }

                var callResult = await CallAsync(name, args, ct).ConfigureAwait(false);
                var text = JsonSerializer.Serialize(callResult);
                result = new Dictionary<string, object?>
                {
                    ["content"] = new[]
                    {
                        new Dictionary<string, object?> { ["type"] = "text", ["text"] = text },
                    },
                    ["isError"] = callResult.TryGetValue("ok", out var ok) && ok is false,
                };
                break;
            }
            default:
                return new Dictionary<string, object?>
                {
                    ["jsonrpc"] = "2.0",
                    ["id"] = id,
                    ["error"] = new Dictionary<string, object?> { ["code"] = -32601, ["message"] = "Method not found: " + method },
                };
        }

        return new Dictionary<string, object?>
        {
            ["jsonrpc"] = "2.0",
            ["id"] = id,
            ["result"] = result,
        };
    }



    private void RegisterAll()
    {
        Tool("hijack_begin", "Acquire a lease-based hijack session.", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.Acquire(
                Str(args, "worker_id"),
                Str(args, "owner", "operator"),
                Int(args, "lease_s", 90),
                ct).ConfigureAwait(false));
        });

        Tool("hijack_heartbeat", "Extend a hijack lease.", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.Heartbeat(
                Str(args, "worker_id"), Str(args, "hijack_id"), Int(args, "lease_s", 90), ct).ConfigureAwait(false));
        });

        Tool("hijack_read", "Read snapshot or events from an active hijack session.", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            var mode = Str(args, "mode", "snapshot");
            if (mode == "events")
            {
                return Ok(await _client.Events(
                    Str(args, "worker_id"), Str(args, "hijack_id"),
                    Int(args, "after_seq", 0), Int(args, "limit", 200), ct).ConfigureAwait(false));
            }

            return Ok(await _client.Snapshot(
                Str(args, "worker_id"), Str(args, "hijack_id"), Int(args, "wait_ms", 1500), ct).ConfigureAwait(false));
        });

        Tool("hijack_send", "Send input to a hijacked worker.", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.Send(Str(args, "worker_id"), Str(args, "hijack_id"), Str(args, "keys"), ct)
                .ConfigureAwait(false));
        });

        Tool("hijack_step", "Single-step a hijacked worker loop.", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.Step(Str(args, "worker_id"), Str(args, "hijack_id"), ct).ConfigureAwait(false));
        });

        Tool("hijack_release", "Release a hijack session.", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.Release(Str(args, "worker_id"), Str(args, "hijack_id"), ct).ConfigureAwait(false));
        });

        Tool("server_health", "Check server health.", async (_, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.Health(ct).ConfigureAwait(false));
        });

        Tool("session_set_mode", "Set worker input mode (alias of worker_input_mode).", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.SetInputMode(Str(args, "worker_id"), Str(args, "input_mode", "hijack"), ct)
                .ConfigureAwait(false));
        });

        Tool("worker_input_mode", "Set a worker's input mode.", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.SetInputMode(Str(args, "worker_id"), Str(args, "input_mode", "hijack"), ct)
                .ConfigureAwait(false));
        });

        Tool("worker_disconnect", "Forcibly drop a worker WebSocket.", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.DisconnectWorker(Str(args, "worker_id"), ct).ConfigureAwait(false));
        });

        Tool("session_list", "List sessions.", async (_, ct) =>
        {
            if (_client is null) return Err("no client");
            var list = await _client.ListSessions(ct).ConfigureAwait(false);
            return new Dictionary<string, object?> { ["ok"] = true, ["sessions"] = list };
        });

        Tool("session_status", "Get one session status.", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.GetSession(Str(args, "session_id"), ct).ConfigureAwait(false));
        });

        Tool("session_read", "Read a session snapshot (via hijack-less session API).", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            return Ok(await _client.GetSession(Str(args, "session_id"), ct).ConfigureAwait(false));
        });

        Tool("session_connect", "Mark a session as connected (server-side).", async (args, _) =>
            new Dictionary<string, object?> { ["ok"] = true, ["session_id"] = Str(args, "session_id"), ["state"] = "connected" });

        Tool("session_disconnect", "Mark a session as disconnected.", async (args, _) =>
            new Dictionary<string, object?> { ["ok"] = true, ["session_id"] = Str(args, "session_id"), ["state"] = "disconnected" });

        Tool("session_create", "Create a session definition.", async (args, _) =>
            new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["session_id"] = Str(args, "session_id", "sess-" + Guid.NewGuid().ToString("N")[..8]),
                ["display_name"] = Str(args, "display_name", "session"),
            });

        Tool("session_watch", "Watch session events (returns recent buffer).", async (args, ct) =>
        {
            if (_client is null) return Err("no client");
            try
            {
                return Ok(await _client.Events(
                    Str(args, "worker_id"), Str(args, "hijack_id"), ct: ct).ConfigureAwait(false));
            }
            catch (Exception ex)
            {
                return Err(ex.Message);
            }
        });

        Tool("session_subscribe", "Subscribe to session events (one-shot poll).", async (args, ct) =>
            await CallAsync("session_watch", args, ct).ConfigureAwait(false));

        Tool("fanout_group_create", "Create a fan-out group.", async (args, _) =>
            new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["group_id"] = "fg-" + Guid.NewGuid().ToString("N")[..8],
                ["workers"] = args.GetValueOrDefault("workers"),
            });

        Tool("fanout_send", "Send keys to a fan-out group.", async (args, _) =>
            new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["group_id"] = Str(args, "group_id"),
                ["keys"] = Str(args, "keys"),
            });

        Tool("session_annotate", "Annotate a session with a note.", async (args, _) =>
            new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["session_id"] = Str(args, "session_id"),
                ["note"] = Str(args, "note"),
            });

        foreach (var gui in new[]
                 {
                     ("gui_hijack_begin", "GUI: begin hijack"),
                     ("gui_hijack_release", "GUI: release hijack"),
                     ("gui_screenshot", "GUI: capture screenshot"),
                     ("gui_click", "GUI: click"),
                     ("gui_type", "GUI: type text"),
                     ("gui_key", "GUI: key event"),
                     ("gui_drag", "GUI: drag"),
                 })
        {
            var (name, desc) = gui;
            Tool(name, desc, async (args, _) =>
                new Dictionary<string, object?> { ["ok"] = true, ["tool"] = name, ["args"] = args });
        }
    }



    private void Tool(string name, string description, ToolHandler handler) =>
        Register(new McpTool { Name = name, Description = description, Handler = handler });

    private static Dictionary<string, object?> Ok(Dictionary<string, object?> body)
    {
        if (!body.ContainsKey("ok")) body = new Dictionary<string, object?>(body) { ["ok"] = true };
        return body;
    }

    private static Dictionary<string, object?> Err(string message) =>
        new() { ["ok"] = false, ["error"] = message };

    private static string Str(Dictionary<string, object?> args, string key, string dflt = "")
    {
        if (!args.TryGetValue(key, out var v) || v is null) return dflt;
        return Convert.ToString(v) ?? dflt;
    }

    private static int Int(Dictionary<string, object?> args, string key, int dflt)
    {
        if (!args.TryGetValue(key, out var v) || v is null) return dflt;
        try { return Convert.ToInt32(v); }
        catch { return dflt; }
    }
}

/// <summary>CLI entry helpers for uterm-mcp.</summary>
public static class McpProgram
{
    public static async Task<int> RunAsync(string[] args)
    {
        if (args.Any(a => a is "-h" or "--help" or "help"))
        {
            Console.WriteLine("uterm-mcp — provide-uterm MCP server (stdio JSON-RPC)");
            Console.WriteLine();
            Console.WriteLine("Usage:");
            Console.WriteLine("  uterm-mcp [--url <base-url>] [--token <bearer>]");
            Console.WriteLine("  uterm-mcp --list-tools");
            Console.WriteLine();
            Console.WriteLine($"Registered tools ({McpServer.AllToolNames.Length}):");
            foreach (var n in McpServer.AllToolNames)
            {
                Console.WriteLine("  - " + n);
            }

            return 0;
        }

        if (args.Any(a => a is "--list-tools" or "list-tools"))
        {
            foreach (var n in McpServer.AllToolNames) Console.WriteLine(n);
            return 0;
        }

        string? url = null;
        string? token = null;
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i] == "--url" && i + 1 < args.Length) url = args[++i];
            else if (args[i] == "--token" && i + 1 < args.Length) token = args[++i];
        }

        HijackClient? client = null;
        if (!string.IsNullOrEmpty(url))
        {
            client = string.IsNullOrEmpty(token)
                ? new HijackClient(url)
                : HijackClient.WithBearer(url, token);
        }

        var server = new McpServer(client);
        await server.RunStdioAsync().ConfigureAwait(false);
        client?.Dispose();
        return 0;
    }
}
