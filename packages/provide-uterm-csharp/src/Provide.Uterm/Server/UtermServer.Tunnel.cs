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
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Tunnel;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Server;

/// <summary>
/// Binary tunnel WS + inspect page + tunnel host REST lifecycle
/// (Python routes/tunnels.py + Go routes_tunnels_full.go).
/// </summary>
public sealed partial class UtermServer
{
    private void MapTunnelRoutes(WebApplication app)
    {
        app.Map("/tunnel/{workerId}", HandleTunnelWs);
        app.MapGet("/app/inspect/{sessionId}", HandleInspectPage);
        // Host lifecycle REST (create/list/rotate/revoke + share consumer).
        app.MapPost("/api/tunnels", (Delegate)HandleCreateTunnel);
        app.MapGet("/api/tunnels", (Delegate)HandleListTunnels);
        app.MapDelete("/api/tunnels/{tunnelId}/tokens", (Delegate)HandleRevokeTunnelTokens);
        app.MapPost("/api/tunnels/{tunnelId}/tokens/rotate", (Delegate)HandleRotateTunnelTokens);
        app.MapGet("/s/{sessionId}", (Delegate)HandleShareConsumer);
    }

    private MemoryTunnelStore EnsureTunnelStore() =>
        _deps.TunnelStore ?? (_lazyTunnelStore ??= new MemoryTunnelStore());

    private MemoryTunnelStore? _lazyTunnelStore;

    private string TunnelBaseUrl(HttpContext ctx)
    {
        var pub = _deps.Config.Server.PublicBaseUrl;
        if (!string.IsNullOrWhiteSpace(pub))
        {
            return pub.TrimEnd('/');
        }

        var scheme = ctx.Request.Scheme;
        var host = ctx.Request.Host.Value ?? "127.0.0.1";
        return $"{scheme}://{host}";
    }

    private static string WsBaseUrl(string httpBase) =>
        httpBase.Replace("https://", "wss://", StringComparison.OrdinalIgnoreCase)
            .Replace("http://", "ws://", StringComparison.OrdinalIgnoreCase);

    private static string TunnelSharePage(string tunnelType) =>
        tunnelType == "http" ? "inspect" : "session";

    private async Task<IResult> HandleCreateTunnel(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.CanCreateSession(p))
        {
            return DetailError(403, "insufficient privileges");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var tunnelType = Str(body, "tunnel_type", "terminal").Trim();
        if (string.IsNullOrEmpty(tunnelType)) tunnelType = "terminal";
        var displayName = Str(body, "display_name", "tunnel").Trim();
        if (string.IsNullOrEmpty(displayName)) displayName = "tunnel";
        var tunnelId = "tunnel-" + Guid.NewGuid().ToString("N")[..12];

        var workerToken = TunnelTokens.GenerateToken();
        var shareToken = TunnelTokens.GenerateToken();
        var controlToken = TunnelTokens.GenerateToken();

        var tunnelCfg = _deps.Config.Tunnel;
        var requestedTtl = tunnelCfg.TokenTtlS;
        if (body.TryGetValue("ttl_s", out var ttlEl) && ttlEl.ValueKind == JsonValueKind.Number)
        {
            requestedTtl = (int)ttlEl.GetDouble();
        }

        var ttlS = Math.Clamp(requestedTtl, 60, Math.Max(60, tunnelCfg.TokenTtlS * 24));
        var now = _clock.Wall();
        var expiresAt = now + ttlS;
        string? issuedIp = null;
        if (tunnelCfg.IpBinding)
        {
            issuedIp = ctx.Connection.RemoteIpAddress?.ToString();
        }

        _deps.Registry.Upsert(new SessionDefinition
        {
            SessionId = tunnelId,
            DisplayName = displayName,
            ConnectorType = "websocket",
            Visibility = "private",
            Owner = p.SubjectId,
        });

        var store = EnsureTunnelStore();
        store.PutToken(tunnelId, new TokenRecord
        {
            WorkerTokenHash = TunnelTokens.HashToken(workerToken),
            ShareTokenHash = TunnelTokens.HashToken(shareToken),
            ControlTokenHash = TunnelTokens.HashToken(controlToken),
            CreatedAt = now,
            ExpiresAt = expiresAt,
            IssuedIp = issuedIp,
            TunnelType = tunnelType,
            SharePage = TunnelSharePage(tunnelType),
        });
        var (shareInvite, controlInvite) = store.IssueInvites(
            tunnelId, shareToken, controlToken, expiresAt, now, issuedIp);

        var baseUrl = TunnelBaseUrl(ctx);
        return Results.Json(new
        {
            tunnel_id = tunnelId,
            display_name = displayName,
            tunnel_type = tunnelType,
            ws_endpoint = WsBaseUrl(baseUrl) + "/tunnel/" + tunnelId,
            worker_token = workerToken,
            share_url = baseUrl + "/s/" + tunnelId + "?invite=" + shareInvite,
            control_url = baseUrl + "/s/" + tunnelId + "?invite=" + controlInvite,
            expires_at = expiresAt,
        }, JsonOpts);
    }

