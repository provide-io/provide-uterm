//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;

namespace Provide.Uterm.Server;

/// <summary>Session webhook CRUD (Go routes_webhooks.go / Python webhooks).</summary>
public sealed partial class UtermServer
{
    private void MapWebhookRoutes(WebApplication app)
    {
        app.MapPost("/api/sessions/{sessionId}/webhooks", (Delegate)HandleRegisterWebhook);
        app.MapGet("/api/sessions/{sessionId}/webhooks", (Delegate)HandleListWebhooks);
        app.MapDelete("/api/sessions/{sessionId}/webhooks/{webhookId}", (Delegate)HandleUnregisterWebhook);
    }

    private async Task<IResult> HandleRegisterWebhook(HttpContext ctx, string sessionId)
    {
        if (!SafeId.IsMatch(sessionId)) return DetailError(422, "invalid session_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Registry.TryGetDefinition(sessionId, out var def))
        {
            return DetailError(404, "unknown session: " + sessionId);
        }

        if (!_deps.Authz.CanMutateSession(p, def, "session.control.update"))
        {
            return DetailError(403, "insufficient privileges");
        }

        var mgr = EnsureWebhooks();
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var url = Str(body, "url");
        if (string.IsNullOrEmpty(url))
        {
            return DetailError(422, "url is required");
        }

        List<string>? eventTypes = null;
        if (body.TryGetValue("event_types", out var et) && et.ValueKind == JsonValueKind.Array)
        {
            eventTypes = new List<string>();
            foreach (var item in et.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.String)
                {
                    var s = item.GetString();
                    if (!string.IsNullOrEmpty(s)) eventTypes.Add(s);
                }
            }
        }

        var pattern = Str(body, "pattern");
        var secret = Str(body, "secret");
        try
        {
            var cfg = mgr.Register(
                sessionId,
                url,
                eventTypes,
                string.IsNullOrEmpty(pattern) ? null : pattern,
                string.IsNullOrEmpty(secret) ? null : secret);
            return Results.Json(new
            {
                webhook_id = cfg.WebhookId,
                session_id = cfg.SessionId,
                url = cfg.Url,
                event_types = cfg.EventTypes,
                pattern = cfg.Pattern,
                secret = cfg.Secret,
            }, JsonOpts);
        }
        catch (ArgumentException ex)
        {
            return DetailError(422, ex.Message);
        }
    }

    private async Task<IResult> HandleListWebhooks(HttpContext ctx, string sessionId)
    {
        if (!SafeId.IsMatch(sessionId)) return DetailError(422, "invalid session_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Registry.TryGetDefinition(sessionId, out var def))
        {
            return DetailError(404, "unknown session: " + sessionId);
        }

        if (!_deps.Authz.CanMutateSession(p, def, "session.control.update"))
        {
            return DetailError(403, "insufficient privileges");
        }

        var list = EnsureWebhooks().ListWebhooks(sessionId).Select(w => new
        {
            webhook_id = w.WebhookId,
            session_id = w.SessionId,
            url = w.Url,
            event_types = w.EventTypes,
            pattern = w.Pattern,
        }).ToList();
        return Results.Json(new { webhooks = list }, JsonOpts);
    }

    private async Task<IResult> HandleUnregisterWebhook(HttpContext ctx, string sessionId, string webhookId)
    {
        if (!SafeId.IsMatch(sessionId)) return DetailError(422, "invalid session_id");
        if (!SafeId.IsMatch(webhookId)) return DetailError(422, "invalid webhook_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Registry.TryGetDefinition(sessionId, out var def))
        {
            return DetailError(404, "unknown session: " + sessionId);
        }

        if (!_deps.Authz.CanMutateSession(p, def, "session.control.update"))
        {
            return DetailError(403, "insufficient privileges");
        }

        var mgr = EnsureWebhooks();
        var cfg = mgr.GetWebhook(webhookId);
        if (cfg is null || !string.Equals(cfg.SessionId, sessionId, StringComparison.Ordinal))
        {
            return DetailError(404, "unknown webhook: " + webhookId);
        }

        mgr.Unregister(webhookId);
        return Results.Json(new { ok = true, webhook_id = webhookId }, JsonOpts);
    }

    private WebhookManager EnsureWebhooks() =>
        _deps.Webhooks ?? (_lazyWebhooks ??= new WebhookManager(allowLoopbackDestinations: true));

    private WebhookManager? _lazyWebhooks;
}
