//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.Fanout;

namespace Provide.Uterm.Server;

/// <summary>Fan-out group REST (Go routes_fanout.go).</summary>
public sealed partial class UtermServer
{
    private void MapFanoutRoutes(WebApplication app)
    {
        app.MapPost("/api/fanout/groups", (Delegate)HandleFanoutCreate);
        app.MapGet("/api/fanout/groups", (Delegate)HandleFanoutList);
        app.MapDelete("/api/fanout/groups/{groupId}", (Delegate)HandleFanoutDelete);
        app.MapPost("/api/fanout/groups/{groupId}/send", (Delegate)HandleFanoutSend);
        app.MapPost("/api/fanout/groups/{groupId}/grants", (Delegate)HandleFanoutGrant);
    }

    private Fanout.Controller EnsureFanout()
    {
        if (_deps.Fanout is not null) return _deps.Fanout;
        // Lazy default so factories without explicit wiring still work.
        return _lazyFanout ??= new Fanout.Controller(new HubFanoutAdapter(_deps.Hub), new ControllerConfig
        {
            Authorizer = new ServerFanoutAuthorizer(_deps.Registry, _deps.Authz),
        });
    }

    private Fanout.Controller? _lazyFanout;

    private sealed class ServerFanoutAuthorizer : IFanoutAuthorizer
    {
        private readonly ISessionRegistry _registry;
        private readonly Provide.Uterm.ServerAuth.AuthorizationService _authz;

        public ServerFanoutAuthorizer(ISessionRegistry registry, Provide.Uterm.ServerAuth.AuthorizationService authz)
        {
            _registry = registry;
            _authz = authz;
        }

        public bool IsGlobalAdmin(Provide.Uterm.ServerAuth.Principal principal) => _authz.IsAdmin(principal);

        public bool CanReadMember(Provide.Uterm.ServerAuth.Principal principal, string workerId) =>
            _registry.TryGetDefinition(workerId, out var definition) && _authz.CanReadSession(principal, definition);
    }

    private async Task<(Provide.Uterm.ServerAuth.Principal Principal, IResult? Error)> RequireFanoutAdmin(HttpContext ctx)
    {
        var (principal, error) = await RequireAuthenticated(ctx).ConfigureAwait(false);
        if (error is not null) return (principal, error);
        if (!_deps.Authz.IsAdmin(principal)) return (principal, BridgeError(403, "admin role required"));
        return (principal, null);
    }

    private sealed class HubFanoutAdapter : IFanoutHub
    {
        private readonly Hub.TermHub _hub;
        public HubFanoutAdapter(Hub.TermHub hub) => _hub = hub;

        public async Task<bool> SendWorkerAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            var dict = msg is Dictionary<string, object?> d
                ? d
                : msg.ToDictionary(kv => kv.Key, kv => kv.Value);
            var (ok, _) = await _hub.SendWorkerAsync(workerId, dict, ct).ConfigureAwait(false);
            return ok;
        }

        public Task BroadcastAsync(
            string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default)
        {
            var dict = msg is Dictionary<string, object?> d
                ? d
                : msg.ToDictionary(kv => kv.Key, kv => kv.Value);
            return _hub.Conn.BroadcastToBrowsersAsync(workerId, dict, ct);
        }

        public IFanoutOutputSubscription SubscribeOutput(string workerId)
        {
            var (subscription, unsubscribe) = _hub.EventBus.Watch(workerId, ["term", "snapshot"]);
            return new HubOutputSubscription(subscription, unsubscribe);
        }

        private sealed class HubOutputSubscription : IFanoutOutputSubscription
        {
            private readonly Hub.EventBus.Subscription _subscription;
            private readonly Action _unsubscribe;

            public HubOutputSubscription(Hub.EventBus.Subscription subscription, Action unsubscribe)
            {
                _subscription = subscription;
                _unsubscribe = unsubscribe;
            }

            public async ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct)
            {
                var item = await _subscription.Channel.Reader.ReadAsync(ct).ConfigureAwait(false);
                if (item is null) return null;
                var type = item.TryGetValue("type", out var typeValue) ? typeValue?.ToString() ?? "" : "";
                var text = "";
                if (item.TryGetValue("data", out var dataValue) && dataValue is Dictionary<string, object?> data)
                {
                    var key = type == "term" ? "data" : "screen";
                    if (data.TryGetValue(key, out var textValue)) text = textValue?.ToString() ?? "";
                }
                return new FanoutOutputEvent(type, text);
            }

