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

/// <summary>Factory helpers for assembling a runnable server from config.</summary>
public static class ServerFactory
{
    /// <param name="graphicalTargets">Registry to use; defaults to a
    /// non-durable one seeded from config.</param>
    /// <param name="clock">Time source for the hub (leases, rate-limit
    /// refills) and the server. Defaults to <see cref="RealClock"/>; tests pass
    /// a <see cref="ManualClock"/> so a spent budget stays spent for the length
    /// of the test instead of refilling on a slow runner.</param>
    /// <param name="hostResolver">DNS for the webhook egress guard. Defaults to
    /// the platform resolver; tests inject one so the guard's DNS rows never
    /// depend on a real answer (or on there being a network at all).</param>
    public static (UtermServer Server, string? DevToken) CreateFromConfig(
        UtermServerConfig cfg,
        string version = "0.0.0-dev",
        IGraphicalTargetRegistry? graphicalTargets = null,
        IClock? clock = null,
        TextWriter? logWriter = null,
        IHostResolver? hostResolver = null)
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
        // A definitely-non-null alias, because the webhook guard's tunnel
        // predicate below is a closure: nullable flow state does not survive into
        // a lambda body, so capturing `clock` there reads as possibly-null.
        var serverClock = clock;
        // Built before the hub, not after, because the hub needs it. This
        // ordering is the whole reason `OnMetric` went unwired: the sink used to
        // be created below, so there was nothing to hand the hub, and every
        // counter it emitted was dropped while the emitting code looked correct.
        var metrics = new ServerMetrics();
        var log = Provide.Telemetry.ProvideTelemetry.GetLogger("provide.uterm.server");
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            WorkerToken = cfg.Auth.WorkerBearerToken,
            MaxWorkers = cfg.MaxWorkers,
            MaxConnectionsPerPrincipal = cfg.MaxConnectionsPerPrincipal,
            BrowserRateLimitPerSec = cfg.BrowserRateLimitPerSec,
            RestAcquireRateLimitPerSec = cfg.RestAcquireRateLimitPerSec,
            RestSendRateLimitPerSec = cfg.RestSendRateLimitPerSec,
            OnMetric = (name, value) => metrics.Inc(name, value),
            OnLog = (level, message) =>
            {
                if (level == "debug") log.Debug(message);
                else if (level == "warn" || level == "warning") log.Warn(message);
                else if (level == "error") log.Error(message);
                else log.Info(message);
            },
        });
        var registry = new InMemorySessionRegistry(cfg.Sessions, cfg.Recording.EnabledByDefault);
        graphicalTargets ??= SeedGraphicalTargets(cfg);
        var tunnelStore = new Tunnel.MemoryTunnelStore();
        // The egress guard's effective permission is decided once, here, where
        // the server is built from config — not at each call site, and not from
        // the config key alone (see EffectiveAllowLoopbackDestinations for why
        // the bind address is half of it). The delivery-time tunnel predicate is
        // handed the real store and the real clock, so an expired or revoked
        // share stops closing the guard the moment it stops being live.
        // The delivery workers are driven off the hub's event bus — the same bus
        // the REST watch/SSE surfaces read, fed by MessageRouter.AppendEvent — so
        // a webhook sees exactly the events an operator watching the session would
        // see. Without this the registry recorded registrations and delivered
        // nothing, which also left the delivery-time half of the egress guard
        // wired to no caller.
        var webhooks = new WebhookManager(
            WebhookEgressPolicy.EffectiveAllowLoopbackDestinations(cfg),
            hostResolver,
            sessionId => tunnelStore.HasLiveShare(sessionId, serverClock.Wall()),
            (name, value) => metrics.Inc(name, value),
            new WebhookDeliveryOptions
            {
                EventBus = hub.EventBus,
                // The server's clock, not the wall clock: the signature timestamp
                // has to agree with whatever the rest of this server stamps, or a
                // receiver's freshness window rejects deliveries for a reason
                // that appears nowhere in either log.
                Now = () => serverClock.Wall(),
                OnLog = (level, message) =>
                {
                    if (level == "debug") log.Debug(message);
                    else if (level == "warn" || level == "warning") log.Warn(message);
                    else if (level == "error") log.Error(message);
                    else log.Info(message);
                },
            });
        var profiles = new InMemoryProfileStore();
        // `api_keys_enabled` used to be forced on here whenever it was false,
        // commented "tests can disable" — which had it backwards: a test can pass
        // whatever it likes, and production was the only caller that could not say
        // no. The reference defaults the key to False (config_schema.py:72), so
        // honouring it as written also removes a divergence.

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
