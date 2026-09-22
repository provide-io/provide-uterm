//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Text;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

public sealed class ServerFanoutTests
{
    [Fact]
    public async Task All_Routes_Require_Authenticated_Global_Admin_Before_Parse_Or_Lookup()
    {
        var cfg = UtermServerConfig.Default();
        await using var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(),
            Auth = new HeaderTestAuthenticator(),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(),
        });
        server.Build(["http://127.0.0.1:0"]);
        await server.StartAsync();
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        var callers = new[]
        {
            (Name: "anonymous", Subject: "", Role: "", Scope: "", Status: HttpStatusCode.Unauthorized),
            (Name: "viewer", Subject: "viewer1", Role: "viewer", Scope: "", Status: HttpStatusCode.Forbidden),
            (Name: "operator", Subject: "operator1", Role: "operator", Scope: "", Status: HttpStatusCode.Forbidden),
            (Name: "session-admin", Subject: "scoped1", Role: "admin", Scope: "w1", Status: HttpStatusCode.Forbidden),
        };
        var requests = new[]
        {
            (Method: HttpMethod.Post, Path: "/api/fanout/groups"),
            (Method: HttpMethod.Get, Path: "/api/fanout/groups"),
            (Method: HttpMethod.Delete, Path: "/api/fanout/groups/missing"),
            (Method: HttpMethod.Post, Path: "/api/fanout/groups/missing/send"),
            (Method: HttpMethod.Post, Path: "/api/fanout/groups/missing/grants"),
        };

        foreach (var caller in callers)
        {
            foreach (var requestCase in requests)
            {
                using var request = new HttpRequestMessage(requestCase.Method, requestCase.Path)
                {
                    Content = new StringContent("{not-json", Encoding.UTF8, "application/json"),
                };
                if (caller.Subject.Length > 0)
                {
                    request.Headers.Add("X-Test-Subject", caller.Subject);
                    request.Headers.Add("X-Test-Role", caller.Role);
                    if (caller.Scope.Length > 0) request.Headers.Add("X-Test-Admin-Scope", caller.Scope);
                }

                using var response = await http.SendAsync(request);
                Assert.True(response.StatusCode == caller.Status,
                    $"{caller.Name} {requestCase.Method} {requestCase.Path}: {(int)response.StatusCode}, want {(int)caller.Status}");
            }
        }
    }

    [Fact]
    public void Configuration_Defaults_Strict_And_Loads_Explicit_Permissive_Mode()
    {
        Assert.False(UtermServerConfig.Default().FanoutAllowUnknownMembers);
        var path = Path.Combine(Path.GetTempPath(), "uterm-fanout-" + Guid.NewGuid().ToString("N") + ".toml");
        try
        {
            File.WriteAllText(path, "fanout_allow_unknown_members = true\n");
            Assert.True(ConfigLoader.Load(path).FanoutAllowUnknownMembers);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public async Task Create_Refusals_Are_400_With_Their_Message()
    {
        await using var server = await StartAsync(fanout: null);
        using var http = AdminClient(server);

        var members = string.Join(",", Enumerable.Range(0, 51).Select(i => $"\"w{i}\""));
        var (status, body) = await PostCreateAsync(http, "{\"worker_ids\":[" + members + "]}");
        Assert.Equal(400, status);
        Assert.Equal("{\"error\":\"Group size 51 exceeds max 50\"}", body);

        (status, body) = await PostCreateAsync(http, "{\"worker_ids\":[\"w1\"],\"error_pattern\":\"(unterminated\"}");
        Assert.Equal(400, status);
        Assert.StartsWith("{\"error\":\"Invalid pattern", body);
    }

    [Fact]
    public async Task Create_Store_Fault_Is_A_Generic_500_Without_Its_Detail()
    {
        // A pluggable store's ArgumentException used to be echoed as a 400 like a
        // refusal; its text describes the server, not the request.
        var fanout = new Provide.Uterm.Fanout.Controller(null, new Provide.Uterm.Fanout.ControllerConfig
        {
            Store = new FailingStore(),
            Authorizer = new AllowAllAuthorizer(),
        });
        await using var server = await StartAsync(fanout);
        using var http = AdminClient(server);

        var (status, body) = await PostCreateAsync(http, "{\"worker_ids\":[\"w1\"]}");

        Assert.Equal(500, status);
        Assert.Equal("Internal Server Error", body);
        Assert.DoesNotContain("postgres", body);
        Assert.DoesNotContain("db.internal", body);
    }

    [Fact]
    public void Create_Failure_Echoes_Only_The_Refusal_Type()
    {
        Assert.IsType<Provide.Uterm.Fanout.FanoutGroupRejectedException>(Assert.ThrowsAny<ArgumentException>(() =>
            new Provide.Uterm.Fanout.Controller(null, new Provide.Uterm.Fanout.ControllerConfig { MaxGroupSize = 1 })
                .CreateGroup(new Provide.Uterm.Fanout.Group { WorkerIds = ["w1", "w2"] }, "admin")));
        Assert.IsType<Provide.Uterm.Fanout.FanoutGroupRejectedException>(Assert.ThrowsAny<ArgumentException>(() =>
            new Provide.Uterm.Fanout.Controller(null, new Provide.Uterm.Fanout.ControllerConfig())
                .CreateGroup(new Provide.Uterm.Fanout.Group { WorkerIds = ["w1"], ErrorPattern = new string('a', 201) }, "admin")));
    }

    private static async Task<UtermServer> StartAsync(Provide.Uterm.Fanout.Controller? fanout)
    {
        var cfg = UtermServerConfig.Default();
        cfg.FanoutAllowUnknownMembers = true;
        var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(),
            Auth = new HeaderTestAuthenticator(),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(),
            Fanout = fanout,
        });
        server.Build(["http://127.0.0.1:0"]);
        await server.StartAsync();
        return server;
    }

    private static HttpClient AdminClient(UtermServer server)
    {
        var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        http.DefaultRequestHeaders.Add("X-Test-Subject", "admin1");
        http.DefaultRequestHeaders.Add("X-Test-Role", "admin");
        return http;
    }

    private static async Task<(int Status, string Body)> PostCreateAsync(HttpClient http, string json)
    {
        using var response = await http.PostAsync("/api/fanout/groups",
            new StringContent(json, Encoding.UTF8, "application/json"));
        return ((int)response.StatusCode, await response.Content.ReadAsStringAsync());
    }

    private sealed class FailingStore : Provide.Uterm.Fanout.IGroupStore
    {
        public void Save(Provide.Uterm.Fanout.Group group) =>
            throw new ArgumentException("store write failed: postgres://svc@db.internal/fanout");

        public bool TryGet(string groupId, out Provide.Uterm.Fanout.Group group)
        {
            group = null!;
            return false;
        }

        public void Delete(string groupId)
        {
        }

        public bool GrantAccess(string groupId, string grantee, string principal) => false;

        public IReadOnlyList<Provide.Uterm.Fanout.Group> ListForPrincipal(string principal) => [];
    }

    private sealed class AllowAllAuthorizer : Provide.Uterm.Fanout.IFanoutAuthorizer
    {
        public bool IsGlobalAdmin(Principal principal) => true;

        public bool CanReadMember(Principal principal, string workerId) => true;
    }

    private sealed class HeaderTestAuthenticator : IAuthenticator
    {
        public Task<Principal> AuthenticateAsync(AuthRequest request, CancellationToken cancellationToken = default)
        {
            var subject = request.Header("X-Test-Subject");
            if (subject.Length == 0) return Task.FromResult(Principal.Anonymous());
            return Task.FromResult(new Principal
            {
                SubjectId = subject,
                Roles = StringSet.Of(request.Header("X-Test-Role")),
                Scopes = StringSet.Of("*"),
                AdminSessionScope = string.IsNullOrEmpty(request.Header("X-Test-Admin-Scope"))
                    ? null
                    : request.Header("X-Test-Admin-Scope"),
            });
        }
    }
}
