//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.DeckMux;
using Provide.Uterm.Hub;
using Provide.Uterm.Recording;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

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
    /// <summary>Internal deterministic seam for post-registration setup-failure tests.</summary>
    internal Func<Task>? BrowserSetupHook { get; init; }
    /// <summary>Internal deterministic seam observing consumed WebSocket fragments.</summary>
    internal Action<string, int, bool>? WebSocketFragmentObserved { get; init; }
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

    private readonly ResumeTokenStore _resumeTokens;
    private readonly object _resumeGate = new();
    private readonly Dictionary<object, string> _browserResumeTokens = new();
    private readonly DeckMuxPresence _deckMux;
    private readonly Action<int, bool>? _browserFragmentObserved;
    private readonly Action<int, bool>? _workerFragmentObserved;
    private readonly Action<int, bool>? _tunnelFragmentObserved;

    public UtermServer(ServerDeps deps)
    {
        _deps = deps;
        _clock = deps.Clock ?? new RealClock();
        _recording = deps.Recording ?? new NullStore();
        _startTime = _clock.Wall();
        _resumeTokens = new ResumeTokenStore(_clock);
        _deckMux = new DeckMuxPresence(new HubDeckMuxBroadcaster(_deps.Hub));
        if (deps.WebSocketFragmentObserved is { } fragmentObserved)
        {
            _browserFragmentObserved = (count, final) => fragmentObserved("browser", count, final);
            _workerFragmentObserved = (count, final) => fragmentObserved("worker", count, final);
            _tunnelFragmentObserved = (count, final) => fragmentObserved("tunnel", count, final);
        }
        _lazyFanout = new Lazy<Fanout.Controller>(
            CreateDefaultFanout,
            System.Threading.LazyThreadSafetyMode.ExecutionAndPublication);
        if (deps.Registry is InMemorySessionRegistry memoryRegistry)
        {
            deps.Hub.Conn.ConfigureWorkerOfflineMarker(workerId =>
                memoryRegistry.MarkWorker(workerId, false, false));
        }
    }

    private sealed class HubDeckMuxBroadcaster : IDeckMuxBroadcaster
    {
        private readonly TermHub _hub;
        public HubDeckMuxBroadcaster(TermHub hub) => _hub = hub;
        public Task BroadcastAsync(string workerId, Dictionary<string, object?> msg, CancellationToken ct = default) =>
            _hub.Conn.BroadcastToBrowsersAsync(workerId, msg, ct);
    }

    private string MintResumeToken(string workerId, string role, object connection)
    {
        var token = _resumeTokens.Mint(workerId, role);
        lock (_resumeGate)
        {
            if (_browserResumeTokens.Remove(connection, out var previous)) _resumeTokens.Revoke(previous);
            _browserResumeTokens[connection] = token;
        }

        return token;
    }

    private string? CurrentResumeToken(object connection)
    {
        lock (_resumeGate)
        {
            return _browserResumeTokens.GetValueOrDefault(connection);
        }
    }

    private void FinishResumeToken(object connection, long? ownershipVersion)
    {
        string? token;
        lock (_resumeGate)
        {
            _browserResumeTokens.Remove(connection, out token);
        }

        if (token is null) return;
        _resumeTokens.MarkDisconnected(token, ownershipVersion);
    }

    /// <summary>The graphical-target registry this server was built with.
    /// Exposed for tests that assert which backend the factory selected.</summary>
    internal IGraphicalTargetRegistry GraphicalTargets => _deps.GraphicalTargets;

    /// <summary>The hub this server was built with.
    /// Exposed so a test can assert what the factory wired onto it — a callback
    /// the factory forgets is invisible from outside.</summary>
    internal TermHub HubForTests => _deps.Hub;

    /// <summary>The metric sink this server was built with, for the same reason.</summary>
    internal ServerMetrics MetricsForTests => _deps.Metrics;

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
}
