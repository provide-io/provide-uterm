//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

public sealed class ResumeOwnershipTests
{
    [Fact]
    public void DisconnectedCurrentOwnerCanRestoreSameOwnershipVersion()
    {
        var hub = HubWithWorker();
        var original = new Socket();
        var resumed = new Socket();
        hub.Conn.RegisterBrowser("w", original, "admin");
        hub.Conn.RegisterBrowser("w", resumed, "admin");
        Assert.True(hub.Lease.TryAcquireWs("w", original).Ok);

        var ownershipVersion = hub.Conn.CleanupBrowser("w", original);

        Assert.NotNull(ownershipVersion);
        Assert.True(hub.Lease.TryRestoreWsOwnership("w", resumed, ownershipVersion.Value));
        Assert.Same(resumed, hub.Registry.Get("w")!.HijackOwner);
    }

    [Fact]
    public async Task LaterOwnerPreventsOldTokenFromRestoringEvenAfterRelease()
    {
        var hub = HubWithWorker();
        var original = new Socket();
        var later = new Socket();
        var resumed = new Socket();
        hub.Conn.RegisterBrowser("w", original, "admin");
        hub.Conn.RegisterBrowser("w", later, "admin");
        hub.Conn.RegisterBrowser("w", resumed, "admin");
        Assert.True(hub.Lease.TryAcquireWs("w", original).Ok);
        var oldVersion = hub.Conn.CleanupBrowser("w", original)!.Value;

        Assert.True(hub.Lease.TryAcquireWs("w", later).Ok);
        Assert.True((await hub.Lease.TryReleaseWsAsync("w", later)).Released);

        Assert.False(hub.Lease.TryRestoreWsOwnership("w", resumed, oldVersion));
        Assert.Null(hub.Registry.Get("w")!.HijackOwner);
    }

    [Fact]
    public async Task LaterRestOwnerPreventsOldDashboardTokenFromRestoringAfterRelease()
    {
        var hub = HubWithWorker();
        var original = new Socket();
        var resumed = new Socket();
        hub.Conn.RegisterBrowser("w", original, "admin");
        hub.Conn.RegisterBrowser("w", resumed, "admin");
        Assert.True(hub.Lease.TryAcquireWs("w", original).Ok);
        var oldVersion = hub.Conn.CleanupBrowser("w", original)!.Value;

        var (acquired, reason) = await hub.TryAcquireRestHijackAsync(
            "w", "rest-owner", 30, "rest-hijack", 10);
        Assert.True(acquired, reason);
        var restVersion = hub.Registry.Get("w")!.HijackOwnershipVersion;
        Assert.Equal(oldVersion + 1, restVersion);
        Assert.NotNull(hub.ExtendHijackLease("w", "rest-hijack", "rest-owner", 30, 11));
        Assert.Equal(restVersion, hub.Registry.Get("w")!.HijackOwnershipVersion);
        Assert.True((await hub.ReleaseRestHijackAsync("w", "rest-hijack")).Released);
        Assert.Equal(restVersion, hub.Registry.Get("w")!.HijackOwnershipVersion);

        Assert.False(hub.Lease.TryRestoreWsOwnership("w", resumed, oldVersion));
        Assert.Null(hub.Registry.Get("w")!.HijackOwner);
    }

    [Fact]
    public void NonOwnerDisconnectHasNoRestorableOwnership()
    {
        var hub = HubWithWorker();
        var browser = new Socket();
        hub.Conn.RegisterBrowser("w", browser, "viewer");

        Assert.Null(hub.Conn.CleanupBrowser("w", browser));
    }

    private static TermHub HubWithWorker()
    {
        var hub = new TermHub();
        hub.Conn.RegisterWorker("w", new Socket());
        return hub;
    }

    private sealed class Socket : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;
    }
}
