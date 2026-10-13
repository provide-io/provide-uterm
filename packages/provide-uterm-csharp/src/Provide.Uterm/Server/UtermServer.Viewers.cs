//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;

namespace Provide.Uterm.Server;

/// <summary>
/// POST /api/sessions/{sessionId}/viewers/revoke (Python routes/viewers.py, Go
/// routes_viewers.go): record a share-viewer cutoff for the session and close
/// every live browser socket held by a share viewer it now refuses.
/// </summary>
public sealed partial class UtermServer
{
    /// <summary>
    /// Close code for a revoked share viewer: an application code (4000-4999) so
    /// a client can tell "access was taken away" from a drop or a policy refusal
    /// and stop reconnecting. 4410 echoes HTTP 410 Gone.
    /// </summary>
    internal const int ViewerRevokedCloseCode = 4410;

    /// <summary>Deliberately generic: the embedder knows why, and says so in its own words.</summary>
    internal const string ViewerRevokedReason = "viewer access revoked";

    private void MapViewerRoutes(WebApplication app) =>
        app.MapPost("/api/sessions/{sessionId}/viewers/revoke", (Delegate)HandleRevokeViewers);

    private async Task<IResult> HandleRevokeViewers(HttpContext ctx, string sessionId)
    {
        var (_, _, err) = await TryGatedSession(ctx, sessionId, "session.control.update").ConfigureAwait(false);
        if (err is not null) return err;
        var revokedAt = _deps.Authz.RevokeShareViewers(sessionId, _clock.Wall());
        var targets = _deps.Hub.Conn.BrowsersOf(sessionId)
            .Where(b => b.Role == "viewer"
                && b.Ws is BrowserWsConn conn
                && _deps.Authz.ShareViewerRevoked(conn.Principal, sessionId))
            .Select(b => (BrowserWsConn)b.Ws)
            .ToList();
        await Task.WhenAll(targets.Select(conn =>
            conn.CloseOutputAsync((WebSocketCloseStatus)ViewerRevokedCloseCode, ViewerRevokedReason))).ConfigureAwait(false);
        return Results.Json(new Dictionary<string, object?>
        {
            ["session_id"] = sessionId,
            ["revoked_at"] = revokedAt,
            ["closed"] = targets.Count,
            ["close_code"] = ViewerRevokedCloseCode,
            ["close_reason"] = ViewerRevokedReason,
        }, JsonOpts);
    }
}
