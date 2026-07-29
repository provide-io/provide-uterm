//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Provide.Uterm.Server;

/// <summary>
/// The server's own lifecycle: building the host, starting it, stopping it —
/// and the one thing that has to happen in between, which is bringing up the
/// sessions the configuration asked to have running.
/// </summary>
public sealed partial class UtermServer
{
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
        UseFrameworkRefusalBodies(app);
        MapRoutes(app);
        _app = app;
        return app;
    }

    /// <summary>Start listening and mark ready. Returns when the host stops.</summary>
    public async Task RunAsync(CancellationToken cancellationToken = default)
    {
        var app = _app ?? Build();
        await app.StartAsync(cancellationToken).ConfigureAwait(false);
        OnStarted(app);
        _runTask = app.WaitForShutdownAsync(cancellationToken);
        await _runTask.ConfigureAwait(false);
    }

    /// <summary>Start in background; useful for CLI and tests.</summary>
    public async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var app = _app ?? Build();
        await app.StartAsync(cancellationToken).ConfigureAwait(false);
        OnStarted(app);
    }

    /// <summary>
    /// Everything that happens once the host is listening, in one place so the
    /// two ways of starting cannot drift apart.
    /// </summary>
    private void OnStarted(WebApplication app)
    {
        BaseAddress = app.Urls.FirstOrDefault();
        MarkReady();
        StartAutoStartSessions();
    }

    /// <summary>
    /// Bring up every session the configuration flagged <c>auto_start</c>.
    ///
    /// This is <c>registry.start_auto_start_sessions()</c>, which the reference
    /// runs from the app lifespan, and Go's <c>StartAutoStartSessions</c>. A
    /// port that stores the flag and never acts on it reports "not running" to
    /// every client for a session the operator asked to have running — and,
    /// because the flag is still echoed on the wire, says <c>auto_start: true</c>
    /// while it does so.
    ///
    /// One session that will not come up must not stop the rest, so a failure
    /// is recorded on that session and the loop carries on — the same
    /// tolerance Go's boot loop has.
    /// </summary>
    private void StartAutoStartSessions()
    {
        foreach (var item in _deps.Registry.ListWithDefinitions())
        {
            var def = item.Definition;
            if (def is null || !def.AutoStart) continue;
            try
            {
                ActivateSession(def.SessionId, def);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                RecordSessionStartFailure(def.SessionId, ex);
            }
        }
    }

    /// <summary>
    /// Mark a session that could not be brought up, the way the reference's
    /// runtime does: the lifecycle says <c>error</c> and <c>last_error</c> says
    /// what happened, rather than the session quietly looking stopped.
    /// </summary>
    private void RecordSessionStartFailure(string sessionId, Exception error)
    {
        if (!_deps.Registry.TryGetStatus(sessionId, out var status)) return;
        status.LifecycleState = SessionLifecycleState.Error;
        status.Connected = false;
        status.LastError = error.Message;
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
