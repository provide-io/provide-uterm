//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Embed;
using Xunit;

namespace Provide.Uterm.Tests.Embed;

public class DetachClientTests
{
    [Fact]
    public async Task Detach_MarksHandleUnattached_And_IsIdempotent()
    {
        var hub = new EmbedHub();
        var session = await hub.CreateSessionAsync(new EmbedSessionOptions { SessionId = "s1" });
        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);

        var handle = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata { ClientId = "c1" },
        });
        Assert.True(handle.IsAttached);

        await session.DetachClientAsync("c1");
        Assert.False(handle.IsAttached);

        // Re-attach under same id after detach.
        var handle2 = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata { ClientId = "c1" },
        });
        Assert.True(handle2.IsAttached);

        await session.DisposeAsync();
        Assert.False(handle2.IsAttached);
    }
}
