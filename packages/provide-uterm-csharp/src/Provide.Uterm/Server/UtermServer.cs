//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.DeckMux;
using Provide.Uterm.Hub;
using Provide.Uterm.Recording;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Tunnel;

namespace Provide.Uterm.Server;

/// <summary>Dependency bundle for constructing a <see cref="UtermServer"/>.</summary>
public sealed class ServerDeps
{
    public required TermHub Hub { get; init; }
    public required IAuthenticator Auth { get; init; }
    public required AuthorizationService Authz { get; init; }
    public required UtermServerConfig Config { get; init; }
    public required ISessionRegistry Registry { get; init; }
    public IGraphicalTargetRegistry GraphicalTargets { get; init; } = new InMemoryGraphicalTargetRegistry();
    public string Version { get; init; } = "0.0.0-dev";
    public IClock? Clock { get; init; }
    /// <summary>Backs /api/sessions/{id}/recording routes. Defaults to <see cref="NullStore"/>.</summary>
    public IRecordingStore? Recording { get; init; }
    /// <summary>Session webhooks (register/list/delete). Default constructed when null.</summary>
    public WebhookManager? Webhooks { get; init; }
    /// <summary>Fan-out controller. Lazy-built against Hub when null.</summary>
    public Fanout.Controller? Fanout { get; init; }
    /// <summary>Tunnel token/invite store for /api/tunnels host lifecycle.</summary>
    public Tunnel.MemoryTunnelStore? TunnelStore { get; init; }
    /// <summary>Connection profiles store.</summary>
    public IProfileStore? Profiles { get; init; }
    /// <summary>Server metrics counters.</summary>
    public ServerMetrics? Metrics { get; init; }
    /// <summary>API key registry (admin /api/keys).</summary>
    public ApiKeyStore? ApiKeys { get; init; }
    /// <summary>Optional path to built frontend assets (SPA hosting).</summary>
    public string? FrontendDir { get; init; }
}

/// <summary>
/// ASP.NET Core Minimal API host for health, sessions, hijack REST, and browser WS.
/// </summary>
public sealed partial class UtermServer : IAsyncDisposable
{
    private static readonly Regex SafeId = new(@"^[A-Za-z0-9._-]+$", RegexOptions.Compiled);
    private static readonly Regex HijackIdPattern = new(@"^[A-Za-z0-9._-]{1,128}$", RegexOptions.Compiled);
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly ServerDeps _deps;
    private readonly IClock _clock;
    private readonly IRecordingStore _recording;
    private readonly double _startTime;
    private volatile bool _ready;
    private WebApplication? _app;
    private Task? _runTask;

    // In-process resume tokens (Python ControlPlaneResumeStore / Go InMemoryResumeStore).
    private readonly ConcurrentDictionary<string, (string WorkerId, string Role, double ExpiresAt)> _resumeTokens = new();
    private readonly DeckMuxPresence _deckMux;

    public UtermServer(ServerDeps deps)
    {
        _deps = deps;
        _clock = deps.Clock ?? new RealClock();
        _recording = deps.Recording ?? new NullStore();
        _startTime = _clock.Wall();
        _deckMux = new DeckMuxPresence(new HubDeckMuxBroadcaster(_deps.Hub));
    }

    private sealed class HubDeckMuxBroadcaster : IDeckMuxBroadcaster
    {
        private readonly TermHub _hub;
        public HubDeckMuxBroadcaster(TermHub hub) => _hub = hub;
        public Task BroadcastAsync(string workerId, Dictionary<string, object?> msg, CancellationToken ct = default) =>
            _hub.Conn.BroadcastToBrowsersAsync(workerId, msg, ct);
    }

    private string MintResumeToken(string workerId, string role)
    {
        var tok = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        _resumeTokens[tok] = (workerId, role, _clock.Monotonic() + 300);
        return tok;
    }

    /// <summary>The graphical-target registry this server was built with.
    /// Exposed for tests that assert which backend the factory selected.</summary>
    internal IGraphicalTargetRegistry GraphicalTargets => _deps.GraphicalTargets;

