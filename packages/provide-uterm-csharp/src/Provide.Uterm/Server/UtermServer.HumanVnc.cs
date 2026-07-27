//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Sockets;
using System.Net.WebSockets;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.Policy;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.Vnc;

namespace Provide.Uterm.Server;

/// <summary>
/// Human VNC WebSocket relay — path
/// <c>/worker/{workerId}/hijack/{hijackId}/gui/vnc</c>
/// (Go <c>ServeHumanRelay</c> semantics over raw RFB TCP, not litevirt gRPC).
/// </summary>
public sealed partial class UtermServer
{
    private static readonly StrictPolicyEngine HumanVncPolicy = new();

    /// <summary>
    /// Test seam: when set, used as the upstream duplex stream instead of dialing RFB.
    /// Lifetime (if returned) is disposed after the relay ends.
    /// </summary>
    internal Func<CancellationToken, Task<(Stream Stream, IAsyncDisposable? Lifetime)>>? HumanVncUpstreamFactory
    {
        get;
        set;
    }

    private void MapHumanVncRoutes(WebApplication app)
    {
        app.Map("/worker/{workerId}/hijack/{hijackId}/gui/vnc", HandleHumanVnc);
    }

    private async Task HandleHumanVnc(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err))
        {
            await err!.ExecuteAsync(ctx).ConfigureAwait(false);
            return;
        }

        // Go authenticated middleware parity: anonymous → 401 before capability check.
        var (p, authErr) = await RequireAuthenticated(ctx).ConfigureAwait(false);
        if (authErr is not null)
        {
            await authErr.ExecuteAsync(ctx).ConfigureAwait(false);
            return;
        }

        if (!AuthorizeHub(p, workerId, "session.control.hijack", out err))
        {
            await err!.ExecuteAsync(ctx).ConfigureAwait(false);
            return;
        }

        var hs = _deps.Hub.GetRestSession(workerId, hijackId);
        if (hs is null)
        {
            await BridgeError(404, "Invalid or expired hijack session.").ExecuteAsync(ctx).ConfigureAwait(false);
            return;
        }

        // Principal-bound: if lease is owned by someone else, refuse before upgrade.
        if (hs.AcquiredBy is not null && hs.AcquiredBy != p.SubjectId)
        {
            await BridgeError(403, "hijack lease not owned by caller").ExecuteAsync(ctx).ConfigureAwait(false);
            return;
        }

        var role = ResolvePrincipalRole(p, workerId);
        // Fail-closed inject: only pass hijackId as lease when this principal owns it.
        var leaseId = hs.AcquiredBy is not null && hs.AcquiredBy == p.SubjectId
            ? hijackId
            : string.Empty;

        Stream? upstream = null;
        IAsyncDisposable? upstreamLife = null;
        TcpClient? tcp = null;
        try
        {
            if (HumanVncUpstreamFactory is not null)
            {
                (upstream, upstreamLife) = await HumanVncUpstreamFactory(ctx.RequestAborted)
                    .ConfigureAwait(false);
            }
            else
            {
                var dialed = await DialRfbUpstreamAsync(ctx, p).ConfigureAwait(false);
                if (dialed is null)
                {
                    return; // HTTP error already written
                }

                (tcp, upstream) = dialed.Value;
            }

            if (!ctx.WebSockets.IsWebSocketRequest)
            {
                ctx.Response.StatusCode = StatusCodes.Status400BadRequest;
                return;
            }

            using var ws = await ctx.WebSockets.AcceptWebSocketAsync().ConfigureAwait(false);
            await RunHumanVncRelayAsync(
                    ws, upstream!, workerId, leaseId, p.SubjectId, role, ctx.RequestAborted)
                .ConfigureAwait(false);
        }
        finally
        {
            if (upstreamLife is not null)
            {
                try { await upstreamLife.DisposeAsync().ConfigureAwait(false); }
                catch { /* best-effort */ }
            }
            else if (tcp is not null)
            {
                // TcpClient.Dispose also closes the NetworkStream.
                try { tcp.Dispose(); }
                catch { /* best-effort */ }
            }
            else
            {
                try { upstream?.Dispose(); }
                catch { /* best-effort */ }
            }
        }
    }

    /// <summary>
    /// Dial RFB TCP for query <c>target_id</c>. Returns null after writing an HTTP error.
    /// </summary>
    private async Task<(TcpClient Tcp, Stream Stream)?> DialRfbUpstreamAsync(HttpContext ctx, Principal p)
    {
        var targetId = ctx.Request.Query["target_id"].ToString();
        if (string.IsNullOrWhiteSpace(targetId))
        {
            await DetailError(422, "target_id is required for vnc relay").ExecuteAsync(ctx).ConfigureAwait(false);
            return null;
        }

        if (!GraphicalTargetScope.TryForTenant(p.TenantId ?? string.Empty, out var scope))
        {
            await DetailError(403, "graphical target access denied").ExecuteAsync(ctx).ConfigureAwait(false);
            return null;
        }

        var target = await _deps.GraphicalTargets.GetAsync(scope, targetId);
        if (target is null)
        {
            await DetailError(404, "target not found").ExecuteAsync(ctx).ConfigureAwait(false);
            return null;
        }

        var protocol = target.Protocol.Trim().ToLowerInvariant();
        if (protocol == GraphicalTargetConstants.ProtocolMemory)
        {
            await DetailError(501, "graphical protocol not supported for vnc relay: memory")
                .ExecuteAsync(ctx).ConfigureAwait(false);
            return null;
        }

        if (protocol == GraphicalTargetConstants.ProtocolLitevirt)
        {
            await DetailError(501, "graphical protocol not supported: litevirt")
                .ExecuteAsync(ctx).ConfigureAwait(false);
            return null;
        }

        if (protocol != GraphicalTargetConstants.ProtocolRfb)
        {
            await DetailError(501, "graphical protocol not supported: " + protocol)
                .ExecuteAsync(ctx).ConfigureAwait(false);
            return null;
        }

        string rfbHost;
        int rfbPort;
        try
        {
            (rfbHost, rfbPort) = GraphicalTargetParsing.ParseRfbEndpoint(target.Endpoint);
        }
        catch (Exception ex)
        {
            await DetailError(422, ex.Message).ExecuteAsync(ctx).ConfigureAwait(false);
            return null;
        }

        try
        {
            var blockPrivate = _deps.Config.Security.BlockPrivateConnectorTargets;
            await EgressGuard.AssertConnectorTargetAllowedAsync(rfbHost, blockPrivate, ctx.RequestAborted)
                .ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            await DetailError(403, "invalid endpoint: " + ex.Message).ExecuteAsync(ctx).ConfigureAwait(false);
            return null;
        }

        TcpClient? tcp = null;
        try
        {
            tcp = new TcpClient();
            await tcp.ConnectAsync(rfbHost, rfbPort, ctx.RequestAborted).ConfigureAwait(false);
            var stream = tcp.GetStream();
            var owned = tcp;
            tcp = null; // ownership transferred to caller
            return (owned, stream);
        }
        catch (Exception ex)
        {
            try { tcp?.Dispose(); }
            catch { /* best-effort */ }

            await DetailError(502, "rfb connect failed: " + ex.Message).ExecuteAsync(ctx).ConfigureAwait(false);
            return null;
        }
    }

    private static async Task RunHumanVncRelayAsync(
        WebSocket ws,
        Stream upstream,
        string sessionId,
        string leaseId,
        string principalId,
        string principalRole,
        CancellationToken requestAborted)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(requestAborted);
        var token = linked.Token;
        RfbInputFilter.CanInject canInject =
            static (sid, lid, _, role) => HumanVncPolicy.CanInject(sid, lid, role) is null;

        using var clientSrc = new WsBinaryReadStream(ws, token);
        using var clientDst = new WsBinaryWriteStream(ws, token);

        try
        {
            await HumanRelay.RelayAsync(
                    clientSrc, upstream, upstream, clientDst,
                    canInject, sessionId, leaseId, principalId, principalRole, token)
                .ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Relay ends on filter/network errors; close WS below.
        }
        finally
        {
            try { linked.Cancel(); }
            catch (ObjectDisposedException) { /* ignore */ }

            if (ws.State is WebSocketState.Open or WebSocketState.CloseReceived)
            {
                try
                {
                    await ws.CloseAsync(
                            WebSocketCloseStatus.NormalClosure, "relay closed", CancellationToken.None)
                        .ConfigureAwait(false);
                }
                catch
                {
                    // best-effort
                }
            }
        }
    }

    private string ResolvePrincipalRole(Principal p, string workerId)
    {
        if (_deps.Registry.TryGetDefinition(workerId, out var def))
        {
            return _deps.Authz.ResolveBrowserRole(p, def);
        }

        if (_deps.Authz.IsAdmin(p) || p.Roles.Has("admin"))
        {
            return "admin";
        }

        return p.Roles.Has("operator") ? "operator" : "viewer";
    }
}
