//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Server;

/// <summary>
/// Remaining host REST parity with Go: profiles, API keys, approvals, metrics,
/// security-posture, events stream/watch, session patch/bulk delete, quick connect.
/// </summary>
public sealed partial class UtermServer
{
    private void MapHostRestRoutes(WebApplication app)
    {
        // Profiles
        app.MapGet("/api/profiles", (Delegate)HandleListProfiles);
        app.MapPost("/api/profiles", (Delegate)HandleCreateProfile);
        app.MapGet("/api/profiles/{profileId}", (Delegate)HandleGetProfile);
        app.MapPut("/api/profiles/{profileId}", (Delegate)HandleUpdateProfile);
        app.MapDelete("/api/profiles/{profileId}", (Delegate)HandleDeleteProfile);
        app.MapPost("/api/profiles/{profileId}/connect", (Delegate)HandleConnectProfile);

        // API keys
        app.MapPost("/api/keys", (Delegate)HandleCreateApiKey);
        app.MapGet("/api/keys", (Delegate)HandleListApiKeys);
        app.MapDelete("/api/keys/{keyId}", (Delegate)HandleRevokeApiKey);

        // Approvals
        app.MapGet("/api/approvals", (Delegate)HandleListApprovals);
        app.MapPost("/api/approvals/{requestId}/approve", (Delegate)HandleApprove);
        app.MapPost("/api/approvals/{requestId}/reject", (Delegate)HandleReject);

        // Metrics + posture
        app.MapGet("/api/metrics", (Delegate)HandleMetricsJson);
        app.MapGet("/api/metrics/prometheus", (Delegate)HandleMetricsPrometheus);
        app.MapGet("/api/security-posture", (Delegate)HandleSecurityPosture);

        // Session extras
        app.MapPatch("/api/sessions/{sessionId}", (Delegate)HandlePatchSession);
        app.MapDelete("/api/sessions", (Delegate)HandleBulkDeleteSessions);
        app.MapGet("/api/sessions/{sessionId}/events/stream", (Delegate)HandleEventStream);
        app.MapGet("/api/sessions/{sessionId}/events/watch", (Delegate)HandleWatchSessionEvents);
        app.MapPost("/api/connect", (Delegate)HandleQuickConnect);
    }

    private IProfileStore EnsureProfiles() =>
        _deps.Profiles ?? (_lazyProfiles ??= new InMemoryProfileStore());

    private IProfileStore? _lazyProfiles;

    private ServerMetrics EnsureMetrics() =>
        _deps.Metrics ?? (_lazyMetrics ??= new ServerMetrics());

    private ServerMetrics? _lazyMetrics;

    private ApiKeyStore EnsureApiKeys() =>
        _deps.ApiKeys ?? (_lazyApiKeys ??= new ApiKeyStore());

    private ApiKeyStore? _lazyApiKeys;

    private static bool CanReadProfile(AuthorizationService authz, Principal p, ConnectionProfile profile) =>
        authz.IsAdmin(p) || profile.Owner == p.SubjectId;

    private static bool CanMutateProfile(AuthorizationService authz, Principal p, ConnectionProfile profile) =>
        authz.IsAdmin(p) || profile.Owner == p.SubjectId;

    // ---- profiles ----------------------------------------------------------

    private async Task<IResult> HandleListProfiles(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        var store = EnsureProfiles();
        var list = _deps.Authz.IsAdmin(p)
            ? store.ListProfiles(null)
            : store.ListProfiles(p.SubjectId);
        return Results.Json(list.Select(ProfileDto), JsonOpts);
    }

