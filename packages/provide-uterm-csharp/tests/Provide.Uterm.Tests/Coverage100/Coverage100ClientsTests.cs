//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using Provide.Uterm.Client;
using Provide.Uterm.Conformance;
using Provide.Uterm.Hub;
using Provide.Uterm.Recording;
using Provide.Uterm.SessionLogger;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>Client-library lease round trips, the GUI no-session refusal and logger edges.</summary>
public sealed class Coverage100ClientsTests
{
    private sealed class OkWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) => Task.CompletedTask;
    }

    private static async Task<C100Server> WithWorkerAsync()
    {
        // Registered before the server starts, as the configured session's worker.
        return await C100Server.StartAsync(new C100Server.Options
        {
            Deps = (cfg, hub) =>
            {
                Assert.True(hub.Conn.RegisterWorker(C100Server.Session, new OkWorker()));
                return C100Server.Default(cfg, hub);
            },
        });
    }

    [Fact]
    public async Task Live_driver_runs_every_lease_action_against_a_minted_lease()
    {
        await using var s = await WithWorkerAsync();
        var scenario = LiveScenario.Parse($$"""
            {"id": "900_c100_lease", "title": "Lease", "steps": [
              {"id": "acquire", "action": "hijack_acquire", "worker_id": "{{C100Server.Session}}", "owner": "operator", "lease_s": 30},
              {"id": "beat", "action": "hijack_heartbeat", "worker_id": "{{C100Server.Session}}", "hijack_id": "${acquire.body.hijack_id}", "lease_s": 45},
              {"id": "send", "action": "hijack_send", "worker_id": "{{C100Server.Session}}", "hijack_id": "${acquire.body.hijack_id}", "keys": "x"},
              {"id": "stepped", "action": "hijack_step", "worker_id": "{{C100Server.Session}}", "hijack_id": "${acquire.body.hijack_id}"},
              {"id": "snap", "action": "hijack_snapshot", "worker_id": "{{C100Server.Session}}", "hijack_id": "${acquire.body.hijack_id}"},
              {"id": "release", "action": "hijack_release", "worker_id": "{{C100Server.Session}}", "hijack_id": "${acquire.body.hijack_id}"}
            ], "expect": []}
            """);
        var result = await LiveClientDriver.RunAsync(scenario, s.BaseUrl, s.AdminToken);
        Assert.Equal(LiveResult.StatusCompleted, result.Status);
        foreach (var step in result.Steps)
        {
            Assert.True(step.Ok, $"{step.Id}: {step.Status} {step.Error} {step.Body}");
        }
    }

    [Fact]
    public async Task Heartbeat_overload_without_a_lease_length_uses_the_default()
    {
        await using var s = await WithWorkerAsync();
        using var client = HijackClient.WithBearer(s.BaseUrl, s.AdminToken);
        var acq = await client.AcquireAsync(C100Server.Session, owner: "operator", leaseS: 30);
        var beat = await client.Heartbeat(C100Server.Session, acq["hijack_id"]!.ToString()!, CancellationToken.None);
        Assert.True(beat.ContainsKey("lease_expires_at") || beat.ContainsKey("ok"), string.Join(",", beat.Keys));
    }

    [Fact]
    public async Task Gui_input_without_an_attached_session_is_not_found()
    {
        await using var s = await WithWorkerAsync();
        using var client = HijackClient.WithBearer(s.BaseUrl, s.AdminToken);
        var acq = await client.AcquireAsync(C100Server.Session, owner: "operator", leaseS: 30);
        using var admin = s.Http(s.AdminToken);
        var click = await admin.PostAsync(
            $"/worker/{C100Server.Session}/hijack/{acq["hijack_id"]}/gui/click", C100Server.Json(new { x = 1, y = 1 }));
        Assert.Equal(HttpStatusCode.NotFound, click.StatusCode);
    }

    private sealed class CountingStore : IRecordingStore
    {
        private readonly NullStore _inner = new();
        public int Appends { get; private set; }
        public Task StartSessionAsync(string sessionId, IReadOnlyDictionary<string, object?> metadata) => _inner.StartSessionAsync(sessionId, metadata);
        public Task AppendEventsAsync(string sessionId, IReadOnlyList<Event> events)
        {
            Appends++;
            return Task.CompletedTask;
        }

        public Task EndSessionAsync(string sessionId) => _inner.EndSessionAsync(sessionId);
        public Task<Meta> RecordingMetaAsync(string sessionId) => _inner.RecordingMetaAsync(sessionId);
        public Task<IReadOnlyList<Event>> GetEntriesAsync(string sessionId, Query query) => _inner.GetEntriesAsync(sessionId, query);
        public Task<string> GetPathAsync(string sessionId) => _inner.GetPathAsync(sessionId);
    }

    [Fact]
    public async Task Logger_holds_events_logged_before_it_has_a_session()
    {
        var store = new CountingStore();
        var logger = new SessionLogger.SessionLogger(store, new SessionLoggerOptions { BatchSize = 1 });
        await logger.LogAsync("read", new Dictionary<string, object?> { ["x"] = 1 });
        // No session yet: the flush has nowhere to write, so nothing is appended.
        Assert.Equal(0, store.Appends);
    }
}
