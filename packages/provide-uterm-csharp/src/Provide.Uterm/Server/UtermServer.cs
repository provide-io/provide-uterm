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

    public UtermServer(ServerDeps deps)
    {
        _deps = deps;
        _clock = deps.Clock ?? new RealClock();
        _recording = deps.Recording ?? new NullStore();
        _startTime = _clock.Wall();
    }

    public string? BaseAddress { get; private set; }

    public void MarkReady() => _ready = true;

    /// <summary>Build the host without binding. Used by in-process tests via <see cref="CreateHandler"/>.</summary>
    public WebApplication Build(string[]? urls = null)
    {
        var builder = WebApplication.CreateBuilder(new WebApplicationOptions
        {
            Args = Array.Empty<string>(),
            ApplicationName = typeof(UtermServer).Assembly.FullName,
        });
        builder.Logging.ClearProviders(); // requires Microsoft.Extensions.Logging
        builder.WebHost.UseKestrel();
        if (urls is { Length: > 0 })
        {
            builder.WebHost.UseUrls(urls);
        }
        else
        {
            var host = _deps.Config.Server.Host;
            var port = _deps.Config.Server.Port;
            builder.WebHost.UseUrls($"http://{host}:{port}");
        }

        builder.Services.AddSingleton(this);
        var app = builder.Build();
        app.UseWebSockets();
        MapRoutes(app);
        _app = app;
        return app;
    }

    /// <summary>Start listening and mark ready. Returns when the host stops.</summary>
    public async Task RunAsync(CancellationToken cancellationToken = default)
    {
        var app = _app ?? Build();
        await app.StartAsync(cancellationToken).ConfigureAwait(false);
        BaseAddress = app.Urls.FirstOrDefault();
        MarkReady();
        _runTask = app.WaitForShutdownAsync(cancellationToken);
        await _runTask.ConfigureAwait(false);
    }

    /// <summary>Start in background; useful for CLI and tests.</summary>
    public async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var app = _app ?? Build();
        await app.StartAsync(cancellationToken).ConfigureAwait(false);
        BaseAddress = app.Urls.FirstOrDefault();
        MarkReady();
    }

    public async Task StopAsync(CancellationToken cancellationToken = default)
    {
        if (_app is not null)
        {
            await _app.StopAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_app is not null)
        {
            await _app.DisposeAsync().ConfigureAwait(false);
            _app = null;
        }
    }

    /// <summary>Expose the request pipeline for in-process HttpClient tests.</summary>
    public HttpMessageHandler CreateHandler()
    {
        var app = _app ?? Build(new[] { "http://127.0.0.1:0" });
        // Ensure routes exist; for TestServer-style use we return a custom handler.
        return new PipelineHandler(app);
    }

    private void MapRoutes(WebApplication app)
    {
        app.MapGet("/api/health", HandleHealth);
        app.MapGet("/healthz", () => Results.Json(new { status = "ok" }));
        app.MapGet("/readyz", () => _ready
            ? Results.Json(new { status = "ready" })
            : Results.Json(new { status = "not_ready" }, statusCode: 503));

        app.MapGet("/api/sessions", async (HttpContext ctx) =>
        {
            var p = await Authenticate(ctx).ConfigureAwait(false);
            var items = _deps.Registry.ListWithDefinitions()
                .Where(it => it.Definition is not null && it.Status is not null && _deps.Authz.CanReadSession(p, it.Definition!))
                .Select(it => EnrichStatus(it.Status!))
                .ToList();
            return Results.Json(items, JsonOpts);
        });

        app.MapGet("/api/sessions/{sessionId}", async (HttpContext ctx, string sessionId) =>
        {
            var p = await Authenticate(ctx).ConfigureAwait(false);
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

        app.MapGet("/api/graphical-targets", HandleListGraphicalTargets);
        app.MapGet("/api/graphical-targets/{targetId}", HandleGetGraphicalTarget);
        app.MapPost("/api/graphical-targets", HandleCreateGraphicalTarget);
        app.MapPut("/api/graphical-targets/{targetId}", HandleUpdateGraphicalTarget);
        app.MapDelete("/api/graphical-targets/{targetId}", HandleDeleteGraphicalTarget);

        // GUI REST (Go-compatible paths; memory attach for deterministic fixtures)
        app.MapPost("/worker/{workerId}/gui/attach", HandleGuiAttach);
        app.MapGet("/worker/{workerId}/hijack/{hijackId}/gui/screenshot", HandleGuiScreenshot);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/gui/click", HandleGuiClick);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/gui/type", HandleGuiType);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/gui/key", HandleGuiKey);
        app.MapPost("/worker/{workerId}/hijack/{hijackId}/gui/drag", HandleGuiDrag);

        // Browser / worker WebSockets with DLE/STX control channel
        app.Map("/ws/browser/{workerId}", HandleBrowserWs);
        app.Map("/ws/worker/{workerId}", HandleWorkerWs);
    }

    private SessionStatus EnrichStatus(SessionStatus st)
    {
        var hubSt = _deps.Hub.Registry.Get(st.SessionId);
        if (hubSt is not null)
        {
            st.WorkerOnline = hubSt.WorkerWs is not null;
            st.IsHijacked = _deps.Hub.State.IsHijacked(hubSt);
            st.InputMode = hubSt.InputMode;
        }

        return st;
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
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.control.hijack", out var err)) return err!;

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
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.control.hijack", out err)) return err!;

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
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.control.hijack", out err)) return err!;

        var clientId = ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown";
        if (!_deps.Hub.AllowRestSendFor(clientId))
        {
            return BridgeError(429, "rate_limited");
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
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.control.hijack", out err)) return err!;
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
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.control.hijack", out err)) return err!;

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
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.read", out err)) return err!;
        if (_deps.Hub.GetRestSession(workerId, hijackId) is null)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

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
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.read", out err)) return err!;
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
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!AuthorizeHub(p, workerId, "session.control.mode", out var err)) return err!;
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var mode = Str(body, "input_mode", InputModes.Hijack);
        var (ok, reason) = _deps.Hub.Router.SetInputMode(workerId, mode);
        if (!ok) return BridgeError(400, reason);
        return Results.Json(new { ok = true, worker_id = workerId, input_mode = mode }, JsonOpts);
    }

    private async Task<IResult> HandleDisconnectWorker(HttpContext ctx, string workerId)
    {
        if (!SafeId.IsMatch(workerId)) return DetailError(422, "invalid worker_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
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

        var p = await Authenticate(ctx).ConfigureAwait(false);
        var role = "viewer";
        if (_deps.Registry.TryGetDefinition(workerId, out var def))
        {
            if (!_deps.Authz.CanReadSession(p, def))
            {
                ctx.Response.StatusCode = StatusCodes.Status403Forbidden;
                return;
            }

            role = _deps.Authz.ResolveBrowserRole(p, def);
        }

        using var ws = await ctx.WebSockets.AcceptWebSocketAsync().ConfigureAwait(false);
        var conn = new BrowserWsConn(ws);
        var state = _deps.Hub.Conn.RegisterBrowser(workerId, conn, role);
        var hello = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["role"] = role,
            ["worker_id"] = workerId,
            ["state"] = state,
            ["ts"] = _clock.Wall(),
        });
        await conn.SendTextAsync(hello, ctx.RequestAborted).ConfigureAwait(false);

        var buffer = new byte[8192];
        try
        {
            while (ws.State == WebSocketState.Open)
            {
                var result = await ws.ReceiveAsync(buffer, ctx.RequestAborted).ConfigureAwait(false);
                if (result.MessageType == WebSocketMessageType.Close) break;
                var text = Encoding.UTF8.GetString(buffer, 0, result.Count);
                await HandleBrowserMessage(workerId, conn, text, ctx.RequestAborted).ConfigureAwait(false);
            }
        }
        finally
        {
            _deps.Hub.Conn.CleanupBrowser(workerId, conn);
            if (ws.State == WebSocketState.Open)
            {
                await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "bye", CancellationToken.None)
                    .ConfigureAwait(false);
            }
        }
    }

    private async Task HandleBrowserMessage(string workerId, BrowserWsConn conn, string text, CancellationToken ct)
    {
        if (ControlChannelCodec.IsControlFrame(text))
        {
            // Control frames from browser (resize, hijack acquire, etc.)
            return;
        }

        if (_deps.Hub.Lease.PrepareBrowserInput(workerId, conn))
        {
            await _deps.Hub.Conn.SendRestInputAsync(workerId, "", text, ct).ConfigureAwait(false);
            // Prefer worker raw send when no REST hijack
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
                    // Worker control (snapshot, hello) — store snapshot if present
                    continue;
                }

                _deps.Hub.AppendEventData(workerId, "term", new Dictionary<string, object?> { ["data"] = text });
                _deps.Hub.State.TouchActivity(workerId);
            }
        }
        finally
        {
            _deps.Hub.Conn.DeregisterWorker(workerId, conn);
            if (_deps.Registry is InMemorySessionRegistry mem2)
            {
                mem2.MarkWorker(workerId, false, false, InputModes.Hijack);
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

    private bool AuthorizeHub(Principal p, string workerId, string capability, out IResult? error)
    {
        if (!_deps.Registry.TryGetDefinition(workerId, out var def))
        {
            // Ad-hoc workers: allow admin/operator with capability
            if (capability == "session.read")
            {
                if (_deps.Authz.HasCapability(p, capability))
                {
                    error = null;
                    return true;
                }
            }
            else if (_deps.Authz.IsAdmin(p) || (_deps.Authz.HasCapability(p, capability) && p.Roles.Has("operator")))
            {
                // Auto-register unknown worker as a session so subsequent calls work.
                _deps.Registry.Upsert(new SessionDefinition
                {
                    SessionId = workerId,
                    DisplayName = workerId,
                    ConnectorType = "shell",
                    Visibility = "public",
                    Owner = p.SubjectId,
                });
                error = null;
                return true;
            }

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

    private static IResult BridgeError(int status, string error) =>
        Results.Json(new { ok = false, error }, statusCode: status);

    private static string AcquireErrorMessage(string reason) => reason switch
    {
        "no_worker" => "No worker connected.",
        "open_mode" => "Worker is in open input mode.",
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

    /// <summary>Routes in-process HTTP through the WebApplication pipeline.</summary>
    private sealed class PipelineHandler : HttpMessageHandler
    {
        private readonly WebApplication _app;
        private readonly ConcurrentDictionary<string, byte> _started = new();

        public PipelineHandler(WebApplication app) => _app = app;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            // Use TestServer-like approach via HttpContext features is complex;
            // instead start Kestrel on ephemeral port once and forward.
            if (_started.TryAdd("1", 0))
            {
                await _app.StartAsync(cancellationToken).ConfigureAwait(false);
            }

            var baseUrl = _app.Urls.FirstOrDefault() ?? "http://127.0.0.1";
            using var client = new HttpClient { BaseAddress = new Uri(baseUrl) };
            // Rebuild request for HttpClient
            var clone = new HttpRequestMessage(request.Method, request.RequestUri);
            if (request.Content is not null)
            {
                clone.Content = request.Content;
            }

            foreach (var h in request.Headers)
            {
                clone.Headers.TryAddWithoutValidation(h.Key, h.Value);
            }

            return await client.SendAsync(clone, cancellationToken).ConfigureAwait(false);
        }
    }
}

/// <summary>Factory helpers for assembling a runnable server from config.</summary>
public static class ServerFactory
{
    public static (UtermServer Server, string? DevToken) CreateFromConfig(UtermServerConfig cfg, string version = "0.0.0-dev")
    {
        var apiKeys = new ApiKeyStore();
        string? devToken = null;
        if (cfg.Auth.Mode.Equals("dev_token", StringComparison.OrdinalIgnoreCase))
        {
            devToken = DevIdp.Setup(cfg.Auth);
        }

        var auth = new LocalIdentityProvider(cfg.Auth, apiKeys);
        var authz = new AuthorizationService();
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            WorkerToken = cfg.Auth.WorkerBearerToken,
            MaxWorkers = cfg.MaxWorkers,
            BrowserRateLimitPerSec = cfg.BrowserRateLimitPerSec,
        });
        var registry = new InMemorySessionRegistry(cfg.Sessions);
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = auth,
            Authz = authz,
            Config = cfg,
            Registry = registry,
            Version = version,
            Clock = clock,
            Recording = BuildRecordingStore(cfg),
        });
        return (server, devToken);
    }

    /// <summary>Select recording store from config (local JSONL / memory / null).</summary>
    public static IRecordingStore BuildRecordingStore(UtermServerConfig cfg) =>
        cfg.Recording.StoreType.ToLowerInvariant() switch
        {
            "local" => new LocalFileStore(cfg.Recording.Directory),
            "memory" => new InMemoryStore(),
            _ => new NullStore(),
        };
}
