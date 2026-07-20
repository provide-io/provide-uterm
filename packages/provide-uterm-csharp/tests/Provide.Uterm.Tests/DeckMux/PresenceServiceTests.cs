//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.DeckMux;

namespace Provide.Uterm.Tests.DeckMuxServices;

using DeckPresence = Provide.Uterm.DeckMux.DeckMuxPresence;

public sealed class PresenceServiceTests
{
    private sealed class CapturingBroadcaster : IDeckMuxBroadcaster
    {
        public List<(string WorkerId, Dictionary<string, object?> Msg)> Sent { get; } = new();

        public Task BroadcastAsync(string workerId, Dictionary<string, object?> msg, CancellationToken ct = default)
        {
            Sent.Add((workerId, new Dictionary<string, object?>(msg)));
            return Task.CompletedTask;
        }
    }

    [Fact]
    public async Task OnConnect_SendsPresenceSyncWithSelf()
    {
        var hub = new CapturingBroadcaster();
        var d = new DeckPresence(hub);
        var ws = new object();
        var sync = await d.OnBrowserConnectAsync("w1", ws, "admin");
        Assert.Equal("presence_sync", sync["type"]);
        var users = Assert.IsType<List<object>>(sync["users"]);
        Assert.Single(users);
        Assert.Empty(hub.Sent); // no fan-out when alone
    }

    [Fact]
    public async Task SecondConnect_BroadcastsSync()
    {
        var hub = new CapturingBroadcaster();
        var d = new DeckPresence(hub);
        _ = await d.OnBrowserConnectAsync("w1", new object(), "admin");
        _ = await d.OnBrowserConnectAsync("w1", new object(), "viewer");
        Assert.NotEmpty(hub.Sent);
        Assert.Equal("presence_sync", hub.Sent[^1].Msg["type"]);
    }

    [Fact]
    public async Task PresenceUpdate_Pin_BroadcastsUserDict()
    {
        var hub = new CapturingBroadcaster();
        var d = new DeckPresence(hub);
        var ws = new object();
        _ = await d.OnBrowserConnectAsync("w1", ws, "admin");
        hub.Sent.Clear();
        await d.HandleMessageAsync(
            "w1",
            ws,
            new Dictionary<string, object?>
            {
                ["type"] = "presence_update",
                ["scroll_line"] = 42,
                ["pin"] = new Dictionary<string, object?> { ["line"] = 17, ["label"] = "important" },
            });
        Assert.Single(hub.Sent);
        var msg = hub.Sent[0].Msg;
        Assert.Equal("presence_update", msg["type"]);
        Assert.Equal(42, msg["scroll_line"]);
        var pin = Assert.IsType<Dictionary<string, object?>>(msg["pin"]);
        Assert.Equal(17L, Convert.ToInt64(pin["line"]));
    }

    [Fact]
    public async Task ControlRequest_GrantsWhenNoOwner()
    {
        var hub = new CapturingBroadcaster();
        var d = new DeckPresence(hub);
        var ws = new object();
        _ = await d.OnBrowserConnectAsync("w1", ws, "admin");
        hub.Sent.Clear();
        await d.HandleMessageAsync("w1", ws, new Dictionary<string, object?> { ["type"] = "control_request" });
        Assert.Single(hub.Sent);
        Assert.Equal("control_transfer", hub.Sent[0].Msg["type"]);
        Assert.Equal("", hub.Sent[0].Msg["from_user_id"]);
        Assert.NotEqual("", hub.Sent[0].Msg["to_user_id"]?.ToString());
    }

    [Fact]
    public async Task ControlRequest_ReleaseWhenAlreadyOwner()
    {
        var hub = new CapturingBroadcaster();
        var d = new DeckPresence(hub);
        var ws = new object();
        _ = await d.OnBrowserConnectAsync("w1", ws, "admin");
        await d.HandleMessageAsync("w1", ws, new Dictionary<string, object?> { ["type"] = "control_request" });
        hub.Sent.Clear();
        await d.HandleMessageAsync("w1", ws, new Dictionary<string, object?> { ["type"] = "control_request" });
        Assert.Single(hub.Sent);
        Assert.Equal("control_transfer", hub.Sent[0].Msg["type"]);
        Assert.Equal("", hub.Sent[0].Msg["to_user_id"]?.ToString());
    }

