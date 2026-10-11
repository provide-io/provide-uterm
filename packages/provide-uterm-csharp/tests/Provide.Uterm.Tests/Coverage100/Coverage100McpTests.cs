//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.WebSockets;
using System.Text.Json;
using Provide.Uterm.Hub;
using StreamJsonRpc;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// The <c>/mcp</c> WebSocket: its refusals, and every hijack tool driven over
/// JSON-RPC against a live server with a registered worker.
/// </summary>
public sealed class Coverage100McpTests
{
    private sealed class OkWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) => Task.CompletedTask;
    }

    private static Task<C100Server> StartAsync(bool publicBaseUrl = true) =>
        C100Server.StartAsync(new C100Server.Options
        {
            Configure = cfg =>
            {
                if (!publicBaseUrl)
                {
                    cfg.Server.PublicBaseUrl = ""; // unset: the tools call back on 127.0.0.1:<port>
                }
            },
            Deps = (cfg, hub) =>
            {
                Assert.True(hub.Conn.RegisterWorker(C100Server.Session, new OkWorker()));
                return C100Server.Default(cfg, hub);
            },
        });

    private static Uri McpUri(C100Server s) => new(s.BaseUrl.Replace("http", "ws", StringComparison.Ordinal) + "/mcp");

    private static async Task<HttpStatusCode> RefusedWsAsync(C100Server s, Action<ClientWebSocketOptions>? configure = null)
    {
        using var ws = new ClientWebSocket();
        ws.Options.CollectHttpResponseDetails = true;
        configure?.Invoke(ws.Options);
        await Assert.ThrowsAsync<WebSocketException>(() => ws.ConnectAsync(McpUri(s), CancellationToken.None));
        return ws.HttpStatusCode;
    }

    [Fact]
    public async Task Mcp_refuses_query_tokens_plain_http_anonymous_and_viewer_callers()
    {
        await using var s = await StartAsync();
        using var http = s.Http();
        var query = await http.GetAsync("/mcp?token=abc");
        Assert.Equal(HttpStatusCode.BadRequest, query.StatusCode);
        Assert.Contains("use Authorization: Bearer", await query.Content.ReadAsStringAsync(), StringComparison.Ordinal);
        Assert.Equal(HttpStatusCode.BadRequest, (await http.GetAsync("/mcp")).StatusCode);

        Assert.Equal(HttpStatusCode.Unauthorized, await RefusedWsAsync(s));
        var viewer = s.Token("watcher", "viewer");
        Assert.Equal(HttpStatusCode.Forbidden, await RefusedWsAsync(s, o => o.SetRequestHeader("Authorization", "Bearer " + viewer)));
    }

    private static async Task<(JsonRpc Rpc, ClientWebSocket Ws)> OpenAsync(C100Server s, Action<ClientWebSocketOptions> auth)
    {
        var ws = new ClientWebSocket();
        auth(ws.Options);
        await ws.ConnectAsync(McpUri(s), CancellationToken.None);
        var rpc = new JsonRpc(new WebSocketMessageHandler(ws, new SystemTextJsonFormatter()));
        rpc.StartListening();
        return (rpc, ws);
    }

    private static async Task<(bool IsError, JsonElement Body)> CallAsync(JsonRpc rpc, string name, object arguments)
    {
        var result = await rpc.InvokeWithParameterObjectAsync<JsonElement>("tools/call", new { name, arguments });
        var text = result.GetProperty("content")[0].GetProperty("text").GetString()!;
        var isError = result.TryGetProperty("isError", out var e) && e.GetBoolean();
        return (isError, JsonDocument.Parse(text).RootElement.Clone());
    }

    [Fact]
    public async Task Mcp_tools_drive_a_whole_lease_over_json_rpc()
    {
        await using var s = await StartAsync();
        var (rpc, ws) = await OpenAsync(s, o => o.SetRequestHeader("Authorization", "Bearer " + s.AdminToken));
        using (ws)
        using (rpc)
        {
            var tools = await rpc.InvokeAsync<JsonElement>("tools/list");
            Assert.Equal(
                new[] { "hijack_begin", "hijack_heartbeat", "hijack_read", "hijack_send", "hijack_step", "hijack_release" },
                tools.GetProperty("tools").EnumerateArray().Select(t => t.GetProperty("name").GetString()).ToArray());

            var session = C100Server.Session;
            var (err, begun) = await CallAsync(rpc, "hijack_begin", new { worker_id = session });
            Assert.False(err, begun.ToString());
            Assert.True(begun.GetProperty("success").GetBoolean());
            var id = begun.GetProperty("hijack_id").GetString()!;

            // A second lease on the same worker is refused by the API: an error result with its status.
            var (busyErr, busy) = await CallAsync(rpc, "hijack_begin", new { worker_id = session, lease_s = 30, owner = "other" });
            Assert.True(busyErr);
            Assert.False(busy.GetProperty("success").GetBoolean());
            Assert.Equal(409, busy.GetProperty("status").GetInt32());

            foreach (var (tool, args) in new (string, object)[]
            {
                ("hijack_heartbeat", new { worker_id = session, hijack_id = id }),
                ("hijack_heartbeat", new { worker_id = session, hijack_id = id, lease_s = 45 }),
                ("hijack_read", new { worker_id = session, hijack_id = id, mode = "snapshot", wait_ms = 0 }),
                ("hijack_read", new { worker_id = session, hijack_id = id, mode = "events", after_seq = 0, limit = 5 }),
                ("hijack_read", new { worker_id = session, hijack_id = id, mode = "events" }),
                ("hijack_send", new { worker_id = session, hijack_id = id, keys = "ls\n", timeout_ms = 100, poll_interval_ms = 10 }),
                ("hijack_step", new { worker_id = session, hijack_id = id }),
                ("hijack_release", new { worker_id = session, hijack_id = id }),
            })
            {
                var (toolErr, body) = await CallAsync(rpc, tool, args);
                Assert.False(toolErr, $"{tool}: {body}");
                Assert.True(body.GetProperty("success").GetBoolean(), tool);
            }

            var (unknownErr, unknown) = await CallAsync(rpc, "hijack_teleport", new { });
            Assert.True(unknownErr);
            Assert.Equal("Unknown tool: hijack_teleport", unknown.GetProperty("error").GetString());
        }
    }

    [Fact]
    public async Task Mcp_without_a_bearer_calls_back_anonymously_on_the_bound_port()
    {
        await using var s = await StartAsync(publicBaseUrl: false);
        var operatorToken = s.Token("op", "operator");
        // Authenticated by cookie (with an unrelated Authorization header):
        // there is no bearer to forward, so the tools' callbacks are anonymous.
        var (rpc, ws) = await OpenAsync(s, o =>
        {
            o.Cookies = new CookieContainer();
            o.Cookies.Add(new Uri(s.BaseUrl), new Cookie(s.Config.Auth.TokenCookie, operatorToken));
            o.SetRequestHeader("Authorization", "Basic Zm9vOmJhcg==");
        });
        using (ws)
        using (rpc)
        {
            var (err, body) = await CallAsync(rpc, "hijack_begin", new { worker_id = C100Server.Session });
            Assert.True(err);
            Assert.Equal(401, body.GetProperty("status").GetInt32());
        }
    }
}
