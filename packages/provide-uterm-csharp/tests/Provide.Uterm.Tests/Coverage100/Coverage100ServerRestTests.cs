//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Text.Json;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Tunnel;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>REST refusal and edge arms for profiles, keys, approvals, sessions, SSE and tunnels.</summary>
public sealed class Coverage100ServerRestTests
{
    [Fact]
    public async Task Profile_update_applies_port_and_tags_and_connect_needs_create_rights()
    {
        var profiles = new InMemoryProfileStore();
        await using var s = await C100Server.StartAsync(new C100Server.Options
        {
            Deps = (cfg, hub) =>
            {
                var d = C100Server.Default(cfg, hub);
                return new ServerDeps
                {
                    Hub = d.Hub, Auth = d.Auth, Authz = d.Authz, Config = d.Config, Registry = d.Registry,
                    ApiKeys = d.ApiKeys, Profiles = profiles, Version = d.Version,
                };
            },
        });
        using var admin = s.Http(s.AdminToken);
        var created = await C100Server.ReadJsonAsync(await admin.PostAsync("/api/profiles", C100Server.Json(new { name = "p" })));
        var pid = created.GetProperty("profile_id").GetString()!;
        var put = await admin.PutAsync($"/api/profiles/{pid}", C100Server.Json(new { port = 2222, tags = new[] { "x" } }));
        var updated = await C100Server.ReadJsonAsync(put);
        Assert.Equal(2222, updated.GetProperty("port").GetInt32());
        Assert.Equal("x", updated.GetProperty("tags")[0].GetString());

        // A viewer may read their own profile but cannot open a session from it.
        profiles.CreateProfile(new ConnectionProfile { ProfileId = "profile-viewerown", Owner = "v1", Name = "mine" });
        using var viewer = s.Http(s.Token("v1", "viewer"));
        var connect = await viewer.PostAsync("/api/profiles/profile-viewerown/connect", C100Server.Json(new { }));
        Assert.Equal(HttpStatusCode.Forbidden, connect.StatusCode);
    }

    [Fact]
    public async Task Api_key_creation_validates_scopes_and_expiry()
    {
        await using var s = await C100Server.StartAsync();
        using var admin = s.Http(s.AdminToken);
        Assert.Equal(HttpStatusCode.UnprocessableEntity, (await admin.PostAsync("/api/keys", C100Server.Json(new { name = "k" }))).StatusCode);
        Assert.Equal(HttpStatusCode.UnprocessableEntity, (await admin.PostAsync("/api/keys", C100Server.Json(new { name = "k", scopes = new[] { "", " " } }))).StatusCode);
        var ok = await admin.PostAsync("/api/keys", C100Server.Json(new { name = "k", scopes = new[] { "viewer" }, expires_in_s = 120 }));
        Assert.Equal(HttpStatusCode.OK, ok.StatusCode);
        Assert.NotEqual(JsonValueKind.Null, (await C100Server.ReadJsonAsync(ok)).GetProperty("expires_at").ValueKind);

        await using var off = await C100Server.StartAsync(new C100Server.Options { Configure = c => c.Auth.ApiKeysEnabled = false });
        using var admin2 = off.Http(off.AdminToken);
        var refused = await admin2.PostAsync("/api/keys", C100Server.Json(new { name = "k", scopes = new[] { "viewer" } }));
        Assert.Equal(HttpStatusCode.Forbidden, refused.StatusCode);
    }

    [Fact]
    public async Task Rejecting_an_already_rejected_approval_is_a_bad_request()
    {
        await using var s = await C100Server.StartAsync();
        s.Hub.Approvals.Add(new ApprovalRequest
        {
            Id = "apr-x",
            WorkerId = C100Server.Session,
            SubmitterId = "someone",
            Command = "ls",
            CreatedAt = 1,
            ExpiresAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 3600,
        });
        using var admin = s.Http(s.AdminToken);
        Assert.Equal(HttpStatusCode.OK, (await admin.PostAsync("/api/approvals/apr-x/reject", null)).StatusCode);
        Assert.Equal(HttpStatusCode.BadRequest, (await admin.PostAsync("/api/approvals/apr-x/reject", null)).StatusCode);
    }

