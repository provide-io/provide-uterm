//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Server;

/// <summary>Binary tunnel WS + HTTP inspect page (Python tunnel/fastapi_routes + inspect_page_html).</summary>
public sealed partial class UtermServer
{
    private void MapTunnelRoutes(WebApplication app)
    {
        app.Map("/tunnel/{workerId}", HandleTunnelWs);
        app.MapGet("/app/inspect/{sessionId}", HandleInspectPage);
    }

    /// <summary>Minimal inspect bootstrap HTML (React main is loaded when assets exist).</summary>
    private IResult HandleInspectPage(HttpContext ctx, string sessionId)
    {
        if (!SafeId.IsMatch(sessionId))
        {
            return Results.NotFound();
        }

        // TEST_MODE / multi-backend: open page without JWT so Playwright can load UI.
        // Production still authenticates browser WS separately.
        var ui = _deps.Config.Ui;
        var assets = string.IsNullOrWhiteSpace(ui.AssetsPath) ? "/ui" : ui.AssetsPath.TrimEnd('/');
        var appPath = string.IsNullOrWhiteSpace(ui.AppPath) ? "/app" : ui.AppPath.TrimEnd('/');
        var bootstrap = JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["page_kind"] = "inspect",
            ["title"] = "Inspect " + sessionId,
            ["app_path"] = appPath,
            ["assets_path"] = assets,
            ["session_id"] = sessionId,
            ["surface"] = "operator",
            ["share_role"] = (string?)null,
        });
        // Prefer hashed main from vanilla-manifest when present under assets; fall back to module path.
        var mainScript = assets + "/assets/main.js";
        var html =
            "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">" +
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
            "<title>Inspect " + sessionId + "</title>" +
            "<link rel=\"stylesheet\" href=\"" + assets + "/assets/main.css\">" +
            "</head><body><div id=\"app-root\"></div>" +
            "<script id=\"app-bootstrap\" type=\"application/json\">" + bootstrap + "</script>" +
            "<script type=\"module\" src=\"" + mainScript + "\"></script>" +
            "</body></html>";
        return Results.Content(html, "text/html; charset=utf-8");
    }

    private async Task HandleTunnelWs(HttpContext ctx, string workerId)
    {
        if (!ctx.WebSockets.IsWebSocketRequest)
        {
            ctx.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }

        if (!SafeId.IsMatch(workerId))
        {
            ctx.Response.StatusCode = StatusCodes.Status422UnprocessableEntity;
            return;
        }

        if (!string.IsNullOrEmpty(_deps.Hub.WorkerToken))
        {
            var auth = ctx.Request.Headers.Authorization.ToString();
            var expected = "Bearer " + _deps.Hub.WorkerToken;
            if (!string.Equals(auth, expected, StringComparison.Ordinal))
            {
                ctx.Response.StatusCode = StatusCodes.Status401Unauthorized;
                return;
            }
        }

        using var ws = await ctx.WebSockets.AcceptWebSocketAsync().ConfigureAwait(false);
        var conn = new BrowserWsConn(ws);
        _deps.Hub.Conn.RegisterWorker(workerId, conn);
        var st = _deps.Hub.Registry.Get(workerId);
        if (st is not null)
        {
            st.IsTunnelWorker = true;
        }

        if (_deps.Registry is InMemorySessionRegistry mem)
        {
            mem.MarkWorker(workerId, true, false, InputModes.Hijack);
        }

        await _deps.Hub.Conn.BroadcastToBrowsersAsync(
            workerId,
            new Dictionary<string, object?>
            {
                ["type"] = "worker_connected",
                ["worker_id"] = workerId,
                ["ts"] = _clock.Wall(),
            },
            CancellationToken.None).ConfigureAwait(false);

        var buffer = new byte[65536];
        try
        {
            while (ws.State == WebSocketState.Open)
            {
                var result = await ws.ReceiveAsync(buffer, ctx.RequestAborted).ConfigureAwait(false);
                if (result.MessageType == WebSocketMessageType.Close) break;
                if (result.Count < 2) continue;

                TunnelFrame frame;
                try
                {
                    frame = TunnelCodec.DecodeFrame(buffer.AsSpan(0, result.Count));
                }
                catch
                {
                    continue;
                }

                if (frame.IsEof) continue;
                if (frame.IsControl)
                {
                    HandleTunnelControl(workerId, frame.Payload);
                    continue;
                }

                if (frame.Channel == TunnelProtocol.ChannelHttp)
                {
                    Dictionary<string, object?>? httpMsg;
                    try
                    {
                        httpMsg = TunnelCodec.DecodeControl(frame.Payload);
                    }
                    catch
                    {
                        continue;
                    }

                    httpMsg["_channel"] = "http";
                    _deps.Hub.AppendEventData(
                        workerId,
                        httpMsg.TryGetValue("type", out var t) ? t?.ToString() ?? "http" : "http",
                        httpMsg);
                    _deps.Hub.State.TouchActivity(workerId);
                    await _deps.Hub.Conn.BroadcastToBrowsersAsync(workerId, httpMsg, ctx.RequestAborted)
                        .ConfigureAwait(false);
                    continue;
                }

                if (frame.Channel >= TunnelProtocol.ChannelData && frame.Payload.Length > 0)
                {
                    var text = Encoding.UTF8.GetString(frame.Payload);
                    _deps.Hub.AppendEventData(workerId, "term", new Dictionary<string, object?> { ["data"] = text });
                    _deps.Hub.State.TouchActivity(workerId);
                    await _deps.Hub.Conn.BroadcastToBrowsersAsync(
                        workerId,
                        new Dictionary<string, object?>
                        {
                            ["type"] = "term",
                            ["data"] = text,
                            ["ts"] = _clock.Wall(),
                        },
                        ctx.RequestAborted).ConfigureAwait(false);
                }
            }
        }
        finally
        {
            var (shouldBroadcast, wasHijacked) = _deps.Hub.Conn.DeregisterWorker(workerId, conn);
            if (_deps.Registry is InMemorySessionRegistry mem2)
            {
                mem2.MarkWorker(workerId, false, false, InputModes.Hijack);
            }

            if (shouldBroadcast)
            {
                try
                {
                    await _deps.Hub.Conn.BroadcastToBrowsersAsync(
                        workerId,
                        new Dictionary<string, object?>
                        {
                            ["type"] = "worker_disconnected",
                            ["worker_id"] = workerId,
                            ["ts"] = _clock.Wall(),
                        },
                        CancellationToken.None).ConfigureAwait(false);
                    if (wasHijacked)
                    {
                        await _deps.Hub.BroadcastHijackStateAsync(workerId, CancellationToken.None)
                            .ConfigureAwait(false);
                    }
                }
                catch
                {
                    // best-effort
                }
            }
        }
    }

    private void HandleTunnelControl(string workerId, byte[] payload)
    {
        Dictionary<string, object?> msg;
        try
        {
            msg = TunnelCodec.DecodeControl(payload);
        }
        catch
        {
            return;
        }

        var mtype = msg.TryGetValue("type", out var t) ? t?.ToString() : null;
        if (mtype == "open")
        {
            var mode = msg.TryGetValue("input_mode", out var m) ? m?.ToString() : null;
            if (mode is "hijack" or "open")
            {
                var st = _deps.Hub.Registry.Get(workerId);
                if (st is not null) st.InputMode = mode;
            }
        }
        else if (mtype == "snapshot")
        {
            var screen = msg.TryGetValue("screen", out var s) ? s?.ToString() ?? "" : "";
            var snap = new Dictionary<string, object?>
            {
                ["type"] = "snapshot",
                ["screen"] = screen,
                ["ts"] = _clock.Wall(),
            };
            _deps.Hub.Conn.UpdateLastSnapshot(workerId, snap);
            _ = _deps.Hub.Conn.BroadcastToBrowsersAsync(workerId, snap, CancellationToken.None);
        }
    }
}