    private async Task<IResult> HandleCreateProfile(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.CanCreateSession(p))
        {
            return DetailError(403, "insufficient privileges");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var now = _clock.Wall();
        var profile = new ConnectionProfile
        {
            ProfileId = "profile-" + Guid.NewGuid().ToString("N")[..12],
            Owner = p.SubjectId,
            Name = string.IsNullOrWhiteSpace(Str(body, "name")) ? "Unnamed" : Str(body, "name").Trim(),
            ConnectorType = string.IsNullOrWhiteSpace(Str(body, "connector_type"))
                ? "ssh"
                : Str(body, "connector_type"),
            Host = NullIfEmpty(Str(body, "host")),
            Port = body.TryGetValue("port", out var portEl) && portEl.ValueKind == JsonValueKind.Number
                ? portEl.GetInt32()
                : null,
            Username = NullIfEmpty(Str(body, "username")),
            InputMode = string.IsNullOrWhiteSpace(Str(body, "input_mode")) ? "open" : Str(body, "input_mode"),
            RecordingEnabled = body.TryGetValue("recording_enabled", out var re) && re.ValueKind == JsonValueKind.True,
            Visibility = string.IsNullOrWhiteSpace(Str(body, "visibility")) ? "private" : Str(body, "visibility"),
            CreatedAt = now,
            UpdatedAt = now,
        };
        if (body.TryGetValue("tags", out var tags) && tags.ValueKind == JsonValueKind.Array)
        {
            profile.Tags = StringList(tags);
        }

        var created = EnsureProfiles().CreateProfile(profile);
        EnsureMetrics().Inc("profiles_created_total");
        return Results.Json(ProfileDto(created), JsonOpts);
    }