    [Fact]
    public async Task Session_routes_refuse_bad_ids_unowned_patches_and_vanished_status()
    {
        var inner = new InMemorySessionRegistry(new[] { new SessionDefinition { SessionId = C100Server.Session, Visibility = "public", Owner = "admin" } });
        var registry = new StatuslessRegistry(inner);
        await using var s = await C100Server.StartAsync(new C100Server.Options
        {
            Deps = (cfg, hub) =>
            {
                var d = C100Server.Default(cfg, hub);
                return new ServerDeps
                {
                    Hub = d.Hub, Auth = d.Auth, Authz = d.Authz, Config = d.Config, Registry = registry,
                    ApiKeys = d.ApiKeys, Profiles = d.Profiles, Version = d.Version,
                };
            },
        });
        using var op = s.Http(s.Token("op1", "operator"));
        Assert.Equal(HttpStatusCode.Forbidden, (await op.PatchAsync($"/api/sessions/{C100Server.Session}", C100Server.Json(new { display_name = "x" }))).StatusCode);
        Assert.Equal(HttpStatusCode.UnprocessableEntity, (await op.GetAsync("/api/sessions/bad%20id/events/stream")).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await op.GetAsync("/api/sessions/ghost/events/stream")).StatusCode);

        // The definition is readable but its status vanished (a concurrent delete).
        registry.HideStatus = true;
        using var admin = s.Http(s.AdminToken);
        Assert.Equal(HttpStatusCode.NotFound, (await admin.PostAsync($"/api/sessions/{C100Server.Session}/analyze", null)).StatusCode);

