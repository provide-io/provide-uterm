//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.Shell;

namespace Provide.Uterm.Tests.Server;

/// <summary>
/// The in-process worker answering the control messages a WebSocket worker
/// would: <c>analyze_req</c> and <c>control</c> (reference:
/// <c>_process_control_msg</c>), and the hub's worker ceiling refusing it.
/// </summary>
public sealed class LocalWorkerLinkSessionControlTests
{
    private const string WorkerId = "local-control";

    private sealed class CapturingBrowser : IWorkerWs
    {
        private readonly object _gate = new();
        private readonly List<string> _payloads = [];

        public IReadOnlyList<Dictionary<string, object?>> Frames
        {
            get
            {
                lock (_gate)
                {
                    return _payloads
                        .SelectMany(p => new ControlFrameDecoder().Feed(p))
                        .OfType<ControlChunk>()
                        .Select(c => c.Control)
                        .ToList();
                }
            }
        }

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            lock (_gate) _payloads.Add(payload);
            return Task.CompletedTask;
        }
    }

    private static string Type(Dictionary<string, object?> frame) =>
        frame.TryGetValue("type", out var t) ? t?.ToString() ?? "" : "";

    private static async Task<(LocalWorkerLink Link, CapturingBrowser Browser)> AttachAsync()
    {
        var hub = new TermHub(new TermHubConfig());
        var connector = new UshellConnector(WorkerId, new UshellConnectorConfig { PollSleep = _ => { } });
        connector.Start();
        var link = new LocalWorkerLink(hub, WorkerId, connector, null);
        Assert.True(await link.AttachAsync(InputModes.Open));
        var browser = new CapturingBrowser();
        hub.Conn.RegisterBrowser(WorkerId, browser, "viewer");
        return (link, browser);
    }

    private static string Control(Dictionary<string, object?> message) =>
        ControlChannelCodec.EncodeControlFrame(message);

    [Fact]
    public async Task AnAnalyzeRequestIsAnsweredWithAnAnalysis()
    {
        var (link, browser) = await AttachAsync();

        await link.SendTextAsync(Control(new Dictionary<string, object?> { ["type"] = "analyze_req" }));

        var analysis = Assert.Single(browser.Frames, f => Type(f) == "analysis");
        Assert.IsType<string>(analysis["formatted"]);
        Assert.True(analysis.ContainsKey("ts"));
    }

    [Fact]
    public async Task AControlMessageIsHandedToTheConnector()
    {
        var (link, browser) = await AttachAsync();

        await link.SendTextAsync(Control(new Dictionary<string, object?>
        {
            ["type"] = "control",
            ["action"] = "snapshot_request",
        }));
        Assert.Single(browser.Frames, f => Type(f) == "snapshot");

        // No action is no request; an unknown message type gets no answer.
        await link.SendTextAsync(Control(new Dictionary<string, object?> { ["type"] = "control" }));
        await link.SendTextAsync(Control(new Dictionary<string, object?> { ["type"] = "control", ["action"] = null }));
        await link.SendTextAsync(Control(new Dictionary<string, object?> { ["type"] = "no_such_message" }));
        Assert.Single(browser.Frames, f => Type(f) == "snapshot");
    }

    [Fact]
    public async Task AWorkerOverTheHubsCeilingIsRefusedAndLeftInactive()
    {
        var hub = new TermHub(new TermHubConfig { MaxWorkers = 1 });
        var first = new LocalWorkerLink(hub, "first", new UshellConnector("first"), null);
        Assert.True(await first.AttachAsync(InputModes.Open));

        var second = new LocalWorkerLink(hub, "second", new UshellConnector("second"), null);
        Assert.False(await second.AttachAsync(InputModes.Open));

        Assert.False(second.IsActive);
        Assert.True(first.IsActive);
    }
}
