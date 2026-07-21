using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using StreamJsonRpc;
using Provide.Uterm.Client;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Server;

public sealed partial class UtermServer
{
    private void MapMcpRoutes(WebApplication app)
    {
        // MCP over WebSocket — requires authenticated principal (Bearer header only;
        // query-string tokens are rejected to avoid log/Referer leakage).
        // Hello still advertises mcp_supported per behavior.json (csharp=false) on
        // the browser path; this endpoint remains available for local tooling but
        // is gated the same way as other privileged surfaces.
        app.Map("/mcp", async (HttpContext ctx) =>
        {
            if (!string.IsNullOrEmpty(ctx.Request.Query["token"].ToString()))
            {
                ctx.Response.StatusCode = 400;
                await ctx.Response.WriteAsync("token query parameter is not allowed; use Authorization: Bearer").ConfigureAwait(false);
                return;
            }

            if (!ctx.WebSockets.IsWebSocketRequest)
            {
                ctx.Response.StatusCode = 400;
                return;
            }

            var (principal, authErr) = await RequireAuthenticated(ctx).ConfigureAwait(false);
            if (authErr is not null)
            {
                await authErr.ExecuteAsync(ctx).ConfigureAwait(false);
                return;
            }

            // Operator+ required (viewer cannot drive MCP hijack tools).
            var role = principal.Roles.Contains("admin") ? "admin"
                : principal.Roles.Contains("operator") ? "operator" : "viewer";
            if (role is "viewer")
            {
                ctx.Response.StatusCode = 403;
                await ctx.Response.WriteAsync("insufficient privileges").ConfigureAwait(false);
                return;
            }

            var token = "";
            if (ctx.Request.Headers.TryGetValue("Authorization", out var authHeader))
            {
                var val = authHeader.ToString();
                if (val.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase))
                {
                    token = val["Bearer ".Length..].Trim();
                }
            }

            using var ws = await ctx.WebSockets.AcceptWebSocketAsync().ConfigureAwait(false);
            var baseUrl = _deps.Config.Server.PublicBaseUrl ?? $"http://127.0.0.1:{_deps.Config.Server.Port}";
            var client = string.IsNullOrEmpty(token) ? new HijackClient(baseUrl) : HijackClient.WithBearer(baseUrl, token);
            var target = new McpToolsTarget(client);

            using var rpc = new JsonRpc(new StreamJsonRpc.WebSocketMessageHandler(ws, new SystemTextJsonFormatter()), target);
            rpc.StartListening();
            await rpc.Completion.ConfigureAwait(false);
        });
    }
}

public sealed class McpToolsTarget
{
    private readonly HijackClient _client;

    public McpToolsTarget(HijackClient client)
    {
        _client = client;
    }

    [JsonRpcMethod("tools/list")]
    public object ListTools()
    {
        return new
        {
            tools = new object[]
            {
                new {
                    name = "hijack_begin",
                    description = "Acquire a lease-based hijack session for a running worker.",
                    inputSchema = new {
                        type = "object",
                        properties = new {
                            worker_id = new { type = "string" },
                            lease_s = new { type = "integer", @default = 90 },
                            owner = new { type = "string", @default = "operator" }
                        },
                        required = new[] { "worker_id" }
                    }
                },
                new {
                    name = "hijack_heartbeat",
                    description = "Extend a hijack lease.",
                    inputSchema = new {
                        type = "object",
                        properties = new {
                            worker_id = new { type = "string" },
                            hijack_id = new { type = "string" },
                            lease_s = new { type = "integer", @default = 90 }
                        },
                        required = new[] { "worker_id", "hijack_id" }
                    }
                },
                new {
                    name = "hijack_read",
                    description = "Read snapshot or events from an active hijack session.",
                    inputSchema = new {
                        type = "object",
                        properties = new {
                            worker_id = new { type = "string" },
                            hijack_id = new { type = "string" },
                            mode = new { type = "string", @default = "snapshot" },
                            wait_ms = new { type = "integer", @default = 1500 },
                            after_seq = new { type = "integer", @default = 0 },
                            limit = new { type = "integer", @default = 200 }
                        },
                        required = new[] { "worker_id", "hijack_id" }
                    }
                },
                new {
                    name = "hijack_send",
                    description = "Send input to a hijacked worker, optionally guarded by prompt/regex.",
                    inputSchema = new {
                        type = "object",
                        properties = new {
                            worker_id = new { type = "string" },
                            hijack_id = new { type = "string" },
                            keys = new { type = "string" },
                            expect_prompt_id = new { type = "string" },
                            expect_regex = new { type = "string" },
                            timeout_ms = new { type = "integer", @default = 2000 },
                            poll_interval_ms = new { type = "integer", @default = 120 }
                        },
                        required = new[] { "worker_id", "hijack_id", "keys" }
                    }
                },
                new {
                    name = "hijack_step",
                    description = "Single-step a hijacked worker loop.",
                    inputSchema = new {
                        type = "object",
                        properties = new {
                            worker_id = new { type = "string" },
                            hijack_id = new { type = "string" }
                        },
                        required = new[] { "worker_id", "hijack_id" }
                    }
                },
                new {
                    name = "hijack_release",
                    description = "Release hijack session and resume worker automation.",
                    inputSchema = new {
                        type = "object",
                        properties = new {
                            worker_id = new { type = "string" },
                            hijack_id = new { type = "string" }
                        },
                        required = new[] { "worker_id", "hijack_id" }
                    }
                }
            }
        };
    }