        Assert.Null(inner.PatchSession("ghost", "x", null, null));
        Assert.Equal("private", inner.PatchSession(C100Server.Session, null, "private", null)!.Visibility);
    }

    [Fact]
    public async Task Event_stream_unsubscribes_when_the_client_goes_away()
    {
        await using var s = await C100Server.StartAsync();
        using var admin = s.Http(s.AdminToken);
        using var resp = await admin.GetAsync($"/api/sessions/{C100Server.Session}/events/stream", HttpCompletionOption.ResponseHeadersRead);
        var body = await resp.Content.ReadAsStreamAsync();
        var buf = new byte[256];
        Assert.True(await body.ReadAsync(buf) > 0); // the immediate heartbeat
        Assert.Equal(1, s.Hub.EventBus.SubscriberCount(C100Server.Session));
        resp.Dispose();
        admin.CancelPendingRequests();
        var deadline = DateTime.UtcNow.AddSeconds(15);
        while (s.Hub.EventBus.SubscriberCount(C100Server.Session) != 0)
        {
            Assert.True(DateTime.UtcNow < deadline, "stream never unsubscribed");
            await Task.Delay(10);
        }
    }

    [Fact]
    public async Task Tunnels_are_scoped_to_their_owner_and_stale_invites_are_refused()
    {
        var store = new MemoryTunnelStore();
        await using var s = await C100Server.StartAsync(new C100Server.Options
        {
            Configure = c => c.Tunnel.CookieSamesite = "Strict",
            Deps = (cfg, hub) =>
            {
                var d = C100Server.Default(cfg, hub);
                return new ServerDeps
                {
                    Hub = d.Hub, Auth = d.Auth, Authz = d.Authz, Config = d.Config, Registry = d.Registry,
                    ApiKeys = d.ApiKeys, Profiles = d.Profiles, Version = d.Version, TunnelStore = store,
                };
            },
        });
        using var admin = s.Http(s.AdminToken);
        using var op = s.Http(s.Token("op1", "operator"));
        var adminTunnel = await C100Server.ReadJsonAsync(await admin.PostAsync("/api/tunnels", C100Server.Json(new { })));
        var adminId = adminTunnel.GetProperty("tunnel_id").GetString()!;
        var opTunnel = await C100Server.ReadJsonAsync(await op.PostAsync("/api/tunnels", C100Server.Json(new { })));
        var opId = opTunnel.GetProperty("tunnel_id").GetString()!;

        var listed = await C100Server.ReadJsonAsync(await op.GetAsync("/api/tunnels"));
        Assert.Equal(new[] { opId }, listed.EnumerateArray().Select(t => t.GetProperty("tunnel_id").GetString()));

        Assert.Equal(HttpStatusCode.Forbidden, (await op.DeleteAsync($"/api/tunnels/{adminId}/tokens")).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await op.PostAsync($"/api/tunnels/{adminId}/tokens/rotate", null)).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await op.PostAsync("/api/tunnels/ghost/tokens/rotate", null)).StatusCode);

        // Replace the token record under an outstanding invite: the invite is stale.
        var shareUrl = new Uri(adminTunnel.GetProperty("share_url").GetString()!);
        var record = store.GetToken(adminId)!;
        store.PutToken(adminId, new TokenRecord
        {
            WorkerTokenHash = record.WorkerTokenHash,
            ShareTokenHash = TunnelTokens.HashToken(TunnelTokens.GenerateToken()),
            ControlTokenHash = record.ControlTokenHash,
            CreatedAt = record.CreatedAt,
            ExpiresAt = record.ExpiresAt,
            TunnelType = record.TunnelType,
            SharePage = record.SharePage,
        });
        using var anon = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { BaseAddress = new Uri(s.BaseUrl) };
        var stale = await anon.GetAsync(shareUrl.PathAndQuery);
        Assert.Equal(HttpStatusCode.Forbidden, stale.StatusCode);

        // A fresh invite redirects with a SameSite=Strict cookie.
        var control = new Uri(opTunnel.GetProperty("control_url").GetString()!);
        var redirect = await anon.GetAsync(control.PathAndQuery);
        Assert.Equal(HttpStatusCode.Redirect, redirect.StatusCode);
        Assert.Contains("samesite=strict", string.Join(";", redirect.Headers.GetValues("Set-Cookie")), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task Webhook_routes_refuse_unknown_sessions_and_non_owners()
    {
        await using var s = await C100Server.StartAsync();
        using var admin = s.Http(s.AdminToken);
        using var op = s.Http(s.Token("op1", "operator"));
        Assert.Equal(HttpStatusCode.NotFound, (await admin.PostAsync("/api/sessions/ghost/webhooks", C100Server.Json(new { url = "http://127.0.0.1:9/x" }))).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await op.GetAsync($"/api/sessions/{C100Server.Session}/webhooks")).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await admin.DeleteAsync("/api/sessions/ghost/webhooks/wh1")).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await op.DeleteAsync($"/api/sessions/{C100Server.Session}/webhooks/wh1")).StatusCode);
    }

    private sealed class EchoWorker : IWorkerWs
    {
        public TermHub? Hub { get; set; }
        public string WorkerId { get; init; } = "";

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            Hub?.AppendEventData(WorkerId, "term", new Dictionary<string, object?> { ["data"] = "out:" + payload.Length });
            return Task.CompletedTask;
        }
    }

    [Fact]
    public async Task Fanout_reads_worker_output_and_only_the_creator_may_delete_or_grant()
    {
        await using var s = await C100Server.StartAsync();
        var worker = new EchoWorker { Hub = s.Hub, WorkerId = C100Server.Session };
        Assert.True(s.Hub.Conn.RegisterWorker(C100Server.Session, worker));
        using var admin = s.Http(s.AdminToken);
        var group = await C100Server.ReadJsonAsync(await admin.PostAsync("/api/fanout/groups", C100Server.Json(new
        {
            name = "g",
            worker_ids = new[] { C100Server.Session },
            quiesce_ms = 50,
        })));
        var gid = group.GetProperty("group_id").GetString()!;

        var sent = await C100Server.ReadJsonAsync(await admin.PostAsync($"/api/fanout/groups/{gid}/send", C100Server.Json(new { data = "ls\n", quiesce_ms = 50, max_response_ms = 5000 })));
        var row = sent.GetProperty("results").EnumerateArray().Single();
        Assert.StartsWith("out:", row.GetProperty("output_delta").GetString(), StringComparison.Ordinal);

        // A grantee can see the group but may neither delete it nor re-grant it.
        Assert.Equal(HttpStatusCode.NoContent, (await admin.PostAsync($"/api/fanout/groups/{gid}/grants", C100Server.Json(new { grantee = "admin2" }))).StatusCode);
        using var other = s.Http(s.Token("admin2", "admin"));
        Assert.Equal(HttpStatusCode.Forbidden, (await other.DeleteAsync($"/api/fanout/groups/{gid}")).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await other.PostAsync($"/api/fanout/groups/{gid}/grants", C100Server.Json(new { grantee = "x" }))).StatusCode);
    }

    /// <summary>Delegates to an in-memory registry but can pretend a status vanished.</summary>
    private sealed class StatuslessRegistry(InMemorySessionRegistry inner) : ISessionRegistry
    {
        public bool HideStatus { get; set; }
        public bool TryGetDefinition(string sessionId, out SessionDefinition definition) => inner.TryGetDefinition(sessionId, out definition);
        public bool TryGetStatus(string sessionId, out SessionStatus status)
        {
            if (HideStatus)
            {
                status = null!;
                return false;
            }

            return inner.TryGetStatus(sessionId, out status);
        }

        public IReadOnlyList<SessionItem> ListWithDefinitions() => inner.ListWithDefinitions();
        public SessionDefinition Upsert(SessionDefinition def) => inner.Upsert(def);
        public bool Delete(string sessionId) => inner.Delete(sessionId);
        public SessionStatus? StartSession(string sessionId) => inner.StartSession(sessionId);
        public SessionStatus? StopSession(string sessionId) => inner.StopSession(sessionId);
        public SessionStatus? RestartSession(string sessionId) => inner.RestartSession(sessionId);
        public SessionStatus? ClearSession(string sessionId) => inner.ClearSession(sessionId);
        public SessionStatus? SetMode(string sessionId, string inputMode) => inner.SetMode(sessionId, inputMode);
        public SessionStatus? PatchSession(string sessionId, string? displayName, string? visibility, IReadOnlyList<string>? tags) =>
            inner.PatchSession(sessionId, displayName, visibility, tags);
    }
}
