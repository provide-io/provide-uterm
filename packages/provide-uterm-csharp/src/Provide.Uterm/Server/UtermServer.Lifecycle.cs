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
        app.UseMiddleware<TelemetryMiddleware>();
        app.UseMiddleware<SecurityHeadersMiddleware>(_deps.Config.Security);
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
        await OnStartedAsync(app, cancellationToken).ConfigureAwait(false);
        _runTask = app.WaitForShutdownAsync(cancellationToken);
        await _runTask.ConfigureAwait(false);
    }

    /// <summary>Start in background; useful for CLI and tests.</summary>
    public async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var app = _app ?? Build();
        await app.StartAsync(cancellationToken).ConfigureAwait(false);
        await OnStartedAsync(app, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// Everything that happens once the host is listening, in one place so the
    /// two ways of starting cannot drift apart.
    /// </summary>
    private async Task OnStartedAsync(WebApplication app, CancellationToken cancellationToken)
    {
        BaseAddress = app.Urls.FirstOrDefault();
        MarkReady();
        StartApprovalSweep();
        await StartAutoStartSessionsAsync(cancellationToken).ConfigureAwait(false);
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
    private async Task StartAutoStartSessionsAsync(CancellationToken cancellationToken)
    {
        foreach (var item in _deps.Registry.ListWithDefinitions())
        {
            var def = item.Definition;
            if (def is null || !def.AutoStart) continue;
            try
            {
                await ActivateSessionAsync(def.SessionId, def, cancellationToken).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                RecordSessionStartFailure(def.SessionId, ex);
            }
        }
    }

    /// <summary>
    /// Mark a session that could not be brought up, where the reference's run
    /// loop comes to rest: <c>stopped</c>, with the reason in
    /// <c>last_error</c> and the instant in <c>stopped_at</c>.
    ///
    /// <c>server/runtime.py</c>'s <c>_run</c> (~425-482) does assign
    /// <c>_state = "error"</c> on a failed run — but that is a state *between
    /// retry attempts*. A permanent failure breaks out of the retry loop and the
    /// line after it assigns <c>"stopped"</c> and <c>_stopped_at</c>; a transient
    /// one sleeps a backoff and assigns <c>"starting"</c> again at the top of the
    /// loop. Nothing ever rests at <c>error</c>.
    ///
    /// So "stopped because nobody asked" and "stopped because it failed" are told
    /// apart by <c>last_error</c>, never by the state — which is why both it and
    /// <c>stopped_at</c> are written here.
    ///
    /// <c>error</c> stays in this port's vocabulary because it is the reference's
    /// (<c>bridge/contracts.py: SessionLifecycle</c>), and a client reading a
    /// reference server's state field can still be handed it. This port has no
    /// retry loop — one start attempt, no backoff — so it has no window in which
    /// to publish <c>error</c>, and nothing here assigns it.
    /// </summary>
    private void RecordSessionStartFailure(string sessionId, Exception error)
    {
        if (!_deps.Registry.TryGetStatus(sessionId, out var status)) return;
        status.LifecycleState = SessionLifecycleState.Stopped;
        status.Connected = false;
        status.LastError = error.Message;
        status.StoppedAt = _clock.Wall();
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
        // Before the app, not after: the workers POST outbound while the pipeline
        // is still up, and stopping them first means nothing is mid-delivery
        // against a half-disposed server.
        await ShutdownWebhooksAsync().ConfigureAwait(false);
        await StopApprovalSweepAsync().ConfigureAwait(false);
        if (_app is not null)
        {
            await _app.DisposeAsync().ConfigureAwait(false);
            _app = null;
        }

        // After the app, not before: annotate and the session logger write to the
        // recording sink from inside the request pipeline, so closing it while
        // requests can still arrive would trade a leaked handle for a write to a
        // disposed stream.
        //
        // <see cref="Recording.LocalFileStore"/> keeps one append handle per
        // session open for the store's lifetime, and the only thing that ever
        // closes one is EndSessionAsync — which never runs for a session that was
        // written to without being started and stopped, such as one that was only
        // annotated. So a server that is not asked to release the store holds a
        // write handle on every recording file it touched, for as long as the
        // process lives.
        //
        // On POSIX that is "merely" a descriptor leak, because the handle's
        // FileShare.Read maps to a shared advisory lock and a second writer opens
        // the same path regardless. On Windows the share mode is enforced: the
        // next FileMode.Append open of that path is refused outright, so a second
        // server over the same recording directory — an in-process restart, or a
        // test suite that boots one server per case — gets an IOException out of
        // the annotate route instead of a recording.
        (_recording as IDisposable)?.Dispose();
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