    private void MapRoutes(WebApplication app)
    {
        app.MapGet("/api/health", HandleHealth);
        app.MapGet("/healthz", () => Results.Json(new { status = "ok" }));
        app.MapGet("/readyz", () => _ready
            ? Results.Json(new { status = "ready" })
            : Results.Json(new { status = "not_ready" }, statusCode: 503));

        app.MapGet("/api/sessions", async (HttpContext ctx) =>
        {
            var (p, err) = await RequireAuthenticated(ctx).ConfigureAwait(false);
            if (err is not null) return err;
            var items = _deps.Registry.ListWithDefinitions()
                .Where(it => it.Definition is not null && it.Status is not null && _deps.Authz.CanReadSession(p, it.Definition!))
                .Select(it => EnrichStatus(it.Status!))
                .ToList();
            return Results.Json(items, JsonOpts);
        });

        app.MapGet("/api/sessions/{sessionId}", async (HttpContext ctx, string sessionId) =>
        {
            var (p, err) = await RequireAuthenticated(ctx).ConfigureAwait(false);
            if (err is not null) return err;
            if (!_deps.Registry.TryGetDefinition(sessionId, out var def))
            {
                return DetailError(404, "unknown session: " + sessionId);
            }

            if (!_deps.Authz.CanReadSession(p, def))
            {
                return DetailError(403, "insufficient privileges");
            }

            var item = _deps.Registry.ListWithDefinitions().FirstOrDefault(i => i.Definition?.SessionId == sessionId);
            return Results.Json(EnrichStatus(item?.Status ?? new SessionStatus
            {
                SessionId = def.SessionId,
                DisplayName = def.DisplayName,
                ConnectorType = def.ConnectorType,
                Visibility = def.Visibility,
                Owner = def.Owner,
            }), JsonOpts);
        });

        app.MapPost("/api/sessions", async (HttpContext ctx) =>
        {
            var p = await Authenticate(ctx).ConfigureAwait(false);
            if (!_deps.Authz.CanCreateSession(p))
            {
                return DetailError(403, "insufficient privileges");
            }

            var body = await ReadJson(ctx).ConfigureAwait(false);
            var id = Str(body, "session_id");
            if (string.IsNullOrEmpty(id)) id = "sess-" + Guid.NewGuid().ToString("N")[..12];
            if (!SafeId.IsMatch(id)) return DetailError(422, "invalid session_id");
            var def = new SessionDefinition
            {
                SessionId = id,
                DisplayName = Str(body, "display_name", id),
                ConnectorType = Str(body, "connector_type", "shell"),
                Visibility = Str(body, "visibility", _deps.Config.Security.DefaultSessionVisibility),
                Owner = p.SubjectId,
            };
            _deps.Registry.Upsert(def);
            return Results.Json(new { ok = true, session_id = id }, JsonOpts);
        });

        app.MapDelete("/api/sessions/{sessionId}", async (HttpContext ctx, string sessionId) =>
        {
            var p = await Authenticate(ctx).ConfigureAwait(false);
            if (!_deps.Registry.TryGetDefinition(sessionId, out var def))
            {
                return DetailError(404, "unknown session: " + sessionId);
            }

            if (!_deps.Authz.CanMutateSession(p, def, "session.control.delete"))
            {
                return DetailError(403, "insufficient privileges");
            }

            _deps.Registry.Delete(sessionId);
            return Results.Json(new { ok = true }, JsonOpts);
        });

        // Thin recording surface (Python/Go parity): annotate + meta/entries/download
        app.MapPost("/api/sessions/{sessionId}/annotate", HandleAnnotateSession);
        app.MapGet("/api/sessions/{sessionId}/recording", HandleRecordingMeta);
        app.MapGet("/api/sessions/{sessionId}/recording/entries", HandleRecordingEntries);
        app.MapGet("/api/sessions/{sessionId}/recording/download", HandleRecordingDownload);

        // Session lifecycle control plane (Go routes_sessions_control)
        MapSessionControlRoutes(app);
        // Session webhooks (Go routes_webhooks)
        MapWebhookRoutes(app);
        // Fan-out groups (Go routes_fanout)
        MapFanoutRoutes(app);
        // Host REST residual: profiles, keys, approvals, metrics, posture, sessions extras
        MapHostRestRoutes(app);
        // SPA / static UI (Python frontend-dir parity)
        MapStaticUi(app);

        // Hijack REST surface
        app.MapPost("/worker/{workerId}/hijack/acquire", HandleHijackAcquire);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/heartbeat", HandleHijackHeartbeat);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/send", HandleHijackSend);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/step", HandleHijackStep);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/release", HandleHijackRelease);
        app.MapGet("/worker/{workerId}/hijack/{hijackId}/snapshot", HandleHijackSnapshot);
        app.MapGet("/worker/{workerId}/hijack/{hijackId}/events", HandleHijackEvents);
        app.MapPost("/worker/{workerId}/input_mode", HandleInputMode);
        app.MapPost("/worker/{workerId}/disconnect_worker", HandleDisconnectWorker);

        // GUI REST (Go-compatible paths; memory attach for deterministic fixtures)
        app.MapPost("/worker/{workerId}/gui/attach", HandleGuiAttach);
        app.MapGet("/worker/{workerId}/hijack/{hijackId}/gui/screenshot", HandleGuiScreenshot);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/gui/click", HandleGuiClick);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/gui/type", HandleGuiType);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/gui/key", HandleGuiKey);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/gui/drag", HandleGuiDrag);
        // Human VNC WebSocket relay (RFB input filter + lease policy)
        MapHumanVncRoutes(app);

        app.MapGet("/api/graphical-targets", (Delegate)HandleListGraphicalTargets);
        app.MapGet("/api/graphical-targets/{targetId}", HandleGetGraphicalTarget);
        app.MapPost("/api/graphical-targets", (Delegate)HandleCreateGraphicalTarget);
        app.MapPut("/api/graphical-targets/{targetId}", HandleUpdateGraphicalTarget);
        app.MapDelete("/api/graphical-targets/{targetId}", HandleDeleteGraphicalTarget);
        
        MapMcpRoutes(app);

        // Browser / worker WebSockets with DLE/STX control channel
        // Path shape matches Python/Go: terminal channel is /term on the worker id.
        app.Map("/ws/browser/{workerId}/term", HandleBrowserWs);
        app.Map("/ws/worker/{workerId}/term", HandleWorkerWs);
        // Binary tunnel + inspect page (Python register_tunnel_routes + inspect_page_html).
        MapTunnelRoutes(app);
    }

    private SessionStatus EnrichStatus(SessionStatus st)
    {
        var hubSt = _deps.Hub.Registry.Get(st.SessionId);
        if (hubSt is not null)
        {
            // Keep registry WorkerOnline for REST-activated shell sessions without a WS.
            st.Connected = st.Connected || hubSt.WorkerWs is not null;
            st.IsHijacked = _deps.Hub.State.IsHijacked(hubSt);
            st.InputMode = hubSt.InputMode;
        }

        if (_deps.Registry.TryGetDefinition(st.SessionId, out var def) && def.ConnectorConfig.Count > 0)
        {
            st.ConnectorConfig = new Dictionary<string, object?>(def.ConnectorConfig);
        }

        return st;
    }

    private static Dictionary<string, object?> ExtractConnectorConfig(Dictionary<string, JsonElement> body)
    {
        var cfg = new Dictionary<string, object?>(StringComparer.Ordinal);
        foreach (var key in new[] { "host", "username", "password", "shell", "url", "command" })
        {
            if (body.TryGetValue(key, out var el) && el.ValueKind == JsonValueKind.String)
            {
                var s = el.GetString();
                if (!string.IsNullOrEmpty(s)) cfg[key] = s;
            }
        }

        if (body.TryGetValue("port", out var portEl) && portEl.ValueKind == JsonValueKind.Number)
        {
            cfg["port"] = portEl.GetInt32();
        }

        // Nested connector_config object (Go-shaped clients)
        if (body.TryGetValue("connector_config", out var nested) && nested.ValueKind == JsonValueKind.Object)
        {
            foreach (var prop in nested.EnumerateObject())
            {
                cfg[prop.Name] = prop.Value.ValueKind switch
                {
                    JsonValueKind.String => prop.Value.GetString(),
                    JsonValueKind.Number => prop.Value.TryGetInt32(out var i) ? i : prop.Value.GetDouble(),
                    JsonValueKind.True => true,
                    JsonValueKind.False => false,
                    _ => prop.Value.ToString(),
                };
            }
        }

        return cfg;
    }

    private IResult HandleHealth()
    {
        if (!_ready)
        {
            return Results.Json(new
            {
                status = "starting",
                ok = false,
                ready = false,
                service = "uterm-server",
            }, statusCode: 503);
        }

        var backend = string.IsNullOrEmpty(_deps.Config.ControlPlane.Backend)
            ? "memory"
            : _deps.Config.ControlPlane.Backend;
        var uptime = Math.Round((_clock.Wall() - _startTime) * 100) / 100;
        var active = _deps.Registry.ListWithDefinitions().Count;
        return Results.Json(new
        {
            status = "ok",
            ok = true,
            ready = true,
            service = "uterm-server",
            version = _deps.Version,
            uptime_s = uptime,
            active_sessions = active,
            control_plane_backend = backend,
        });
    }

    private async Task<IResult> HandleHijackAcquire(HttpContext ctx, string workerId)
    {
        if (!SafeId.IsMatch(workerId)) return DetailError(422, "invalid worker_id");
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        var clientId = ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown";
        if (!_deps.Hub.AllowRestAcquireFor(clientId))
        {
            _deps.Hub.Metric("rest_acquire_rate_limited_total", 1);
            return BridgeError(429, "rate_limited");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var owner = Str(body, "owner", "operator");
        if (string.IsNullOrWhiteSpace(owner)) owner = "operator";
        var leaseS = StateStore.ClampLease(Int(body, "lease_s", 90));
        _deps.Hub.CleanupExpiredHijack(workerId);

        var hijackId = NewHijackId();
        var wallNow = _clock.Wall();
        var monoNow = _clock.Monotonic();
        var (ok, reason) = await _deps.Hub.TryAcquireRestHijackAsync(workerId, owner, leaseS, hijackId, monoNow, ctx.RequestAborted)
            .ConfigureAwait(false);
        if (!ok)
        {
            return BridgeError(409, AcquireErrorMessage(reason));
        }

        _deps.Hub.Metric("hijack_acquires_total", 1);
        _deps.Hub.NotifyHijackChanged(workerId, true, owner);
        _deps.Hub.AppendEventData(workerId, "hijack_acquired", new Dictionary<string, object?>
        {
            ["hijack_id"] = hijackId,
            ["owner"] = owner,
            ["lease_s"] = leaseS,
        });
        await _deps.Hub.BroadcastHijackStateAsync(workerId, ctx.RequestAborted).ConfigureAwait(false);

        var session = _deps.Hub.GetRestSession(workerId, hijackId);
        if (session is not null)
        {
            session.AcquiredBy = p.SubjectId;
        }

        return Results.Json(new
        {
            ok = true,
            worker_id = workerId,
            hijack_id = hijackId,
            lease_expires_at = wallNow + leaseS,
            owner,
        }, JsonOpts);
    }

    private async Task<IResult> HandleHijackHeartbeat(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        var hs = _deps.Hub.GetRestSession(workerId, hijackId);
        if (hs is null) return BridgeError(404, "Invalid or expired hijack session.");

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var leaseS = StateStore.ClampLease(Int(body, "lease_s", 90));
        var newExpires = _deps.Hub.ExtendHijackLease(workerId, hijackId, hs.Owner, leaseS, _clock.Monotonic());
        if (newExpires is null) return BridgeError(404, "Invalid or expired hijack session.");

        _deps.Hub.AppendEventData(workerId, "hijack_heartbeat", new Dictionary<string, object?>
        {
            ["hijack_id"] = hijackId,
            ["lease_s"] = leaseS,
        });
        await _deps.Hub.BroadcastHijackStateAsync(workerId, ctx.RequestAborted).ConfigureAwait(false);
        return Results.Json(new
        {
            ok = true,
            worker_id = workerId,
            hijack_id = hijackId,
            lease_expires_at = _clock.Wall() + leaseS,
        }, JsonOpts);
    }

    private async Task<IResult> HandleHijackSend(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        if (!AllowRestWrite(ctx, "rest_send_rate_limited_total", out var limited))
        {
            return limited!;
        }

        if (_deps.Hub.GetRestSession(workerId, hijackId) is null)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var keys = Str(body, "keys");
        var (ok, reason) = await _deps.Hub.Conn.SendRestInputAsync(workerId, hijackId, keys, ctx.RequestAborted)
            .ConfigureAwait(false);
        if (!ok) return BridgeError(409, reason);
        _deps.Hub.AppendEventData(workerId, "hijack_send", new Dictionary<string, object?>
        {
            ["hijack_id"] = hijackId,
            ["n"] = keys.Length,
        });
        return Results.Json(new { ok = true, worker_id = workerId, hijack_id = hijackId }, JsonOpts);
    }

    private async Task<IResult> HandleHijackStep(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        // Step is a write into a hijacked worker, so it is metered like one, and
        // it spends the *send* budget rather than a budget of its own — the
        // reference charges `allow_rest_send_for` from the step route
        // (bridge/routes/rest.py:429), as does Go (server/bridge_rest2.go:97).
        // The refusal is still counted under step's own name.
        if (!AllowRestWrite(ctx, "rest_step_rate_limited_total", out var limited))
        {
            return limited!;
        }

        if (_deps.Hub.GetRestSession(workerId, hijackId) is null)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

        await _deps.Hub.SendWorkerAsync(workerId, new Dictionary<string, object?>
        {
            ["type"] = "control",
            ["action"] = "step",
            ["hijack_id"] = hijackId,
            ["ts"] = _clock.Wall(),
        }, ctx.RequestAborted).ConfigureAwait(false);
        return Results.Json(new { ok = true, worker_id = workerId, hijack_id = hijackId }, JsonOpts);
    }

    private async Task<IResult> HandleHijackRelease(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        var (released, shouldResume) = _deps.Hub.ReleaseRestHijack(workerId, hijackId);
        if (!released)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

        if (shouldResume)
        {
            await _deps.Hub.SendWorkerAsync(workerId, HijackLeaseManager.ResumeFrame("operator", _clock.Wall()), ctx.RequestAborted)
                .ConfigureAwait(false);
        }

        _deps.Hub.NotifyHijackChanged(workerId, false, null);
        _deps.Hub.AppendEventData(workerId, "hijack_released", new Dictionary<string, object?>
        {
            ["hijack_id"] = hijackId,
        });
        await _deps.Hub.BroadcastHijackStateAsync(workerId, ctx.RequestAborted).ConfigureAwait(false);
        return Results.Json(new { ok = true, worker_id = workerId, hijack_id = hijackId }, JsonOpts);
    }

    private async Task<IResult> HandleHijackSnapshot(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.read").ConfigureAwait(false);
        if (authError is not null) return authError;
        if (_deps.Hub.GetRestSession(workerId, hijackId) is null)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

        // Ask the worker for the screen as it is now, the way the reference does
        // (bridge/routes/rest.py: hijack_snapshot → hub.wait_for_snapshot, whose
        // first act is request_snapshot). A worker that answers in process — the
        // session's own connector, bridged by LocalWorkerLink — has stored its
        // answer by the time this returns, so what the lease reads back includes
        // the keys it just sent. A worker across a socket answers when it
        // answers; this port reads the last snapshot it filed rather than
        // holding the request open for one, which is the reference's poll loop
        // and not yet ported.
        await _deps.Hub.Presence.RequestSnapshotAsync(workerId, ctx.RequestAborted).ConfigureAwait(false);
        var snap = _deps.Hub.Router.GetLastSnapshot(workerId) ?? new Dictionary<string, object?>
        {
            ["text"] = "",
            ["cols"] = 80,
            ["rows"] = 25,
        };
        return Results.Json(new { ok = true, worker_id = workerId, hijack_id = hijackId, snapshot = snap }, JsonOpts);
    }

    private async Task<IResult> HandleHijackEvents(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.read").ConfigureAwait(false);
        if (authError is not null) return authError;
        if (_deps.Hub.GetRestSession(workerId, hijackId) is null)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

        var after = int.TryParse(ctx.Request.Query["after_seq"], out var a) ? a : 0;
        var limit = int.TryParse(ctx.Request.Query["limit"], out var l) ? l : 200;
        var events = _deps.Hub.Router.GetRecentEvents(workerId, limit, after);
        return Results.Json(new { ok = true, events }, JsonOpts);
    }

    private async Task<IResult> HandleInputMode(HttpContext ctx, string workerId)
    {
        if (!SafeId.IsMatch(workerId)) return DetailError(422, "invalid worker_id");
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.mode").ConfigureAwait(false);
        if (authError is not null) return authError;
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var mode = Str(body, "input_mode", InputModes.Hijack);
        var (ok, reason) = _deps.Hub.Router.SetInputMode(workerId, mode);
        if (!ok) return BridgeError(400, reason);
        return Results.Json(new { ok = true, worker_id = workerId, input_mode = mode }, JsonOpts);
    }

    private async Task<IResult> HandleDisconnectWorker(HttpContext ctx, string workerId)
    {
        if (!SafeId.IsMatch(workerId)) return DetailError(422, "invalid worker_id");
        // Authentication before the role check, as on every other hub route:
        // the reference's admin arm (app/hub_authz.py:97-100) runs inside a
        // router already mounted behind _require_authenticated.
        var (p, authError) = await RequireAuthenticated(ctx).ConfigureAwait(false);
        if (authError is not null) return authError;
        if (!_deps.Authz.IsAdmin(p))
        {
            return DetailError(403, "admin role required");
        }

        var ok = _deps.Hub.Conn.DisconnectWorker(workerId);
        return Results.Json(new { ok, worker_id = workerId }, JsonOpts);
    }

    private async Task HandleBrowserWs(HttpContext ctx, string workerId)
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

        // UTERM_TEST_MODE=1: multi-backend Playwright e2e — admin for any worker_id.
        var testMode = string.Equals(
            Environment.GetEnvironmentVariable("UTERM_TEST_MODE"), "1", StringComparison.Ordinal);
        Principal p;
        string role;
        if (testMode)
        {
            p = new Principal { SubjectId = "test-admin", Roles = StringSet.Of("admin") };
            role = "admin";
        }
        else
        {
            p = await Authenticate(ctx).ConfigureAwait(false);
            role = "viewer";
            if (_deps.Registry.TryGetDefinition(workerId, out var def))
            {
                if (!_deps.Authz.CanReadSession(p, def))
                {
                    ctx.Response.StatusCode = StatusCodes.Status403Forbidden;
                    return;
                }

                role = _deps.Authz.ResolveBrowserRole(p, def);
            }
        }

        using var ws = await ctx.WebSockets.AcceptWebSocketAsync().ConfigureAwait(false);
        var conn = new BrowserWsConn(ws);
        // Match Python/Go: register, then hello from registry state + immediate hijack_state.
        var state = _deps.Hub.Conn.RegisterBrowser(workerId, conn, role);
        var canHijack = role is "admin";
        static bool StateBool(IReadOnlyDictionary<string, object?> d, string key) =>
            d.TryGetValue(key, out var v) && v is true;
        static string StateStr(IReadOnlyDictionary<string, object?> d, string key, string fallback) =>
            d.TryGetValue(key, out var v) && v is string s && !string.IsNullOrEmpty(s) ? s : fallback;

        var resumeToken = MintResumeToken(workerId, role);
        // Capability defaults match spec/behavior.json hello_defaults.csharp.
        var hello = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["role"] = role,
            ["worker_id"] = workerId,
            ["ts"] = _clock.Wall(),
            ["can_hijack"] = canHijack,
            // RegisterBrowser uses is_hijacked (hub internal); wire hello uses hijacked.
            ["hijacked"] = StateBool(state, "is_hijacked"),
            ["hijacked_by_me"] = StateBool(state, "hijacked_by_me"),
            ["worker_online"] = StateBool(state, "worker_online"),
            ["input_mode"] = StateStr(state, "input_mode", InputModes.Hijack),
            ["hijack_control"] = "ws",
            ["hijack_step_supported"] = true,
            ["capabilities"] = new Dictionary<string, object?>
            {
                ["hijack_control"] = "ws",
                ["hijack_step_supported"] = true,
            },
            ["mcp_supported"] = false, // spec/behavior.json hello_defaults.csharp
            ["vnc_supported"] = true,
            ["resume_supported"] = true,
            ["resume_token"] = resumeToken,
        });
        await conn.SendTextAsync(hello, ctx.RequestAborted).ConfigureAwait(false);
        // Per-browser owner="me"/"other" — required for second-browser tests.
        var hijackState = _deps.Hub.Router.HijackStateMsgFor(workerId, conn);
        await conn.SendTextAsync(
            ControlChannelCodec.EncodeControlFrame(hijackState),
            ctx.RequestAborted).ConfigureAwait(false);

        // DeckMux: presence_sync on join (+ fan-out when others present).
        var presenceSync = await _deckMux.OnBrowserConnectAsync(workerId, conn, role, ctx.RequestAborted)
            .ConfigureAwait(false);
        await conn.SendTextAsync(
            ControlChannelCodec.EncodeControlFrame(presenceSync),
            ctx.RequestAborted).ConfigureAwait(false);

        var buffer = new byte[8192];
        try
        {
            while (ws.State == WebSocketState.Open)
            {
                var result = await ws.ReceiveAsync(buffer, ctx.RequestAborted).ConfigureAwait(false);
                if (result.MessageType == WebSocketMessageType.Close) break;
                var text = Encoding.UTF8.GetString(buffer, 0, result.Count);
                await HandleBrowserMessage(workerId, conn, role, text, ctx.RequestAborted).ConfigureAwait(false);
            }
        }
        finally
        {
            _deps.Hub.Conn.CleanupBrowser(workerId, conn);
            try
            {
                await _deckMux.OnBrowserDisconnectAsync(workerId, conn, CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch
            {
                // best-effort
            }

            // Fan out released state when the owner drops, matching Python/Go cleanup.
            try
            {
                await _deps.Hub.BroadcastHijackStateAsync(workerId, CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch
            {
                // best-effort on disconnect
            }

            if (ws.State == WebSocketState.Open)
            {
                await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "bye", CancellationToken.None)
                    .ConfigureAwait(false);
            }
        }
    }

    private async Task HandleBrowserMessage(
        string workerId, BrowserWsConn conn, string role, string text, CancellationToken ct)
    {
        if (ControlChannelCodec.IsControlFrame(text))
        {
            var dec = new ControlFrameDecoder();
            foreach (var chunk in dec.Feed(text))
            {
                if (chunk is not ControlChunk ctrl) continue;
                var mtype = ctrl.Control.TryGetValue("type", out var t) ? t?.ToString() : null;
                switch (mtype)
                {
                    case "hijack_request":
                        if (role != "admin")
                        {
                            await conn.SendTextAsync(
                                ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                                {
                                    ["type"] = "error",
                                    ["message"] = "Hijack requires admin role.",
                                }),
                                ct).ConfigureAwait(false);
                            break;
                        }

                        // Pause worker (same control frame as Python/Go dashboard hijack).
                        _ = await _deps.Hub.Conn.SendWorkerAsync(
                            workerId,
                            new Dictionary<string, object?>
                            {
                                ["type"] = "control",
                                ["action"] = "pause",
                                ["source"] = "dashboard",
                                ["ts"] = _clock.Wall(),
                            },
                            ct).ConfigureAwait(false);

                        var (ok, reason) = _deps.Hub.Lease.TryAcquireWs(workerId, conn);
                        if (!ok)
                        {
                            if (reason != "already_hijacked")
                            {
                                _ = await _deps.Hub.Conn.SendWorkerAsync(
                                    workerId,
                                    new Dictionary<string, object?>
                                    {
                                        ["type"] = "control",
                                        ["action"] = "resume",
                                        ["source"] = "dashboard",
                                        ["ts"] = _clock.Wall(),
                                    },
                                    ct).ConfigureAwait(false);
                            }

                            await conn.SendTextAsync(
                                ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                                {
                                    ["type"] = "error",
                                    ["message"] = reason == "already_hijacked"
                                        ? "Session already hijacked."
                                        : "Hijack failed: " + reason,
                                }),
                                ct).ConfigureAwait(false);
                            break;
                        }

                        await _deps.Hub.BroadcastHijackStateAsync(workerId, ct).ConfigureAwait(false);
                        break;
                    case "hijack_release":
                        var (released, restActive) = _deps.Hub.Lease.TryReleaseWs(workerId, conn);
                        if (released && !restActive)
                        {
                            _ = await _deps.Hub.Conn.SendWorkerAsync(
                                workerId,
                                new Dictionary<string, object?>
                                {
                                    ["type"] = "control",
                                    ["action"] = "resume",
                                    ["source"] = "dashboard",
                                    ["ts"] = _clock.Wall(),
                                },
                                ct).ConfigureAwait(false);
                        }

                        await _deps.Hub.BroadcastHijackStateAsync(workerId, ct).ConfigureAwait(false);
                        break;
                    case "hijack_step":
                        _ = await _deps.Hub.Conn.SendWorkerAsync(
                            workerId,
                            new Dictionary<string, object?>
                            {
                                ["type"] = "control",
                                ["action"] = "step",
                                ["source"] = "dashboard",
                                ["ts"] = _clock.Wall(),
                            },
                            ct).ConfigureAwait(false);
                        break;
                    case "snapshot_req":
                        break;
                    case "heartbeat":
                    {
                        // Touch dashboard lease only if this browser owns it (Python touch_if_owner).
                        var st = _deps.Hub.Registry.Get(workerId);
                        if (st is not null
                            && _deps.Hub.State.IsDashboardHijackActive(st)
                            && ReferenceEquals(st.HijackOwner, conn))
                        {
                            var exp = _deps.Hub.Lease.TouchOwner(workerId);
                            if (exp is not null)
                            {
                                await conn.SendTextAsync(
                                    ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                                    {
                                        ["type"] = "heartbeat_ack",
                                        ["lease_expires_at"] = _clock.Wall()
                                            + (exp.Value - _clock.Monotonic()),
                                        ["ts"] = _clock.Wall(),
                                    }),
                                    ct).ConfigureAwait(false);
                                await _deps.Hub.BroadcastHijackStateAsync(workerId, ct)
                                    .ConfigureAwait(false);
                            }
                        }

                        break;
                    }
                    case "resume":
                    {
                        var oldTok = ctrl.Control.TryGetValue("token", out var tokObj)
                            ? tokObj?.ToString() ?? ""
                            : "";
                        if (string.IsNullOrEmpty(oldTok)
                            || !_resumeTokens.TryRemove(oldTok, out var rec)
                            || rec.WorkerId != workerId
                            || rec.ExpiresAt < _clock.Monotonic())
                        {
                            break;
                        }

                        var newTok = MintResumeToken(workerId, role);
                        var stSnap = _deps.Hub.Conn.RegisterBrowser(workerId, conn, role);
                        await conn.SendTextAsync(
                            ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                            {
                                ["type"] = "hello",
                                ["role"] = role,
                                ["worker_id"] = workerId,
                                ["ts"] = _clock.Wall(),
                                ["can_hijack"] = role is "admin",
                                ["hijacked"] = stSnap.TryGetValue("is_hijacked", out var ih) && ih is true,
                                ["hijacked_by_me"] = stSnap.TryGetValue("hijacked_by_me", out var hbm) && hbm is true,
                                ["worker_online"] = stSnap.TryGetValue("worker_online", out var wo) && wo is true,
                                ["input_mode"] = stSnap.TryGetValue("input_mode", out var im) && im is string ims
                                    ? ims
                                    : InputModes.Hijack,
                                ["resume_supported"] = true,
                                ["resume_token"] = newTok,
                                ["resumed"] = true,
                                ["hijack_control"] = "ws",
                                ["hijack_step_supported"] = true,
                                ["mcp_supported"] = false, // spec/behavior.json hello_defaults.csharp
                                ["vnc_supported"] = true,
                            }),
                            ct).ConfigureAwait(false);
                        await _deps.Hub.BroadcastHijackStateAsync(workerId, ct).ConfigureAwait(false);
                        break;
                    }
                    case "presence_update":
                    case "control_request":
                    case "queued_input":
                        await _deckMux.HandleMessageAsync(workerId, conn, ctrl.Control, ct)
                            .ConfigureAwait(false);
                        break;
                    case "ping":
                        await conn.SendTextAsync(
                            ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                            {
                                ["type"] = "pong",
                                ["ts"] = _clock.Wall(),
                            }),
                            ct).ConfigureAwait(false);
                        break;
                }
            }

            return;
        }

        if (_deps.Hub.Lease.PrepareBrowserInput(workerId, conn))
        {
            await _deps.Hub.Conn.SendRestInputAsync(workerId, "", text, ct).ConfigureAwait(false);
            var st = _deps.Hub.Registry.Get(workerId);
            if (st?.WorkerWs is not null)
            {
                await st.WorkerWs.SendTextAsync(text, ct).ConfigureAwait(false);
            }
        }
    }

    private async Task HandleWorkerWs(HttpContext ctx, string workerId)
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

        // Optional worker bearer
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
        if (_deps.Registry is InMemorySessionRegistry mem)
        {
            mem.MarkWorker(workerId, true, false, InputModes.Hijack);
        }

        // Notify already-connected browsers (Python/Go worker_connected fan-out).
        await _deps.Hub.Conn.BroadcastToBrowsersAsync(
            workerId,
            new Dictionary<string, object?>
            {
                ["type"] = "worker_connected",
                ["worker_id"] = workerId,
                ["ts"] = _clock.Wall(),
            },
            CancellationToken.None).ConfigureAwait(false);

        var buffer = new byte[16384];
        try
        {
            while (ws.State == WebSocketState.Open)
            {
                var result = await ws.ReceiveAsync(buffer, ctx.RequestAborted).ConfigureAwait(false);
                if (result.MessageType == WebSocketMessageType.Close) break;
                var text = Encoding.UTF8.GetString(buffer, 0, result.Count);
                if (ControlChannelCodec.IsControlFrame(text))
                {
                    // Fan-out snapshot/control to browsers (color e2e + resume paths).
                    var dec = new ControlFrameDecoder();
                    foreach (var chunk in dec.Feed(text))
                    {
                        if (chunk is not ControlChunk ctrl) continue;
                        var mtype = ctrl.Control.TryGetValue("type", out var t) ? t?.ToString() : null;
                        if (mtype == "snapshot")
                        {
                            _deps.Hub.Conn.UpdateLastSnapshot(workerId, ctrl.Control);
                        }

                        await _deps.Hub.Conn.BroadcastToBrowsersAsync(workerId, ctrl.Control, ctx.RequestAborted)
                            .ConfigureAwait(false);
                    }

                    continue;
                }

                // Raw terminal bytes → term control frames for every browser (Python/Go).
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
                    // best-effort on worker teardown
                }
            }
        }
    }

    private async Task<Principal> Authenticate(HttpContext ctx)
    {
        var req = new AuthRequest
        {
            SourceIp = ctx.Connection.RemoteIpAddress?.ToString() ?? "",
        };
        foreach (var h in ctx.Request.Headers)
        {
            req.Headers[h.Key] = h.Value.ToString();
        }

        foreach (var c in ctx.Request.Cookies)
        {
            req.Cookies[c.Key] = c.Value;
        }

        return await _deps.Auth.AuthenticateAsync(req, ctx.RequestAborted).ConfigureAwait(false);
    }

    /// <summary>
    /// Go/Python <c>require_authenticated</c> parity: anonymous principal → 401.
    /// </summary>
    private async Task<(Principal Principal, IResult? Error)> RequireAuthenticated(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (string.Equals(p.SubjectId, "anonymous", StringComparison.Ordinal))
        {
            return (p, DetailError(401, "authentication required"));
        }

        return (p, null);
    }

    /// <summary>
    /// The two gates every <c>/worker/{id}/...</c> route passes, in the order
    /// the reference mounts them: <c>_require_authenticated</c> first, then
    /// <c>_require_hub_route_authz</c>
    /// (<c>app/routes_wiring.py:47-50</c>). The order is the point. A caller who
    /// presented no credential is nobody and is told so (401) before any session
    /// is looked up; only a caller the server did authenticate can be told they
    /// hold the wrong role (403). Deciding both at once would let an
    /// unauthenticated caller read session state out of which refusal they got.
    /// </summary>
    private async Task<(Principal Principal, IResult? Error)> RequireHubAuthz(
        HttpContext ctx, string workerId, string capability)
    {
        var (p, authError) = await RequireAuthenticated(ctx).ConfigureAwait(false);
        if (authError is not null)
        {
            return (p, authError);
        }

        return AuthorizeHub(p, workerId, capability, out var error) ? (p, null) : (p, error);
    }

    private bool AuthorizeHub(Principal p, string workerId, string capability, out IResult? error)
    {
        if (!_deps.Registry.TryGetDefinition(workerId, out var def))
        {
            // A worker nobody registered is absent, and absent is what the
            // caller is told — in the session routes' `detail` envelope, and
            // calling it a session even here, because that is what the
            // reference's hub authz says (app/hub_authz.py:108-110). It has no
            // ad-hoc arm: a worker with no SessionDefinition has no visibility
            // policy to consult, so there is nothing to authorize against. The
            // arm this port used to have auto-registered the worker for any
            // admin, which turned "does not exist" into "exists, nobody home"
            // (409) and let a typo'd worker id mint a session.
            error = DetailError(404, "unknown session: " + workerId);
            return false;
        }

        if (capability == "session.read")
        {
            if (!_deps.Authz.CanReadSession(p, def))
            {
                error = DetailError(403, "insufficient privileges");
                return false;
            }
        }
        else if (!_deps.Authz.CanMutateSession(p, def, capability))
        {
            // For hijack on public sessions owned by admin, also allow operators that own the session.
            error = DetailError(403, "insufficient privileges");
            return false;
        }

        error = null;
        return true;
    }

    private static bool ValidateIds(string workerId, string hijackId, out IResult? error)
    {
        if (!SafeId.IsMatch(workerId))
        {
            error = DetailError(422, "invalid worker_id");
            return false;
        }

        if (!HijackIdPattern.IsMatch(hijackId))
        {
            error = DetailError(422, "invalid hijack_id");
            return false;
        }

        error = null;
        return true;
    }

    private static IResult DetailError(int status, string detail) =>
        Results.Json(new { detail }, statusCode: status);

    /// <summary>
    /// The REST send budget, charged for every route that writes to a hijacked
    /// worker (<c>send</c> and <c>step</c>), with a per-route counter name.
    ///
    /// Called after authn/authz and before the lease lookup, which is the order
    /// the reference mounts it (bridge/routes/rest.py:345, :429 — both precede
    /// <c>get_rest_session</c>). The position is observable: an over-budget
    /// request for a lease nobody holds answers 429, not 404, so a caller
    /// cannot enumerate lease ids on somebody else's budget.
    /// </summary>
    private bool AllowRestWrite(HttpContext ctx, string metric, out IResult? error)
    {
        var clientId = ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown";
        if (_deps.Hub.AllowRestSendFor(clientId))
        {
            error = null;
            return true;
        }

        _deps.Hub.Metric(metric, 1);
        error = BridgeError(429, "rate_limited");
        return false;
    }

    /// <summary>
    /// The lease routes' refusal envelope: the <c>error</c> key and nothing
    /// else, exactly as the reference writes it
    /// (<c>bridge/routes/rest.py</c>, every <c>JSONResponse({"error": ...})</c>).
    /// The success bodies of those same routes do carry <c>ok: true</c> — that
    /// is the flag a client branches on before it types into somebody's
    /// terminal. Repeating it as <c>ok: false</c> on a refusal invents a second
    /// envelope that no reference client reads and that conformance/live
    /// scenarios 006/007 pin against.
    /// </summary>
    private static IResult BridgeError(int status, string error) =>
        Results.Json(new { error }, statusCode: status);

    // The reference's wording for each acquire refusal, verbatim
    // (bridge/routes/rest.py:213-217 error_msgs).
    private static string AcquireErrorMessage(string reason) => reason switch
    {
        "no_worker" => "No worker connected for this session.",
        "open_mode" => "Hijack not available in open input mode.",
        "already_hijacked" => "Worker is already hijacked.",
        _ => reason,
    };

    private static string NewHijackId() =>
        Convert.ToHexString(RandomNumberGenerator.GetBytes(8)).ToLowerInvariant();

    private static async Task<Dictionary<string, JsonElement>> ReadJson(HttpContext ctx)
    {
        if (ctx.Request.ContentLength is null or 0)
        {
            return new Dictionary<string, JsonElement>();
        }

        try
        {
            var doc = await JsonSerializer.DeserializeAsync<Dictionary<string, JsonElement>>(ctx.Request.Body)
                .ConfigureAwait(false);
            return doc ?? new Dictionary<string, JsonElement>();
        }
        catch
        {
            return new Dictionary<string, JsonElement>();
        }
    }

    private static string Str(Dictionary<string, JsonElement> body, string key, string dflt = "")
    {
        if (!body.TryGetValue(key, out var el)) return dflt;
        return el.ValueKind == JsonValueKind.String ? el.GetString() ?? dflt : el.ToString();
    }

    private static int Int(Dictionary<string, JsonElement> body, string key, int dflt)
    {
        if (!body.TryGetValue(key, out var el)) return dflt;
        return el.ValueKind switch
        {
            JsonValueKind.Number when el.TryGetInt32(out var i) => i,
            JsonValueKind.String when int.TryParse(el.GetString(), out var s) => s,
            _ => dflt,
        };
    }

    /// <summary>WebSocket adapter implementing <see cref="IWorkerWs"/>.</summary>
    private sealed class BrowserWsConn : IWorkerWs
    {
        private readonly WebSocket _ws;
        private readonly SemaphoreSlim _sendGate = new(1, 1);

        public BrowserWsConn(WebSocket ws) => _ws = ws;

        public async Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            var bytes = Encoding.UTF8.GetBytes(payload);
            await _sendGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                if (_ws.State != WebSocketState.Open) return;
                await _ws.SendAsync(bytes, WebSocketMessageType.Text, true, cancellationToken).ConfigureAwait(false);
            }
            finally
            {
                _sendGate.Release();
            }
        }
    }

}