    private async Task<IResult> HandleGetProfile(HttpContext ctx, string profileId)
    {
        if (!SafeId.IsMatch(profileId)) return DetailError(422, "invalid profile_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
        var profile = EnsureProfiles().GetProfile(profileId);
        if (profile is null) return DetailError(404, "unknown profile: " + profileId);
        if (!CanReadProfile(_deps.Authz, p, profile)) return DetailError(403, "insufficient privileges");
        return Results.Json(ProfileDto(profile), JsonOpts);
    }

    private async Task<IResult> HandleUpdateProfile(HttpContext ctx, string profileId)
    {
        if (!SafeId.IsMatch(profileId)) return DetailError(422, "invalid profile_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
        var existing = EnsureProfiles().GetProfile(profileId);
        if (existing is null) return DetailError(404, "unknown profile: " + profileId);
        if (!CanMutateProfile(_deps.Authz, p, existing)) return DetailError(403, "insufficient privileges");
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var updated = EnsureProfiles().UpdateProfile(profileId, prof =>
        {
            if (body.ContainsKey("name")) prof.Name = Str(body, "name", prof.Name);
            if (body.ContainsKey("host")) prof.Host = NullIfEmpty(Str(body, "host"));
            if (body.ContainsKey("username")) prof.Username = NullIfEmpty(Str(body, "username"));
            if (body.ContainsKey("connector_type")) prof.ConnectorType = Str(body, "connector_type", prof.ConnectorType);
            if (body.ContainsKey("visibility")) prof.Visibility = Str(body, "visibility", prof.Visibility);
            if (body.ContainsKey("input_mode")) prof.InputMode = Str(body, "input_mode", prof.InputMode);
            if (body.TryGetValue("port", out var portEl) && portEl.ValueKind == JsonValueKind.Number)
            {
                prof.Port = portEl.GetInt32();
            }

            if (body.TryGetValue("tags", out var tags) && tags.ValueKind == JsonValueKind.Array)
            {
                prof.Tags = StringList(tags);
            }
        });
        return updated is null
            ? DetailError(404, "unknown profile: " + profileId)
            : Results.Json(ProfileDto(updated), JsonOpts);
    }

    private async Task<IResult> HandleDeleteProfile(HttpContext ctx, string profileId)
    {
        if (!SafeId.IsMatch(profileId)) return DetailError(422, "invalid profile_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
        var existing = EnsureProfiles().GetProfile(profileId);
        if (existing is null) return DetailError(404, "unknown profile: " + profileId);
        if (!CanMutateProfile(_deps.Authz, p, existing)) return DetailError(403, "insufficient privileges");
        EnsureProfiles().DeleteProfile(profileId);
        return Results.Json(new { ok = true }, JsonOpts);
    }

    private async Task<IResult> HandleConnectProfile(HttpContext ctx, string profileId)
    {
        if (!SafeId.IsMatch(profileId)) return DetailError(422, "invalid profile_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
        var profile = EnsureProfiles().GetProfile(profileId);
        if (profile is null) return DetailError(404, "unknown profile: " + profileId);
        if (!CanReadProfile(_deps.Authz, p, profile) || !_deps.Authz.CanCreateSession(p))
        {
            return DetailError(403, "insufficient privileges");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        // Wire parity with Go handleConnectProfile: fold profile fields into connector_config.
        var connectorConfig = new Dictionary<string, object?>(StringComparer.Ordinal);
        if (!string.IsNullOrWhiteSpace(profile.Host)) connectorConfig["host"] = profile.Host;
        if (profile.Port is > 0) connectorConfig["port"] = profile.Port.Value;
        if (!string.IsNullOrWhiteSpace(profile.Username)) connectorConfig["username"] = profile.Username;
        var password = Str(body, "password");
        if (!string.IsNullOrEmpty(password))
        {
            connectorConfig["password"] = password; // pragma: allowlist secret
        }

        var sid = "from-profile-" + Guid.NewGuid().ToString("N")[..10];
        var def = new SessionDefinition
        {
            SessionId = sid,
            DisplayName = profile.Name,
            ConnectorType = profile.ConnectorType,
            Visibility = profile.Visibility,
            Owner = p.SubjectId,
            Tags = profile.Tags.ToList(),
            ConnectorConfig = connectorConfig,
        };
        _deps.Registry.Upsert(def);
        var st = await ActivateSessionAsync(sid, def, ctx.RequestAborted).ConfigureAwait(false);
        EnsureMetrics().Inc("profile_connect_total");
        return Results.Json(new
        {
            ok = true,
            session_id = sid,
            profile_id = profileId,
            connector_type = profile.ConnectorType,
            connector_config = connectorConfig,
            status = st is null ? null : EnrichStatus(st),
        }, JsonOpts);
    }

    private static object ProfileDto(ConnectionProfile p) => new
    {
        profile_id = p.ProfileId,
        owner = p.Owner,
        name = p.Name,
        connector_type = p.ConnectorType,
        host = p.Host,
        port = p.Port,
        username = p.Username,
        tags = p.Tags,
        input_mode = p.InputMode,
        recording_enabled = p.RecordingEnabled,
        visibility = p.Visibility,
        created_at = p.CreatedAt,
        updated_at = p.UpdatedAt,
    };

    // ---- API keys ----------------------------------------------------------

    private async Task<IResult> HandleCreateApiKey(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.IsAdmin(p)) return DetailError(403, "admin role required");
        if (!_deps.Config.Auth.ApiKeysEnabled)
        {
            return DetailError(403, "API key management is disabled");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var name = Str(body, "name").Trim();
        if (string.IsNullOrEmpty(name)) return DetailError(422, "name is required");
        if (!body.TryGetValue("scopes", out var scopesEl) || scopesEl.ValueKind != JsonValueKind.Array)
        {
            return DetailError(422, "scopes is required");
        }

        var scopes = new StringSet();
        var invalid = new List<string>();
        foreach (var item in scopesEl.EnumerateArray())
        {
            var sc = item.ValueKind == JsonValueKind.String ? (item.GetString() ?? "").Trim() : "";
            if (sc.Length == 0) continue;
            if (sc is not ("viewer" or "operator" or "admin"))
            {
                invalid.Add(sc);
                continue;
            }

            scopes.Add(sc);
        }

        if (scopes.Count == 0 && invalid.Count == 0)
        {
            return DetailError(422, "scopes must include at least one role scope");
        }

        if (invalid.Count > 0)
        {
            return DetailError(422, "invalid role scopes: " + string.Join(", ", invalid.OrderBy(s => s))
                + " (allowed: admin, operator, viewer)");
        }

        if (body.ContainsKey("tenant_id"))
        {
            return DetailError(422, "tenant_id is server-assigned and cannot be supplied");
        }

        int? expires = null;
        if (body.TryGetValue("expires_in_s", out var expEl) && expEl.ValueKind == JsonValueKind.Number)
        {
            var v = expEl.GetInt32();
            if (v < 60) return DetailError(422, "expires_in_s must be >= 60");
            expires = v;
        }

        var tenant = p.TenantId ?? "";
        var (raw, rec) = EnsureApiKeys().Create(name, scopes, expires, tenant);
        return Results.Json(new
        {
            key = raw,
            key_id = rec.KeyId,
            name = rec.Name,
            tenant_id = rec.TenantId,
            scopes = rec.Scopes.ToList(),
            created_at = rec.CreatedAt,
            expires_at = rec.ExpiresAt,
        }, JsonOpts);
    }

    private async Task<IResult> HandleListApiKeys(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.IsAdmin(p)) return DetailError(403, "admin role required");
        if (!_deps.Config.Auth.ApiKeysEnabled) return DetailError(403, "API key management is disabled");
        var keys = string.IsNullOrEmpty(p.TenantId)
            ? EnsureApiKeys().ListKeys()
            : EnsureApiKeys().ListKeysForTenant(p.TenantId!);
        return Results.Json(keys.Select(k => new
        {
            key_id = k.KeyId,
            name = k.Name,
            tenant_id = k.TenantId,
            scopes = k.Scopes.ToList(),
            created_at = k.CreatedAt,
            expires_at = k.ExpiresAt,
            revoked = k.Revoked,
        }), JsonOpts);
    }

    private async Task<IResult> HandleRevokeApiKey(HttpContext ctx, string keyId)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.IsAdmin(p)) return DetailError(403, "admin role required");
        if (!_deps.Config.Auth.ApiKeysEnabled) return DetailError(403, "API key management is disabled");
        if (!SafeId.IsMatch(keyId)) return DetailError(422, "invalid key_id");
        var ok = string.IsNullOrEmpty(p.TenantId)
            ? EnsureApiKeys().Revoke(keyId)
            : EnsureApiKeys().RevokeForTenant(keyId, p.TenantId!);
        if (!ok) return DetailError(404, "unknown key: " + keyId);
        return Results.Json(new { ok = true, key_id = keyId }, JsonOpts);
    }

    // ---- Approvals ---------------------------------------------------------

    private async Task<IResult> HandleListApprovals(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.IsAdmin(p)) return DetailError(403, "Admin role required");
        var pending = _deps.Hub.Approvals.PendingApprovals();
        return Results.Json(pending.Select(r => new
        {
            id = r.Id,
            worker_id = r.WorkerId,
            submitter_id = r.SubmitterId,
            command = r.Command,
            status = r.Status.ToString().ToLowerInvariant(),
            created_at = r.CreatedAt,
            expires_at = r.ExpiresAt,
        }), JsonOpts);
    }

    private async Task<IResult> HandleApprove(HttpContext ctx, string requestId)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.IsAdmin(p)) return DetailError(403, "Admin role required");
        var req = _deps.Hub.Approvals.Get(requestId);
        if (req is null) return DetailError(404, "Approval request not found");
        if (req.SubmitterId == p.SubjectId) return DetailError(403, "Cannot approve your own command");
        if (!_deps.Hub.Approvals.Claim(requestId, Hub.ApprovalStatus.Approved))
        {
            return DetailError(400, "Approval request is not pending");
        }

