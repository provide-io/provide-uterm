//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests.Hub;

/// <summary>
/// Frames broadcast while a browser is still starting up must not be lost.
///
/// A browser registers with deferBroadcast so its hello, hijack_state and
/// presence_sync arrive before anything else; until it is activated it is not
/// in the broadcast set at all, and what was broadcast meanwhile used to be
/// dropped. That is right for frames the startup sequence already carries and
/// wrong for the inspect channel, which has no replay: the browser builds that
/// list from nothing, so a dropped http_req is a row missing for the rest of
/// the session.
///
/// This is the port where it surfaced — multi-backend Playwright failing
/// "element(s) not found" on a row the worker demonstrably sent, because C#'s
/// pre-activation sequence is the longest and its window the widest.
/// Port of test_startup_broadcast_window.py.
/// </summary>
public sealed class StartupBroadcastWindowTests
{
    private const string WorkerId = "w1";

    private static Dictionary<string, object?> HttpReq(string id, string url) => new()
    {
        ["type"] = "http_req",
        ["id"] = id,
        ["method"] = "GET",
        ["url"] = url,
        ["_channel"] = "http",
    };

    private static (TermHub Hub, CaptureSocket Browser) Pending()
    {
        var hub = new TermHub(new TermHubConfig());
        hub.Conn.RegisterWorker(WorkerId, new CaptureSocket());
        var browser = new CaptureSocket();
        hub.Conn.RegisterBrowser(WorkerId, browser, "viewer", deferBroadcast: true);
        return (hub, browser);
    }

    private static List<string> UrlsSeen(CaptureSocket browser, params string[] urls) =>
        browser.Messages
            .SelectMany(message => urls.Where(url => message.Contains(url, StringComparison.Ordinal)))
            .ToList();

    [Fact]
    public async Task InspectFrameSentDuringStartupIsDeliveredOnActivation()
    {
        var (hub, browser) = Pending();

        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, HttpReq("r1", "/api/users"));
        Assert.Empty(browser.Messages);

        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        Assert.Single(UrlsSeen(browser, "/api/users"));
    }

    [Fact]
    public async Task BufferedInspectFramesKeepTheirOrder()
    {
        var (hub, browser) = Pending();

        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, HttpReq("r0", "/api/zero"));
        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, HttpReq("r1", "/api/one"));
        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, HttpReq("r2", "/api/two"));
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        Assert.Equal(
            new[] { "/api/zero", "/api/one", "/api/two" },
            UrlsSeen(browser, "/api/zero", "/api/one", "/api/two"));
    }

    [Fact]
    public async Task TerminalOutputFromTheWindowIsNotReplayed()
    {
        // The hello's initial_snapshot already covers it; replaying prints twice.
        var (hub, browser) = Pending();

        await hub.Conn.BroadcastToBrowsersAsync(
            WorkerId, new Dictionary<string, object?> { ["type"] = "term", ["data"] = "ls -la\r\n" });
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        Assert.Empty(browser.Messages);
    }

    [Fact]
    public async Task PresenceSyncFromTheWindowIsDeliveredOnActivation()
    {
        // The startup sequence sends each browser its own presence_sync, but it
        // is computed at that browser's OWN join, so it cannot carry a user who
        // arrives while the browser is still starting up. Dropping it left the
        // roster one user short until a later presence event corrected it.
        var (hub, browser) = Pending();

        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, new Dictionary<string, object?>
        {
            ["type"] = "presence_sync",
            ["users"] = new List<object?> { "a", "b" },
            ["config"] = new Dictionary<string, object?>(),
        });
        Assert.Empty(browser.Messages);

        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        Assert.Single(UrlsSeen(browser, "presence_sync"));
    }

    [Fact]
    public async Task PresenceLeaveFromTheWindowIsDeliveredOnActivation()
    {
        // Worse than a missed sync: a delta, so dropping it keeps a ghost user.
        var (hub, browser) = Pending();

        await hub.Conn.BroadcastToBrowsersAsync(
            WorkerId, new Dictionary<string, object?> { ["type"] = "presence_leave", ["user_id"] = "departed" });
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        Assert.Single(UrlsSeen(browser, "departed"));
    }

    [Fact]
    public async Task ControlTransferFromTheWindowIsDeliveredOnActivation()
    {
        // Who is driving is a delta too. The startup presence_sync stamps
        // is_owner as of this browser's join; nothing restates a handover that
        // happens inside the window.
        var (hub, browser) = Pending();

        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, new Dictionary<string, object?>
        {
            ["type"] = "control_transfer",
            ["from_user_id"] = "a",
            ["to_user_id"] = "b",
            ["reason"] = "handover",
        });
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        Assert.Single(UrlsSeen(browser, "control_transfer"));
    }

    [Fact]
    public async Task PresenceUpdateFromTheWindowIsNotReplayed()
    {
        // Transient per-user state the next update supersedes; staying dropped
        // is deliberate, not an oversight.
        var (hub, browser) = Pending();

        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, new Dictionary<string, object?>
        {
            ["type"] = "presence_update",
            ["user_id"] = "a",
            ["name"] = "A",
            ["color"] = "#fff",
            ["role"] = "viewer",
        });
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        Assert.Empty(browser.Messages);
    }

    [Fact]
    public async Task ActivatedBrowserReceivesInspectFramesDirectly()
    {
        var (hub, browser) = Pending();
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, HttpReq("r1", "/api/users"));

        Assert.Single(UrlsSeen(browser, "/api/users"));
    }

    [Fact]
    public async Task StartupBufferIsCappedRatherThanUnbounded()
    {
        // A browser that never activates must not be able to grow this forever.
        var (hub, browser) = Pending();

        for (var index = 0; index < ConnectionManager.StartupBufferMaxFrames + 25; index++)
        {
            await hub.Conn.BroadcastToBrowsersAsync(WorkerId, HttpReq("r", "/api/x"));
        }

        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        Assert.Equal(ConnectionManager.StartupBufferMaxFrames, browser.Messages.Count);
    }

    [Fact]
    public async Task DisconnectingBrowserDropsItsBacklog()
    {
        // Nothing will ever flush it, so holding it is a leak.
        var (hub, browser) = Pending();
        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, HttpReq("r1", "/api/users"));

        hub.Conn.CleanupBrowser(WorkerId, browser);
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        Assert.Empty(browser.Messages);
    }

    [Fact]
    public async Task SocketThatCannotTakeItsBacklogDropsIt()
    {
        // Pending is the right resting state for a socket that just failed a
        // write: the broadcast path skips it rather than retrying into a dead
        // connection, and the disconnect path clears both.
        var hub = new TermHub(new TermHubConfig());
        hub.Conn.RegisterWorker(WorkerId, new CaptureSocket());
        var browser = new FailingSocket();
        hub.Conn.RegisterBrowser(WorkerId, browser, "viewer", deferBroadcast: true);
        await hub.Conn.BroadcastToBrowsersAsync(WorkerId, HttpReq("r1", "/api/users"));

        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);

        // Nothing left to retry, and a second activate is a no-op rather than
        // a second doomed send.
        Assert.Equal(1, browser.Attempts);
        await hub.Conn.ActivateBrowserBroadcastsAsync(WorkerId, browser);
        Assert.Equal(1, browser.Attempts);
    }

    private sealed class FailingSocket : IWorkerWs
    {
        public int Attempts { get; private set; }

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            Attempts++;
            throw new InvalidOperationException("socket gone");
        }
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