    [JsonRpcMethod("tools/call")]
    public async Task<object> CallTool(string name, JsonElement arguments, CancellationToken ct)
    {
        try 
        {
            Dictionary<string, object?> data;
            
            string workerId = arguments.TryGetProperty("worker_id", out var w) ? w.GetString()! : "";
            string hijackId = arguments.TryGetProperty("hijack_id", out var h) ? h.GetString()! : "";

            switch (name)
            {
                case "hijack_begin":
                    int leaseS = arguments.TryGetProperty("lease_s", out var ls) ? ls.GetInt32() : 90;
                    string owner = arguments.TryGetProperty("owner", out var o) ? o.GetString()! : "operator";
                    data = await _client.Acquire(workerId, owner, leaseS, ct).ConfigureAwait(false);
                    break;
                case "hijack_heartbeat":
                    int leaseS2 = arguments.TryGetProperty("lease_s", out var ls2) ? ls2.GetInt32() : 90;
                    data = await _client.Heartbeat(workerId, hijackId, leaseS2, ct).ConfigureAwait(false);
                    break;
                case "hijack_read":
                    string mode = arguments.TryGetProperty("mode", out var m) ? m.GetString()! : "snapshot";
                    if (mode == "events")
                    {
                        int afterSeq = arguments.TryGetProperty("after_seq", out var a) ? a.GetInt32() : 0;
                        int limit = arguments.TryGetProperty("limit", out var lim) ? lim.GetInt32() : 200;
                        data = await _client.Events(workerId, hijackId, afterSeq, limit, ct).ConfigureAwait(false);
                    }
                    else
                    {
                        int waitMs = arguments.TryGetProperty("wait_ms", out var wms) ? wms.GetInt32() : 1500;
                        data = await _client.Snapshot(workerId, hijackId, waitMs, ct).ConfigureAwait(false);
                    }
                    break;
                case "hijack_send":
                    string keys = arguments.TryGetProperty("keys", out var k) ? k.GetString()! : "";
                    string? expectPromptId = arguments.TryGetProperty("expect_prompt_id", out var p) ? p.GetString() : null;
                    string? expectRegex = arguments.TryGetProperty("expect_regex", out var r) ? r.GetString() : null;
                    int timeoutMs = arguments.TryGetProperty("timeout_ms", out var t) ? t.GetInt32() : 2000;
                    int pollIntervalMs = arguments.TryGetProperty("poll_interval_ms", out var pi) ? pi.GetInt32() : 120;
                    
                    data = await _client.Send(workerId, hijackId, Provide.Uterm.Sanitizer.KeystrokeSanitizer.PrepareKeystrokes(keys), expectPromptId, expectRegex, timeoutMs, pollIntervalMs, ct).ConfigureAwait(false);
                    break;
                case "hijack_step":
                    data = await _client.Step(workerId, hijackId, ct).ConfigureAwait(false);
                    break;
                case "hijack_release":
                    data = await _client.Release(workerId, hijackId, ct).ConfigureAwait(false);
                    break;
                default:
                    throw new Exception($"Unknown tool: {name}");
            }

            data["success"] = true;
            return new {
                content = new[] {
                    new {
                        type = "text",
                        text = JsonSerializer.Serialize(data)
                    }
                }
            };
        }
        catch (Provide.Uterm.Client.ApiException ex)
        {
            var errData = new Dictionary<string, object?> { ["success"] = false, ["error"] = ex.Message, ["status"] = (int)ex.StatusCode };
            return new {
                content = new[] {
                    new {
                        type = "text",
                        text = JsonSerializer.Serialize(errData)
                    }
                },
                isError = true
            };
        }
        catch (Exception ex)
        {
            var errData = new Dictionary<string, object?> { ["success"] = false, ["error"] = ex.Message };
            return new {
                content = new[] {
                    new {
                        type = "text",
                        text = JsonSerializer.Serialize(errData)
                    }
                },
                isError = true
            };
        }
    }
}
