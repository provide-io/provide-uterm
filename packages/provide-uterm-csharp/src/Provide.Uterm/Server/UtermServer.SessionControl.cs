//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Server;

/// <summary>
/// Session lifecycle + read control plane routes (Go routes_sessions_control.go).
/// </summary>
public sealed partial class UtermServer
{
    private void MapSessionControlRoutes(WebApplication app)
    {
        app.MapPost("/api/sessions/{sessionId}/connect", (Delegate)HandleConnectSession);
        app.MapPost("/api/sessions/{sessionId}/disconnect", (Delegate)HandleDisconnectSession);
        app.MapPost("/api/sessions/{sessionId}/restart", (Delegate)HandleRestartSession);
        app.MapPost("/api/sessions/{sessionId}/mode", (Delegate)HandleSessionMode);
        app.MapPost("/api/sessions/{sessionId}/clear", (Delegate)HandleClearSession);
        app.MapPost("/api/sessions/{sessionId}/analyze", (Delegate)HandleAnalyzeSession);
        app.MapGet("/api/sessions/{sessionId}/snapshot", (Delegate)HandleSessionSnapshot);
        app.MapGet("/api/sessions/{sessionId}/events", (Delegate)HandleSessionEvents);
    }

    private async Task<(Principal? P, SessionDefinition? Def, IResult? Error)> TryGatedSession(
        HttpContext ctx, string sessionId, string cap)
    {
        if (!SafeId.IsMatch(sessionId))
        {
            return (null, null, DetailError(422, "invalid session_id"));
        }

        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Registry.TryGetDefinition(sessionId, out var def))
        {
            return (null, null, DetailError(404, "unknown session: " + sessionId));
        }

        if (!_deps.Authz.CanMutateSession(p, def, cap))
        {
            return (null, null, DetailError(403, "insufficient privileges"));
        }

        return (p, def, null);
    }

    private async Task<(Principal? P, IResult? Error)> TryReadableSession(
        HttpContext ctx, string sessionId)
    {
        if (!SafeId.IsMatch(sessionId))
        {
            return (null, DetailError(422, "invalid session_id"));
        }

        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Registry.TryGetDefinition(sessionId, out var def))
        {
            return (null, DetailError(404, "unknown session: " + sessionId));
        }

        if (!_deps.Authz.CanReadSession(p, def))
        {
            return (null, DetailError(403, "insufficient privileges"));
        }

        return (p, null);
    }

    private IResult StatusOrNotFound(string id, SessionStatus? st) =>
        st is null ? DetailError(404, "unknown session: " + id) : Results.Json(EnrichStatus(st), JsonOpts);

    private async Task<IResult> HandleConnectSession(HttpContext ctx, string sessionId)
    {
        var (_, _, err) = await TryGatedSession(ctx, sessionId, "session.control.connect").ConfigureAwait(false);
        if (err is not null) return err;
        var st = _deps.Registry.StartSession(sessionId);
        if (st is not null)
        {
            var hubSt = _deps.Hub.Registry.Get(sessionId);
            if (hubSt is not null) hubSt.InputMode = st.InputMode;
        }

        return StatusOrNotFound(sessionId, st);
    }

    private async Task<IResult> HandleDisconnectSession(HttpContext ctx, string sessionId)
    {
        var (_, _, err) = await TryGatedSession(ctx, sessionId, "session.control.connect").ConfigureAwait(false);
        if (err is not null) return err;
        return StatusOrNotFound(sessionId, _deps.Registry.StopSession(sessionId));
    }

    private async Task<IResult> HandleRestartSession(HttpContext ctx, string sessionId)
    {
        var (_, _, err) = await TryGatedSession(ctx, sessionId, "session.control.connect").ConfigureAwait(false);
        if (err is not null) return err;
        return StatusOrNotFound(sessionId, _deps.Registry.RestartSession(sessionId));
    }

    private async Task<IResult> HandleClearSession(HttpContext ctx, string sessionId)
    {
        var (_, _, err) = await TryGatedSession(ctx, sessionId, "session.control.clear").ConfigureAwait(false);
        if (err is not null) return err;
        return StatusOrNotFound(sessionId, _deps.Registry.ClearSession(sessionId));
    }

    private async Task<IResult> HandleSessionMode(HttpContext ctx, string sessionId)
    {
        var (_, _, err) = await TryGatedSession(ctx, sessionId, "session.control.mode").ConfigureAwait(false);
        if (err is not null) return err;
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var mode = Str(body, "input_mode").Trim();
        if (mode is not ("open" or "hijack"))
        {
            return DetailError(422, "input_mode must be 'open' or 'hijack'");
        }

        var st = _deps.Registry.SetMode(sessionId, mode);
        var hubSt = _deps.Hub.Registry.Get(sessionId);
        if (hubSt is not null) hubSt.InputMode = mode;
        return StatusOrNotFound(sessionId, st);
    }

    private async Task<IResult> HandleAnalyzeSession(HttpContext ctx, string sessionId)
    {
        var (_, err) = await TryReadableSession(ctx, sessionId).ConfigureAwait(false);
        if (err is not null) return err;
        if (!_deps.Registry.TryGetStatus(sessionId, out var st))
        {
            return DetailError(404, "unknown session: " + sessionId);
        }

        var snap = _deps.Hub.Router.GetLastSnapshot(sessionId);
        var events = _deps.Hub.Router.GetRecentEvents(sessionId, 20);
        var analysis = new Dictionary<string, object?>
        {
            ["lifecycle_state"] = st.LifecycleState,
            ["worker_online"] = st.WorkerOnline,
            ["is_hijacked"] = st.IsHijacked,
            ["input_mode"] = st.InputMode,
            ["has_snapshot"] = snap is not null,
            ["recent_event_count"] = events.Count,
        };
        return Results.Json(new { session_id = sessionId, analysis }, JsonOpts);
    }

    private async Task<IResult> HandleSessionSnapshot(HttpContext ctx, string sessionId)
    {
        var (_, err) = await TryReadableSession(ctx, sessionId).ConfigureAwait(false);
        if (err is not null) return err;
        var snap = _deps.Hub.Router.GetLastSnapshot(sessionId);
        return Results.Json(snap, JsonOpts);
    }

    private async Task<IResult> HandleSessionEvents(HttpContext ctx, string sessionId)
    {
        var (_, err) = await TryReadableSession(ctx, sessionId).ConfigureAwait(false);
        if (err is not null) return err;
        var limit = 100;
        if (int.TryParse(ctx.Request.Query["limit"], out var lim))
        {
            limit = Math.Clamp(lim, 1, 500);
        }

        var events = _deps.Hub.Router.GetRecentEvents(sessionId, limit);
        return Results.Json(events, JsonOpts);
    }
}