/// <summary>Factory helpers for assembling a runnable server from config.</summary>
public static class ServerFactory
{
    /// <param name="graphicalTargets">Registry to use; defaults to a
    /// non-durable one seeded from config.</param>
    /// <param name="clock">Time source for the hub (leases, rate-limit
    /// refills) and the server. Defaults to <see cref="RealClock"/>; tests pass
    /// a <see cref="ManualClock"/> so a spent budget stays spent for the length
    /// of the test instead of refilling on a slow runner.</param>
    public static (UtermServer Server, string? DevToken) CreateFromConfig(
        UtermServerConfig cfg,
        string version = "0.0.0-dev",
        IGraphicalTargetRegistry? graphicalTargets = null,
        IClock? clock = null)
    {
        var apiKeys = new ApiKeyStore();
        string? devToken = null;
        if (cfg.Auth.Mode.Equals("dev_token", StringComparison.OrdinalIgnoreCase))
        {
            devToken = DevIdp.Setup(cfg.Auth);
        }

        var auth = new LocalIdentityProvider(cfg.Auth, apiKeys);
        var authz = AuthorizationService.FromConfig(cfg);
        clock ??= new RealClock();
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            WorkerToken = cfg.Auth.WorkerBearerToken,
            MaxWorkers = cfg.MaxWorkers,
            BrowserRateLimitPerSec = cfg.BrowserRateLimitPerSec,
            RestAcquireRateLimitPerSec = cfg.RestAcquireRateLimitPerSec,
            RestSendRateLimitPerSec = cfg.RestSendRateLimitPerSec,
        });
        var registry = new InMemorySessionRegistry(cfg.Sessions, cfg.Recording.EnabledByDefault);
        graphicalTargets ??= SeedGraphicalTargets(cfg);
        var tunnelStore = new Tunnel.MemoryTunnelStore();
        var webhooks = new WebhookManager(allowLoopbackDestinations: true);
        var profiles = new InMemoryProfileStore();
        var metrics = new ServerMetrics();
        // Default enable API keys for hosted server (tests can disable).
        if (!cfg.Auth.ApiKeysEnabled)
        {
            cfg.Auth.ApiKeysEnabled = true;
        }

        var frontendDir = Environment.GetEnvironmentVariable("UTERM_FRONTEND_DIR");
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = auth,
            Authz = authz,
            Config = cfg,
            Registry = registry,
            GraphicalTargets = graphicalTargets,
            Version = version,
            Clock = clock,
            Recording = BuildRecordingStore(cfg),
            Webhooks = webhooks,
            TunnelStore = tunnelStore,
            Profiles = profiles,
            Metrics = metrics,
            ApiKeys = apiKeys,
            FrontendDir = string.IsNullOrWhiteSpace(frontendDir) ? null : frontendDir,
        });
        return (server, devToken);
    }

    /// <summary>
    /// Builds a server whose runtime graphical targets live in the control
    /// plane, and returns the engine so the caller can dispose it.
    ///
    /// Durability follows <c>control_plane.backend</c>: sqlite keeps runtime
    /// targets across restarts, memory behaves like <see cref="CreateFromConfig"/>.
    /// The engine is opened AND migrated before use — nothing read a store
    /// before the graphical-target registry, so a missing schema would
    /// otherwise surface only at first use.
    ///
    /// Ownership: on success the engine belongs to the caller; if construction
    /// fails it is closed here rather than leaked.
    /// </summary>
    public static async Task<(UtermServer Server, string? DevToken, ControlPlane.IEngine Engine)>
        CreateFromConfigAsync(
            UtermServerConfig cfg, string version = "0.0.0-dev", CancellationToken ct = default)
    {
        var engine = await ControlPlane.Bootstrap
            .OpenAsync(cfg.ControlPlane.Backend, cfg.ControlPlane.DatabaseUrl, ct)
            .ConfigureAwait(false);
        try
        {
            var graphicalTargets = await NewControlPlaneGraphicalTargetsAsync(cfg, engine, ct)
                .ConfigureAwait(false);
            var (server, devToken) = CreateFromConfig(cfg, version, graphicalTargets);
            return (server, devToken, engine);
        }
        catch
        {
            await engine.CloseAsync(CancellationToken.None).ConfigureAwait(false);
            throw;
        }
    }

    /// <summary>Select recording store from config (local JSONL / memory / null).</summary>
    public static IRecordingStore BuildRecordingStore(UtermServerConfig cfg) =>
        cfg.Recording.StoreType.ToLowerInvariant() switch
        {
            "local" => new LocalFileStore(cfg.Recording.Directory),
            "memory" => new InMemoryStore(),
            _ => new NullStore(),
        };

    /// <summary>Builds a non-durable registry seeded with the config targets.</summary>
    private static IGraphicalTargetRegistry SeedGraphicalTargets(UtermServerConfig cfg) =>
        // Seeding runs once at startup, never in a request path, so completing
        // the (already-synchronous) in-memory seed here cannot starve the pool.
        SeedIntoAsync(new InMemoryGraphicalTargetRegistry(), cfg).GetAwaiter().GetResult();

    /// <summary>
    /// Builds a registry whose runtime targets live in the control plane, seeded
    /// with the same immutable config targets. The engine must already be open
    /// and migrated. Durability follows the configured backend: sqlite keeps
    /// runtime targets across restarts, memory behaves like
    /// <see cref="SeedGraphicalTargets"/>.
    /// </summary>
    public static Task<IGraphicalTargetRegistry> NewControlPlaneGraphicalTargetsAsync(
        UtermServerConfig cfg, ControlPlane.IEngine engine, CancellationToken ct = default) =>
        SeedIntoAsync(new ControlPlaneGraphicalTargetRegistry(engine), cfg, ct);

    /// <summary>Adds every enabled config target as an immutable system entry.</summary>
    private static async Task<IGraphicalTargetRegistry> SeedIntoAsync(
        IGraphicalTargetRegistry registry, UtermServerConfig cfg, CancellationToken ct = default)
    {
        foreach (var target in cfg.GraphicalTargets)
        {
            if (!target.Enabled)
            {
                continue;
            }

            await registry.AddStaticAsync(ToGraphicalTargetDefinition(target), ct).ConfigureAwait(false);
        }

        return registry;
    }

    private static Provide.Uterm.Server.GraphicalTargetDefinition ToGraphicalTargetDefinition(ServerConfig.GraphicalTargetDefinition target)
    {
        var protocol = (target.Protocol ?? GraphicalTargetConstants.ProtocolRfb).Trim().ToLowerInvariant();
        if (!GraphicalTargetConstants.SupportedProtocols.Contains(protocol))
        {
            throw new ArgumentException("unsupported graphical target protocol: " + target.Protocol);
        }

        var endpoint = target.TargetAddress.Trim();
        if (protocol != GraphicalTargetConstants.ProtocolMemory && string.IsNullOrWhiteSpace(endpoint))
        {
            throw new ArgumentException($"graphical target requires target_address for {protocol} protocol: {target.TargetId}");
        }

        var targetId = target.TargetId.Trim();
        if (string.IsNullOrWhiteSpace(targetId))
        {
            targetId = "gt-" + Guid.NewGuid().ToString("N")[..12];
        }

        if (protocol == GraphicalTargetConstants.ProtocolRfb)
        {
            var (host, port) = GraphicalTargetParsing.ParseRfbEndpoint(endpoint);
            endpoint = $"{host}:{port}";
        }
        else if (protocol == GraphicalTargetConstants.ProtocolLitevirt)
        {
            // Plain host:port gRPC target — validated, no rfb:// scheme imposed.
            var (host, port) = GraphicalTargetParsing.ParseLitevirtEndpoint(endpoint);
            endpoint = $"{host}:{port}";
        }

        return new Provide.Uterm.Server.GraphicalTargetDefinition
        {
            TargetId = targetId,
            TenantId = target.TenantId.Trim(),
            DisplayName = string.IsNullOrWhiteSpace(target.Name) ? targetId : target.Name,
            Protocol = protocol,
            Endpoint = protocol == GraphicalTargetConstants.ProtocolMemory ? null : endpoint,
            Width = target.Width <= 0 ? 640 : target.Width > 8192 ? 8192 : target.Width,
            Height = target.Height <= 0 ? 480 : target.Height > 8192 ? 8192 : target.Height,
            Config = new Dictionary<string, object?>(target.Config),
            IsSystem = true,
            IsStatic = true,
            CreatedBy = null,
            CreatedAt = DateTimeOffset.UtcNow,
        };
    }
}
