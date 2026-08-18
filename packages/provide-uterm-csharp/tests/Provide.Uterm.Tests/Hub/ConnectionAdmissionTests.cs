//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

public sealed class ConnectionAdmissionTests
{
    [Fact]
    public void BrowserQuota_RejectsAtomicallyAndCleanupReleasesExactlyOneSlot()
    {
        var hub = new TermHub(new TermHubConfig { MaxConnectionsPerPrincipal = 1 });
        var first = new CaptureSocket();
        var rejected = new CaptureSocket();

        hub.Conn.RegisterBrowser("w", first, "viewer", principalSubjectId: "alice");
        var error = Assert.Throws<BrowserRegistrationException>(() =>
            hub.Conn.RegisterBrowser("w", rejected, "viewer", principalSubjectId: "alice"));

        Assert.Equal(1008, error.CloseCode);
        Assert.Equal("too many connections", error.Message);
        Assert.DoesNotContain(rejected, hub.Registry.Get("w")!.Browsers.Keys);

        hub.Conn.CleanupBrowser("w", first);
        hub.Conn.CleanupBrowser("w", first); // idempotent: no double decrement
        hub.Conn.RegisterBrowser("w", rejected, "viewer", principalSubjectId: "alice");
        Assert.Contains(rejected, hub.Registry.Get("w")!.Browsers.Keys);
    }

    [Theory]
    [InlineData("")]
    [InlineData("anonymous")]
    public void BrowserQuota_ExemptsOnlyReferenceAnonymousSubjects(string subjectId)
    {
        var hub = new TermHub(new TermHubConfig { MaxConnectionsPerPrincipal = 1 });
        hub.Conn.RegisterBrowser("w", new CaptureSocket(), "viewer", principalSubjectId: subjectId);
        hub.Conn.RegisterBrowser("w", new CaptureSocket(), "viewer", principalSubjectId: subjectId);
    }

    [Fact]
    public async Task DeferredBrowser_ReceivesNoBroadcastUntilActivated()
    {
        var hub = new TermHub();
        var pending = new CaptureSocket();
        var active = new CaptureSocket();
        hub.Conn.RegisterBrowser("w", pending, "viewer", deferBroadcast: true);
        hub.Conn.RegisterBrowser("w", active, "viewer");

        await hub.Conn.BroadcastToBrowsersAsync(
            "w", new Dictionary<string, object?> { ["type"] = "worker_connected" });

        Assert.Empty(pending.Messages);
        Assert.Single(active.Messages);

        await hub.Conn.ActivateBrowserBroadcastsAsync("w", pending);
        await hub.Conn.BroadcastToBrowsersAsync(
            "w", new Dictionary<string, object?> { ["type"] = "worker_connected" });
        Assert.Single(pending.Messages);
    }

    [Fact]
    public void CleanedUpSocketCannotBypassQuotaOrReacquireOwnership()
    {
        var hub = new TermHub(new TermHubConfig { MaxConnectionsPerPrincipal = 1 });
        var stale = new CaptureSocket();
        var replacement = new CaptureSocket();
        hub.Conn.RegisterWorker("w", new CaptureSocket());
        hub.Conn.RegisterBrowser("w", stale, "admin", principalSubjectId: "alice");

        hub.Conn.CleanupBrowser("w", stale);
        hub.Conn.RegisterBrowser("w", replacement, "admin", principalSubjectId: "alice");

        Assert.False(hub.Lease.TryAcquireWs("w", stale).Ok);
        Assert.False(hub.Lease.PrepareBrowserInput("w", stale));
        Assert.True(hub.Lease.TryAcquireWs("w", replacement).Ok);
    }

    private sealed class CaptureSocket : IWorkerWs
    {
        public List<string> Messages { get; } = new();

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            Messages.Add(payload);
            return Task.CompletedTask;
        }
    }
}
