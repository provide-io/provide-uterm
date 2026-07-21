//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System;
using System.Threading.Tasks;
using Provide.Uterm.Server;
using Xunit;

namespace Provide.Uterm.Tests.Server;

public class EgressGuardTests
{
    [Fact]
    public async Task Metadata_Ip_Always_Blocked()
    {
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("169.254.169.254", blockPrivate: false));
    }

    [Fact]
    public async Task Loopback_Allowed_When_BlockPrivate_False()
    {
        await EgressGuard.AssertConnectorTargetAllowedAsync("127.0.0.1", blockPrivate: false);
    }

    [Fact]
    public async Task Loopback_Blocked_When_BlockPrivate_True()
    {
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            EgressGuard.AssertConnectorTargetAllowedAsync("127.0.0.1", blockPrivate: true));
    }
}
