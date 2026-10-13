//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

/// <summary>
/// <see cref="ConnectionManager.BrowsersOf"/> is how the viewer-revocation route
/// finds the sockets to close: a snapshot of a worker's browsers with their roles.
/// </summary>
public sealed class BrowsersOfTests
{
    [Fact]
    public void Lists_a_workers_browsers_with_their_roles()
    {
        var hub = new TermHub(new TermHubConfig { Clock = new RealClock() });
        var st = new WorkerTermState();
        var viewer = new object();
        var admin = new object();
        st.Browsers[viewer] = "viewer";
        st.Browsers[admin] = "admin";
        hub.Registry.Put("w1", st);

        var browsers = hub.Conn.BrowsersOf("w1");

        Assert.Equal(2, browsers.Count);
        Assert.Contains((viewer, "viewer"), browsers);
        Assert.Contains((admin, "admin"), browsers);
        // A snapshot: changing the hub afterwards does not change it.
        st.Browsers.Remove(admin);
        Assert.Equal(2, browsers.Count);
    }

    [Fact]
    public void A_worker_with_no_state_has_no_browsers()
    {
        var hub = new TermHub(new TermHubConfig { Clock = new RealClock() });
        Assert.Empty(hub.Conn.BrowsersOf("missing"));
    }
}
