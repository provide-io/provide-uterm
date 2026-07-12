//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Cli;
using Provide.Uterm.Hub;
using Provide.Uterm.Mcp;
using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Tests;

public class HubAndCliTests
{
    [Fact]
    public void Cli_Root_Lists_All_Commands()
    {
        Assert.Equal(
            new[] { "proxy", "listen", "share", "tunnel", "inspect", "watch", "audit", "server" },
            Root.Subcommands);

        using var sw = new StringWriter();
        var code = Root.Execute(new[] { "--help" }, sw, sw);
        Assert.Equal(0, code);
        var help = sw.ToString();
        foreach (var cmd in Root.Subcommands)
        {
            Assert.Contains(cmd, help, StringComparison.Ordinal);
        }
    }

    [Fact]
    public void Mcp_Registers_All_Tools()
    {
        var srv = new McpServer();
        foreach (var name in McpServer.AllToolNames)
        {
            Assert.Contains(name, srv.ToolNames);
        }

        Assert.True(McpServer.AllToolNames.Length >= 21);
    }

    [Fact]
    public async Task Hub_Lease_Acquire_Heartbeat_Release()
    {
        var clock = new ManualClock(wall: 1000);
        clock.SetMonotonic(10);
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var worker = new FakeWorker();
        Assert.True(hub.Conn.RegisterWorker("w1", worker));

        var (ok, reason) = await hub.TryAcquireRestHijackAsync("w1", "operator", 90, "hij-1", 10);
        Assert.True(ok, reason);
        Assert.True(worker.Sent.Count >= 1);

        clock.SetMonotonic(20);
        var exp = hub.ExtendHijackLease("w1", "hij-1", "operator", 90, 20);
        Assert.NotNull(exp);
        Assert.Equal(110, exp!.Value);

        var session = hub.GetRestSession("w1", "hij-1");
        Assert.NotNull(session);
        Assert.Equal("operator", session!.Owner);

        var (released, shouldResume) = hub.ReleaseRestHijack("w1", "hij-1");
        Assert.True(released);
        Assert.True(shouldResume);
        Assert.Null(hub.GetRestSession("w1", "hij-1"));
    }

    [Fact]
    public void Auth_Roles_And_Capabilities()
    {
        var authz = new AuthorizationService();
        var admin = new Principal { SubjectId = "a", Roles = StringSet.Of("admin"), Scopes = StringSet.Of("*") };
        Assert.True(authz.IsAdmin(admin));
        Assert.True(authz.HasCapability(admin, "session.control.hijack"));

        var viewer = new Principal { SubjectId = "v", Roles = StringSet.Of("viewer") };
        Assert.False(authz.HasCapability(viewer, "session.control.hijack"));
        Assert.True(authz.HasCapability(viewer, "session.read"));
    }

    [Fact]
    public void Approvals_Claim_Is_OneShot()
    {
        var store = new InMemoryApprovalStore(new ManualClock(1));
        store.Add(new ApprovalRequest
        {
            Id = "r1",
            WorkerId = "w",
            SubmitterId = "u",
            Command = "ls",
            CreatedAt = 1,
            ExpiresAt = 100,
        });
        Assert.True(store.Claim("r1", ApprovalStatus.Approved));
        Assert.False(store.Claim("r1", ApprovalStatus.Rejected));
    }

    private sealed class FakeWorker : IWorkerWs
    {
        public List<string> Sent { get; } = new();

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            Sent.Add(payload);
            return Task.CompletedTask;
        }
    }
}