    private async Task<IResult> HandleListTunnels(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        var isAdmin = _deps.Authz.IsAdmin(p);
        var store = EnsureTunnelStore();
        var outList = new List<object>();
        foreach (var (id, rec) in store.ListTokens())
        {
            if (!isAdmin)
            {
                if (!_deps.Registry.TryGetDefinition(id, out var def) || !_deps.Authz.IsOwner(p, def))
                {
                    continue;
                }
            }

            outList.Add(new
            {
                tunnel_id = id,
                tunnel_type = rec.TunnelType,
                share_page = rec.SharePage,
                created_at = rec.CreatedAt,
                expires_at = rec.ExpiresAt,
            });
        }

        return Results.Json(outList, JsonOpts);
    }

    private async Task<IResult> HandleRevokeTunnelTokens(HttpContext ctx, string tunnelId)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (_deps.Registry.TryGetDefinition(tunnelId, out var def) &&
            !(_deps.Authz.IsAdmin(p) || _deps.Authz.IsOwner(p, def)))
        {
            return DetailError(403, "insufficient privileges");
        }

        var store = EnsureTunnelStore();
        store.DeleteToken(tunnelId);
        store.DiscardInvitesForSession(tunnelId);
        return Results.Json(new { ok = true, session_id = tunnelId }, JsonOpts);
    }

    private async Task<IResult> HandleRotateTunnelTokens(HttpContext ctx, string tunnelId)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Registry.TryGetDefinition(tunnelId, out var def))
        {
            return DetailError(404, "unknown session: " + tunnelId);
        }

        if (!(_deps.Authz.IsAdmin(p) || _deps.Authz.IsOwner(p, def)))
        {
            return DetailError(403, "insufficient privileges");
        }

        var store = EnsureTunnelStore();
        var old = store.GetToken(tunnelId);
        if (old is null)
        {
            return DetailError(404, "no tunnel tokens for " + tunnelId);
        }

        var tunnelCfg = _deps.Config.Tunnel;
        var ttlS = tunnelCfg.TokenTtlS;
        var now = _clock.Wall();
        var expiresAt = now + ttlS;
        var workerToken = TunnelTokens.GenerateToken();
        var shareToken = TunnelTokens.GenerateToken();
        var controlToken = TunnelTokens.GenerateToken();
        string? issuedIp = null;
        if (tunnelCfg.IpBinding)
        {
            issuedIp = ctx.Connection.RemoteIpAddress?.ToString();
        }

        var tunnelType = string.IsNullOrEmpty(old.TunnelType) ? "terminal" : old.TunnelType;
        store.PutToken(tunnelId, new TokenRecord
        {
            WorkerTokenHash = TunnelTokens.HashToken(workerToken),
            ShareTokenHash = TunnelTokens.HashToken(shareToken),
            ControlTokenHash = TunnelTokens.HashToken(controlToken),
            CreatedAt = now,
            ExpiresAt = expiresAt,
            IssuedIp = issuedIp,
            TunnelType = tunnelType,
            SharePage = TunnelSharePage(tunnelType),
        });
        store.DiscardInvitesForSession(tunnelId);
        var (shareInvite, controlInvite) = store.IssueInvites(
            tunnelId, shareToken, controlToken, expiresAt, now, issuedIp);
        var baseUrl = TunnelBaseUrl(ctx);
        return Results.Json(new
        {
            tunnel_id = tunnelId,
            ws_endpoint = WsBaseUrl(baseUrl) + "/tunnel/" + tunnelId,
            worker_token = workerToken,
            share_url = baseUrl + "/s/" + tunnelId + "?invite=" + shareInvite,
            control_url = baseUrl + "/s/" + tunnelId + "?invite=" + controlInvite,
            expires_at = expiresAt,
        }, JsonOpts);
    }

    private IResult HandleShareConsumer(HttpContext ctx, string sessionId)
    {
        if (!SafeId.IsMatch(sessionId))
        {
            return DetailError(422, "invalid session_id");
        }

        var store = EnsureTunnelStore();
        var entry = store.GetToken(sessionId);
        var inviteValue = ctx.Request.Query["invite"].ToString();
        Invite? invite = null;
        if (!string.IsNullOrEmpty(inviteValue))
        {
            invite = store.ConsumeInviteValue(inviteValue, sessionId, _clock.Wall());
            if (invite is null)
            {
                return DetailError(403, "invalid or expired invite");
            }

            if (entry is not null)
            {
                var tokenHash = invite.Role == TunnelRole.Operator
                    ? entry.ControlTokenHash
                    : entry.ShareTokenHash;
                if (!TunnelTokens.InviteMatchesTokenHash(invite, tokenHash))
                {
                    return DetailError(403, "stale invite");
                }
            }
        }

        var page = entry?.SharePage;
        if (string.IsNullOrEmpty(page)) page = "session";
        if (invite is not null && invite.Role == TunnelRole.Operator)
        {
            page = "operator";
        }

        var appPath = string.IsNullOrWhiteSpace(_deps.Config.Ui.AppPath) ? "/app" : _deps.Config.Ui.AppPath.TrimEnd('/');
        var target = appPath + "/" + page + "/" + sessionId;
        if (invite is not null)
        {
            var cookieOpts = new CookieOptions
            {
                Path = "/",
                HttpOnly = true,
                Secure = _deps.Config.Tunnel.CookieSecure,
                SameSite = _deps.Config.Tunnel.CookieSamesite.ToLowerInvariant() switch
                {
                    "strict" => SameSiteMode.Strict,
                    "none" => SameSiteMode.None,
                    _ => SameSiteMode.Lax,
                },
            };
            ctx.Response.Cookies.Append("uterm_tunnel_" + sessionId, invite.TunnelToken, cookieOpts);
        }

        return Results.Redirect(target);
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
        // Same as the worker WS route: a configured session's own mode, seeded
        // before registration creates the state with the unknown-worker
        // default, so attaching a tunnel does not silently arbitrate a session
        // configured as open. A tunnel that means `hijack` says so in its
        // `open` control frame, which HandleTunnelControl applies.
        if (_deps.Registry.TryGetDefinition(workerId, out var tdef))
        {
            _deps.Hub.Registry.SetDefault(workerId, new Hub.WorkerTermState { InputMode = tdef.InputMode });
        }

        _deps.Hub.Conn.RegisterWorker(workerId, conn);
        var st = _deps.Hub.Registry.Get(workerId);
        if (st is not null)
        {
            st.IsTunnelWorker = true;
        }

        if (_deps.Registry is InMemorySessionRegistry mem)
        {
            mem.MarkWorker(workerId, true, false);
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

        try
        {
            while (ws.State == WebSocketState.Open)
            {
                WebSocketMessage message;
                try
                {
                    message = await WebSocketMessageReader.ReadAsync(
                        ws, _deps.Hub.MaxWsMessageBytes, ctx.RequestAborted).ConfigureAwait(false);
                }
                catch (WebSocketMessageException ex)
                {
                    await ws.CloseAsync(ex.CloseStatus, ex.Message, CancellationToken.None).ConfigureAwait(false);
                    break;
                }

                if (message.IsClose) break;
                if (message.Payload.Length < 2) continue;

                TunnelFrame frame;
                try
                {
                    frame = TunnelCodec.DecodeFrame(message.Payload);
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
            // Inside the identity check, for the reason the worker WS route
            // documents: a displaced tunnel socket closing says nothing about
            // the session the socket that replaced it is still serving.
            var (shouldBroadcast, wasHijacked) = _deps.Hub.Conn.DeregisterWorker(workerId, conn);
            if (shouldBroadcast)
            {
                if (_deps.Registry is InMemorySessionRegistry mem2)
                {
                    mem2.MarkWorker(workerId, false, false);
                }

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
