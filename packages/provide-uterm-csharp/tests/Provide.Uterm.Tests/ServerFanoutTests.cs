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
