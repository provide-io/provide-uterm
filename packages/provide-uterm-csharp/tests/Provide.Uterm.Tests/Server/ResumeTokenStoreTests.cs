//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;
using Provide.Uterm.Server;

namespace Provide.Uterm.Tests.Server;

public sealed class ResumeTokenStoreTests
{
    [Fact]
    public void Consume_IsSingleUse()
    {
        var store = Store(out _);
        var token = store.Mint("worker", "admin");

        var session = store.Consume(token);

        Assert.NotNull(session);
        Assert.Equal("worker", session.WorkerId);
        Assert.Equal("admin", session.Role);
        Assert.Null(store.Consume(token));
    }

    [Fact]
    public void TokensExpireAfterFiveMinutesAndConsumeSweepsAllExpiredEntries()
    {
        var store = Store(out var clock);
        var first = store.Mint("w1", "viewer");
        var second = store.Mint("w2", "viewer");
        clock.SetMonotonic(300);

        Assert.Null(store.Consume(first));
        Assert.Equal(0, store.Count);
        Assert.Null(store.Consume(second));
    }

    [Fact]
    public void MintSweepsExpiredEntries()
    {
        var store = Store(out var clock);
        store.Mint("old", "viewer");
        clock.SetMonotonic(301);

        var fresh = store.Mint("new", "viewer");

        Assert.Equal(1, store.Count);
        Assert.Equal("new", store.Consume(fresh)!.WorkerId);
    }

    [Fact]
    public void HardCapEvictsOldestExpiry()
    {
        var tokens = new Queue<string>(["first", "second", "third"]);
        var clock = new ManualClock();
        var store = new ResumeTokenStore(clock, capacity: 2, tokenGenerator: () => tokens.Dequeue());
        var first = store.Mint("w1", "viewer");
        clock.SetMonotonic(1);
        var second = store.Mint("w2", "viewer");
        clock.SetMonotonic(2);
        var third = store.Mint("w3", "viewer");

        Assert.Equal(2, store.Count);
        Assert.Null(store.Consume(first));
        Assert.Equal("w2", store.Consume(second)!.WorkerId);
        Assert.Equal("w3", store.Consume(third)!.WorkerId);
    }

    [Fact]
    public void OwnershipStateIsRecordedBeforeConsume()
    {
        var store = Store(out _);
        var token = store.Mint("w", "admin");

        Assert.True(store.MarkHijackOwner(token, ownershipVersion: 7));
        var session = store.Consume(token)!;

        Assert.True(session.WasHijackOwner);
        Assert.Equal(7, session.OwnershipVersion);
    }

    private static ResumeTokenStore Store(out ManualClock clock)
    {
        clock = new ManualClock();
        var next = 0;
        return new ResumeTokenStore(clock, tokenGenerator: () => "token-" + ++next);
    }
}