        return Results.Json(new { status = "approved" }, JsonOpts);
    }

    private async Task<IResult> HandleReject(HttpContext ctx, string requestId)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.IsAdmin(p)) return DetailError(403, "Admin role required");
        var req = _deps.Hub.Approvals.Get(requestId);
        if (req is null) return DetailError(404, "Approval request not found");
        if (!_deps.Hub.Approvals.Claim(requestId, Hub.ApprovalStatus.Rejected))
        {
            return DetailError(400, "Approval request is not pending");
        }

        return Results.Json(new { status = "rejected" }, JsonOpts);
    }

    // ---- Metrics / posture -------------------------------------------------

    private async Task<IResult> HandleMetricsJson(HttpContext ctx)
    {
        if (_deps.Config.Security.MetricsRequireAuth)
        {
            var p = await Authenticate(ctx).ConfigureAwait(false);
            if (string.IsNullOrEmpty(p.SubjectId) || p.SubjectId == "anonymous")
            {
                return DetailError(401, "authentication required for /metrics");
            }
        }

        return Results.Json(new { metrics = EnsureMetrics().Snapshot() }, JsonOpts);
    }

    private async Task<IResult> HandleMetricsPrometheus(HttpContext ctx)
    {
        if (_deps.Config.Security.MetricsRequireAuth)
        {
            var p = await Authenticate(ctx).ConfigureAwait(false);
            if (string.IsNullOrEmpty(p.SubjectId) || p.SubjectId == "anonymous")
            {
                return DetailError(401, "authentication required for /metrics");
            }
        }

        return Results.Text(EnsureMetrics().Prometheus(), "text/plain; version=0.0.4; charset=utf-8");
    }

    private async Task<IResult> HandleSecurityPosture(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        var secure = _deps.Config.Security.Mode == "strict";
        var posture = new Dictionary<string, object?>
        {
            ["environment"] = _deps.Config.Environment ?? "development",
            ["secure"] = secure,
        };
        if (_deps.Authz.IsAdmin(p) || _deps.Authz.HasRole(p, "operator"))
        {
            posture["mode"] = _deps.Config.Security.Mode;
            posture["auth_mode"] = _deps.Config.Auth.Mode;
            posture["metrics_require_auth"] = _deps.Config.Security.MetricsRequireAuth;
            posture["block_private_connector_targets"] = _deps.Config.Security.BlockPrivateConnectorTargets;
        }

        return Results.Json(posture, JsonOpts);
    }

    // ---- Session patch / bulk / connect / events ---------------------------

    private async Task<IResult> HandlePatchSession(HttpContext ctx, string sessionId)
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

        var body = await ReadJson(ctx).ConfigureAwait(false);
        string? display = body.ContainsKey("display_name") ? Str(body, "display_name") : null;
        string? vis = body.ContainsKey("visibility") ? Str(body, "visibility") : null;
        List<string>? tags = null;
        if (body.TryGetValue("tags", out var tagsEl) && tagsEl.ValueKind == JsonValueKind.Array)
        {
            tags = StringList(tagsEl);
        }

        var st = _deps.Registry.PatchSession(sessionId, display, vis, tags);
        return st is null
            ? DetailError(404, "unknown session: " + sessionId)
            : Results.Json(EnrichStatus(st), JsonOpts);
    }

    private async Task<IResult> HandleBulkDeleteSessions(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.IsAdmin(p)) return DetailError(403, "insufficient privileges");
        var state = ctx.Request.Query["lifecycle_state"].ToString();
        if (string.IsNullOrEmpty(state)) state = null;
        var n = _deps.Registry.BulkDelete(state);
        return Results.Json(new { ok = true, deleted = n }, JsonOpts);
    }

    private async Task HandleEventStream(HttpContext ctx, string sessionId)
    {
        var (p, err) = await TryReadableSession(ctx, sessionId).ConfigureAwait(false);
        if (err is not null)
        {
            await err.ExecuteAsync(ctx).ConfigureAwait(false);
            return;
        }

        _ = p;
        ctx.Response.Headers.ContentType = "text/event-stream";
        ctx.Response.Headers.CacheControl = "no-cache";
        ctx.Response.Headers["X-Accel-Buffering"] = "no";
        await ctx.Response.StartAsync().ConfigureAwait(false);

        IReadOnlyCollection<string>? eventTypes = null;
        var et = ctx.Request.Query["event_types"].ToString();
        if (!string.IsNullOrWhiteSpace(et))
        {
            eventTypes = et.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        }

        string? pattern = NullIfEmpty(ctx.Request.Query["pattern"].ToString());
        var (sub, unsub) = _deps.Hub.EventBus.Watch(sessionId, eventTypes, pattern);
        try
        {
            // Snapshot recent ring buffer first (matches Python stream bootstrap).
            foreach (var evt in _deps.Hub.Router.GetRecentEvents(sessionId, 50))
            {
                var json = JsonSerializer.Serialize(evt, JsonOpts);
                await ctx.Response.WriteAsync("data: " + json + "\n\n", ctx.RequestAborted).ConfigureAwait(false);
                await ctx.Response.Body.FlushAsync(ctx.RequestAborted).ConfigureAwait(false);
            }

            // Immediate heartbeat so clients/tests see a first chunk without waiting 15s.
            await ctx.Response.WriteAsync("data: {\"type\":\"heartbeat\"}\n\n", ctx.RequestAborted)
                .ConfigureAwait(false);
            await ctx.Response.Body.FlushAsync(ctx.RequestAborted).ConfigureAwait(false);

            // UTERM_TEST_MODE shortens heartbeats so live tests can hit the timer arm.
            var beat = string.Equals(
                Environment.GetEnvironmentVariable("UTERM_TEST_MODE"), "1", StringComparison.Ordinal)
                ? TimeSpan.FromMilliseconds(80)
                : TimeSpan.FromSeconds(15);
            while (!ctx.RequestAborted.IsCancellationRequested)
            {
                using var beatCts = CancellationTokenSource.CreateLinkedTokenSource(ctx.RequestAborted);
                beatCts.CancelAfter(beat);
                try
                {
                    while (await sub.Channel.Reader.WaitToReadAsync(beatCts.Token).ConfigureAwait(false))
                    {
                        while (sub.Channel.Reader.TryRead(out var item))
                        {
                            if (item is null)
                            {
                                await ctx.Response.WriteAsync(
                                    "data: {\"type\":\"worker_disconnected\"}\n\n",
                                    ctx.RequestAborted).ConfigureAwait(false);
                                await ctx.Response.Body.FlushAsync(ctx.RequestAborted).ConfigureAwait(false);
                                return;
                            }

                            var json = JsonSerializer.Serialize(item, JsonOpts);
                            await ctx.Response.WriteAsync("data: " + json + "\n\n", ctx.RequestAborted)
                                .ConfigureAwait(false);
                            await ctx.Response.Body.FlushAsync(ctx.RequestAborted).ConfigureAwait(false);
                        }
                    }
                }
                catch (OperationCanceledException) when (!ctx.RequestAborted.IsCancellationRequested)
                {
                    await ctx.Response.WriteAsync("data: {\"type\":\"heartbeat\"}\n\n", ctx.RequestAborted)
                        .ConfigureAwait(false);
                    await ctx.Response.Body.FlushAsync(ctx.RequestAborted).ConfigureAwait(false);
                }
            }
        }
        catch (OperationCanceledException)
        {
            // client gone
        }
        finally
        {
            unsub();
        }
    }

    private async Task<IResult> HandleWatchSessionEvents(HttpContext ctx, string sessionId)
    {
        var (p, err) = await TryReadableSession(ctx, sessionId).ConfigureAwait(false);
        if (err is not null) return err;
        _ = p;
        var timeoutMs = 5000;
        if (int.TryParse(ctx.Request.Query["timeout_ms"], out var tms))
        {
            timeoutMs = Math.Clamp(tms, 100, 30000);
        }

        var maxEvents = 50;
        if (int.TryParse(ctx.Request.Query["max_events"], out var me))
        {
            maxEvents = Math.Clamp(me, 1, 200);
        }

        IReadOnlyCollection<string>? eventTypes = null;
        var et = ctx.Request.Query["event_types"].ToString();
        if (!string.IsNullOrWhiteSpace(et))
        {
            eventTypes = et.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        }

        string? pattern = NullIfEmpty(ctx.Request.Query["pattern"].ToString());

        // Real long-poll on EventBus (Python watch_session_events / Go shape).
        var result = await _deps.Hub.EventBus.WatchAsync(
            sessionId,
            TimeSpan.FromMilliseconds(timeoutMs),
            maxEvents,
            eventTypes,
            pattern,
            ctx.RequestAborted).ConfigureAwait(false);

        return Results.Json(new
        {
            session_id = sessionId,
            events = result.Events,
            dropped_count = result.DroppedCount,
            timed_out = result.TimedOut,
        }, JsonOpts);
    }

    private async Task<IResult> HandleQuickConnect(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Authz.CanCreateSession(p))
        {
            return DetailError(403, "insufficient privileges");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var connectorType = string.IsNullOrWhiteSpace(Str(body, "connector_type"))
            ? "ssh"
            : Str(body, "connector_type").Trim();
        var displayName = string.IsNullOrWhiteSpace(Str(body, "display_name"))
            ? connectorType
            : Str(body, "display_name");
        var sid = "connect-" + Guid.NewGuid().ToString("N")[..12];
        var connectorConfig = ExtractConnectorConfig(body);
        var def = new SessionDefinition
        {
            SessionId = sid,
            DisplayName = displayName,
            ConnectorType = connectorType,
            Visibility = "private",
            Owner = p.SubjectId,
            ConnectorConfig = connectorConfig,
        };
        _deps.Registry.Upsert(def);
        var st = await ActivateSessionAsync(sid, def, ctx.RequestAborted).ConfigureAwait(false);
        EnsureMetrics().Inc("quick_connect_total");
        return Results.Json(new
        {
            ok = true,
            session_id = sid,
            connector_type = connectorType,
            connector_config = connectorConfig,
            status = st is null ? null : EnrichStatus(st),
        }, JsonOpts);
    }

    private static string? NullIfEmpty(string s) => string.IsNullOrWhiteSpace(s) ? null : s.Trim();
}
