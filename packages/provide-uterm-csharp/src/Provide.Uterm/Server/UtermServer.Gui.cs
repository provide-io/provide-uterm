//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.AspNetCore.Http;
using Provide.Uterm.Gui;
using Provide.Uterm.Hub;

namespace Provide.Uterm.Server;

/// <summary>GUI REST handlers — path-compatible with packages/provide-uterm-go/server/bridge_rest.go.</summary>
public sealed partial class UtermServer
{
    private async Task<IResult> HandleGuiAttach(HttpContext ctx, string workerId)
    {
        if (!SafeId.IsMatch(workerId)) return DetailError(422, "invalid worker_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizesGraphicalAttach(p, workerId, out var authErr)) return authErr!;

        var body = await ReadJson(ctx).ConfigureAwait(false);
        if (!body.TryGetValue("target_id", out var targetVal) || targetVal.ValueKind != JsonValueKind.String)
        {
            return DetailError(422, "target_id is required");
        }

        var targetId = targetVal.GetString() ?? "";
        if (!SafeId.IsMatch(targetId))
        {
            return DetailError(422, "invalid target_id");
        }

        var pTenant = PrincipalTenant(p);
        if (string.IsNullOrEmpty(pTenant))
        {
            return DetailError(403, "graphical target access denied");
        }

        if (!GraphicalTargetScope.TryForTenant(pTenant, out var scope))
        {
            return DetailError(403, "graphical target access denied");
        }

        var target = _deps.GraphicalTargets.Get(scope, targetId);
        if (target is null)
        {
            return DetailError(404, "graphical target not found");
        }

        var state = _deps.Hub.Registry.Get(workerId)
                    ?? _deps.Hub.Registry.SetDefault(workerId, new WorkerTermState());
        state.GraphicalSession = new MemoryGraphicalSession(640, 480);

        return Results.Json(new { ok = true }, JsonOpts);
    }

    private async Task<IResult> HandleGuiScreenshot(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.read", out err)) return err!;

        var hs = _deps.Hub.GetRestSession(workerId, hijackId);
        if (hs is null) return BridgeError(404, "Invalid or expired hijack session.");

        var gui = _deps.Hub.Registry.Get(workerId)?.GraphicalSession;
        if (gui is null) return BridgeError(404, "No graphical session attached.");

        var img = gui.Screenshot();
        var png = Png.EncodeRgba(img.Width, img.Height, img.Pixels);
        var leaseExpires = _clock.Wall() + (hs.LeaseExpiresAt - _clock.Monotonic());
        return Results.Json(new
        {
            ok = true,
            worker_id = workerId,
            hijack_id = hijackId,
            screenshot = Convert.ToBase64String(png),
            lease_expires_at = leaseExpires,
        }, JsonOpts);
    }

    private async Task<IResult> HandleGuiClick(HttpContext ctx, string workerId, string hijackId)
    {
        var (sess, early) = await RequireGraphicalSessionAsync(ctx, workerId, hijackId).ConfigureAwait(false);
        if (early is not null) return early;
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var x = Int(body, "x", 0);
        var y = Int(body, "y", 0);
        var button = Str(body, "button", "left");
        byte mask = button switch
        {
            "left" => 1,
            "middle" => 2,
            "right" => 4,
            _ => 1,
        };
        sess!.InjectPointer(x, y, mask);
        sess.InjectPointer(x, y, 0);
        return Results.Json(new { ok = true }, JsonOpts);
    }

    private async Task<IResult> HandleGuiType(HttpContext ctx, string workerId, string hijackId)
    {
        var (sess, early) = await RequireGraphicalSessionAsync(ctx, workerId, hijackId).ConfigureAwait(false);
        if (early is not null) return early;
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var text = Str(body, "text");
        foreach (var ch in text)
        {
            sess!.InjectKey((uint)ch, true);
            sess.InjectKey((uint)ch, false);
        }

        return Results.Json(new { ok = true }, JsonOpts);
    }

    private async Task<IResult> HandleGuiKey(HttpContext ctx, string workerId, string hijackId)
    {
        var (sess, early) = await RequireGraphicalSessionAsync(ctx, workerId, hijackId).ConfigureAwait(false);
        if (early is not null) return early;
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var keyName = Str(body, "key_name");
        uint sym = keyName switch
        {
            "Enter" => 0xFF0D,
            "Tab" => 0xFF09,
            "Esc" => 0xFF1B,
            "Backspace" => 0xFF08,
            "Up" => 0xFF52,
            "Down" => 0xFF54,
            "Left" => 0xFF51,
            "Right" => 0xFF53,
            _ => 0,
        };
        sess!.InjectKey(sym, true);
        sess.InjectKey(sym, false);
        return Results.Json(new { ok = true }, JsonOpts);
    }

    private async Task<IResult> HandleGuiDrag(HttpContext ctx, string workerId, string hijackId)
    {
        var (sess, early) = await RequireGraphicalSessionAsync(ctx, workerId, hijackId).ConfigureAwait(false);
        if (early is not null) return early;
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var startX = Int(body, "start_x", 0);
        var startY = Int(body, "start_y", 0);
        var endX = Int(body, "end_x", 0);
        var endY = Int(body, "end_y", 0);
        sess!.InjectPointer(startX, startY, 1);
        sess.InjectPointer(endX, endY, 1);
        sess.InjectPointer(endX, endY, 0);
        return Results.Json(new { ok = true }, JsonOpts);
    }

    private async Task<(IGraphicalSession? Session, IResult? Error)> RequireGraphicalSessionAsync(
        HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return (null, err);
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.control.hijack", out err)) return (null, err);

        if (_deps.Hub.GetRestSession(workerId, hijackId) is null)
        {
            return (null, BridgeError(404, "Invalid or expired hijack session."));
        }

        var gui = _deps.Hub.Registry.Get(workerId)?.GraphicalSession;
        if (gui is null)
        {
            return (null, BridgeError(404, "No graphical session attached."));
        }

        return (gui, null);
    }
}