            public ValueTask DisposeAsync()
            {
                _unsubscribe();
                return ValueTask.CompletedTask;
            }
        }
    }

    private static List<string> StringList(JsonElement el)
    {
        var outList = new List<string>();
        if (el.ValueKind != JsonValueKind.Array) return outList;
        foreach (var item in el.EnumerateArray())
        {
            if (item.ValueKind == JsonValueKind.String)
            {
                var s = item.GetString();
                if (!string.IsNullOrEmpty(s)) outList.Add(s);
            }
        }

        return outList;
    }

    private async Task<IResult> HandleFanoutCreate(HttpContext ctx)
    {
        var (p, authError) = await RequireFanoutAdmin(ctx).ConfigureAwait(false);
        if (authError is not null) return authError;
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var workerIds = body.TryGetValue("worker_ids", out var w) ? StringList(w) : new List<string>();
        var name = Str(body, "name");
        foreach (var wid in workerIds)
        {
            if (!_deps.Registry.TryGetDefinition(wid, out var def))
            {
                if (!_deps.Config.FanoutAllowUnknownMembers)
                {
                    return BridgeError(400, "unknown fan-out session: " + wid);
                }
                continue;
            }
            if (!_deps.Authz.CanReadSession(p, def))
            {
                return BridgeError(403, "forbidden: no read access to session " + wid);
            }
        }

        var threshold = 0.8;
        if (body.TryGetValue("divergence_threshold", out var th) && th.ValueKind == JsonValueKind.Number)
        {
            threshold = th.GetDouble();
        }

        var group = new Group
        {
            Name = name,
            WorkerIds = workerIds,
            Mode = string.IsNullOrEmpty(Str(body, "mode")) ? "parallel" : Str(body, "mode"),
            StopOnFirstError = body.TryGetValue("stop_on_first_error", out var so) &&
                               so.ValueKind == JsonValueKind.True,
            ErrorPattern = Str(body, "error_pattern"),
            QuiesceMs = Int(body, "quiesce_ms", 500),
            MaxResponseMs = Int(body, "max_response_ms", 10000),
            DivergenceThreshold = threshold,
        };
        try
        {
            var fanout = EnsureFanout();
            var groupId = fanout.CreateGroup(group, p.SubjectId);
            return Results.Json(new
            {
                group_id = groupId,
                name,
                session_count = workerIds.Count,
            }, JsonOpts);
        }
        catch (ArgumentException ex)
        {
            return BridgeError(400, ex.Message);
        }
    }

    private async Task<IResult> HandleFanoutList(HttpContext ctx)
    {
        var (p, authError) = await RequireFanoutAdmin(ctx).ConfigureAwait(false);
        if (authError is not null) return authError;
        var groups = EnsureFanout().ListGroups(p.SubjectId).Select(g => new
        {
            group_id = g.GroupId,
            name = g.Name,
            session_count = g.WorkerIds.Count,
            mode = g.Mode,
        }).ToList();
        return Results.Json(groups, JsonOpts);
    }

    private async Task<IResult> HandleFanoutDelete(HttpContext ctx, string groupId)
    {
        var (p, authError) = await RequireFanoutAdmin(ctx).ConfigureAwait(false);
        if (authError is not null) return authError;
        var fanout = EnsureFanout();
        var existing = fanout.GetGroup(groupId, p.SubjectId);
        if (existing is null) return BridgeError(404, "group not found");
        if (!string.Equals(existing.CreatedBy, p.SubjectId, StringComparison.Ordinal))
        {
            return BridgeError(403, "only the group creator can delete it");
        }

        fanout.DeleteGroup(groupId, p.SubjectId);
        return Results.NoContent();
    }

    private async Task<IResult> HandleFanoutSend(HttpContext ctx, string groupId)
    {
        var (p, authError) = await RequireFanoutAdmin(ctx).ConfigureAwait(false);
        if (authError is not null) return authError;
        var fanout = EnsureFanout();
        var group = fanout.GetGroup(groupId, p.SubjectId);
        if (group is null)
        {
            return BridgeError(404, "group not found");
        }

        if (!string.IsNullOrWhiteSpace(_deps.Config.Governance.PolicyWebhookUrl))
        {
            return BridgeError(501, "fanout governance is not supported by this server");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var data = Str(body, "data");
        var result = await fanout.SendAsync(
            groupId,
            data,
            p,
            Int(body, "quiesce_ms", 0),
            Int(body, "max_response_ms", 0),
            ctx.RequestAborted).ConfigureAwait(false);
        return Results.Json(new
        {
            group_id = result.GroupId,
            send_id = result.SendId,
            command = result.Command,
            sent_at = result.SentAt,
            results = result.ResultMaps(),
            divergent_sessions = result.DivergentSessions,
            failed_sessions = result.FailedSessions,
        }, JsonOpts);
    }

    private async Task<IResult> HandleFanoutGrant(HttpContext ctx, string groupId)
    {
        var (p, authError) = await RequireFanoutAdmin(ctx).ConfigureAwait(false);
        if (authError is not null) return authError;
        var fanout = EnsureFanout();
        var existing = fanout.GetGroup(groupId, p.SubjectId);
        if (existing is null) return BridgeError(404, "group not found");
        if (!string.Equals(existing.CreatedBy, p.SubjectId, StringComparison.Ordinal))
        {
            return BridgeError(403, "only the group creator can grant access");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var grantee = Str(body, "grantee");
        fanout.GrantAccess(groupId, grantee, p.SubjectId);
        return Results.NoContent();
    }
}