    [Fact]
    public async Task Disconnect_BroadcastsPresenceLeave()
    {
        var hub = new CapturingBroadcaster();
        var d = new DeckPresence(hub);
        var a = new object();
        var b = new object();
        _ = await d.OnBrowserConnectAsync("w1", a, "admin");
        _ = await d.OnBrowserConnectAsync("w1", b, "viewer");
        hub.Sent.Clear();
        await d.OnBrowserDisconnectAsync("w1", b);
        Assert.Contains(hub.Sent, s => s.Msg["type"]?.ToString() == "presence_leave");
    }

    [Fact]
    public async Task PresenceUpdate_AllFields_AndStoreHelpers()
    {
        var hub = new CapturingBroadcaster();
        var d = new DeckPresence(hub);
        var ws = new object();
        _ = await d.OnBrowserConnectAsync("w1", ws, "admin");
        hub.Sent.Clear();

        await d.HandleMessageAsync(
            "w1",
            ws,
            new Dictionary<string, object?>
            {
                ["type"] = "presence_update",
                ["scroll_line"] = 1L,
                ["total_lines"] = 99.0,
                ["typing"] = true,
                ["cols"] = 120,
                ["rows"] = 40,
                ["selection"] = new Dictionary<string, object?> { ["start"] = 0, ["end"] = 2 },
                ["pin"] = new Dictionary<string, object?> { ["line"] = 5 },
                ["scroll_range"] = new Dictionary<string, object?> { ["from"] = 0, ["to"] = 10 },
            });
        Assert.NotEmpty(hub.Sent);

        // Control denied when another owner holds it.
        var ws2 = new object();
        _ = await d.OnBrowserConnectAsync("w1", ws2, "viewer");
        await d.HandleMessageAsync("w1", ws, new Dictionary<string, object?> { ["type"] = "control_request" });
        hub.Sent.Clear();
        await d.HandleMessageAsync("w1", ws2, new Dictionary<string, object?> { ["type"] = "control_request" });
        Assert.Empty(hub.Sent); // other owner → ignore

        // Disconnect unknown conn is a no-op.
        await d.OnBrowserDisconnectAsync("w1", new object());

        // ResolveOrBind preferred id reuse.
        var id1 = d.ResolveOrBindUserId(ws);
        var id2 = d.ResolveOrBindUserId(ws, "ignored");
        Assert.Equal(id1, id2);

        var store = d.GetStore("w1");
        Assert.True(store.TryGet(id1, out var user));
        Assert.NotNull(user);
        store.SetOwner(id1);
        Assert.NotNull(store.GetOwner());
        store.ClearOwner();
        Assert.Null(store.GetOwner());

        // Update unknown user → not ok.
        var (u, ok) = store.Update("nope", new Dictionary<string, object?> { ["typing"] = true });
        Assert.False(ok);
        Assert.Null(u);

        // JsonElement coercion paths (AsDict / CoerceInt).
        using var doc = System.Text.Json.JsonDocument.Parse(
            """{"line": 7, "label": "x", "flag": true, "n": null, "d": 1.5}""");
        var je = doc.RootElement;
        var (u2, ok2) = store.Update(id1, new Dictionary<string, object?>
        {
            ["scroll_line"] = System.Text.Json.JsonSerializer.SerializeToElement(3),
            ["pin"] = je,
            ["queued_keys"] = "abc",
        });
        Assert.True(ok2);
        Assert.NotNull(u2);
        Assert.Equal(3, u2!.ScrollLine);
        Assert.Equal("abc", u2.QueuedKeys);

        var (removed, wasRemoved) = store.Remove("missing");
        Assert.False(wasRemoved);
        Assert.Null(removed);

        // Sync payload with custom config.
        var sync = store.GetSyncPayload(new Dictionary<string, object?> { ["auto_transfer_idle_s"] = 5 });
        Assert.Equal("presence_sync", sync["type"]);
        Assert.NotNull(sync["config"]);
    }

    [Fact]
    public async Task PresenceUpdate_UnknownUser_NoBroadcast()
    {
        var hub = new CapturingBroadcaster();
        var d = new DeckPresence(hub);
        // Message before connect → ResolveOrBind creates id but store has no user yet → Update fails.
        var orphan = new object();
        hub.Sent.Clear();
        await d.HandleMessageAsync(
            "w-orphan",
            orphan,
            new Dictionary<string, object?> { ["type"] = "presence_update", ["scroll_line"] = 1 });
        Assert.Empty(hub.Sent);
    }
}
