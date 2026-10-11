//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Text;
using System.Threading.Channels;
using Provide.Uterm.Connectors;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Shell;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>Transport-session waits and control watchers; ushell connector and cast arms.</summary>
public sealed class Coverage100SessionShellTests
{
    private sealed class ChannelTransport : IConnectionTransport
    {
        private readonly Channel<byte[]> _in = Channel.CreateUnbounded<byte[]>();
        private bool _connected;

        public void Push(string text) => _in.Writer.TryWrite(Encoding.UTF8.GetBytes(text));

        public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            _connected = true;
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            _connected = false;
            _in.Writer.TryComplete();
            return Task.CompletedTask;
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default) => Task.CompletedTask;

        public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            cts.CancelAfter(timeout);
            try
            {
                if (await _in.Reader.WaitToReadAsync(cts.Token) && _in.Reader.TryRead(out var chunk))
                {
                    return chunk;
                }
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                // receive timeout: no data
            }

            return Array.Empty<byte>();
        }

        public bool IsConnected() => _connected;
    }

    private static async Task<(TransportSession Session, ChannelTransport Transport)> ConnectedAsync(bool controlFrames = false)
    {
        var t = new ChannelTransport();
        var session = new TransportSession(t, ct => t.ConnectAsync("", 0, null, ct), new TransportSessionOptions { ControlFrames = controlFrames });
        await session.ConnectAsync();
        return (session, t);
    }

    [Fact]
    public async Task Screen_change_waits_return_true_once_the_screen_moves()
    {
        var (session, t) = await ConnectedAsync();
        await using (session)
        {
            // since >= 0: the change sequence advances past it.
            var since = session.ScreenChangeSeq();
            var bySeq = session.WaitForScreenChange(TimeSpan.FromSeconds(10), since);
            t.Push("a");
            Assert.True(await bySeq);

            // since < 0: the next change, whenever it lands relative to the wait.
            var any = session.WaitForScreenChange(TimeSpan.FromSeconds(10));
            var i = 0;
            while (!any.IsCompleted)
            {
                t.Push("b" + i++);
                await Task.Delay(10);
            }

            Assert.True(await any);
        }
    }

    [Fact]
    public async Task Async_waits_complete_once_an_update_and_a_screen_change_land()
    {
        var (session, t) = await ConnectedAsync();
        await using (session)
        {
            var update = session.WaitForUpdateAsync(TimeSpan.FromSeconds(10));
            var change = session.WaitForScreenChangeAsync(TimeSpan.FromSeconds(10));
            t.Push("z");
            await update.WaitAsync(TimeSpan.FromSeconds(10));
            await change.WaitAsync(TimeSpan.FromSeconds(10));
            Assert.Contains("z", session.Snapshot().Screen, StringComparison.Ordinal);
        }
    }

    [Fact]
    public async Task A_throwing_control_watcher_does_not_stop_the_others()
    {
        var (session, t) = await ConnectedAsync(controlFrames: true);
        await using (session)
        {
            var seen = new TaskCompletionSource<string?>(TaskCreationOptions.RunContinuationsAsynchronously);
            session.AddControlFrameWatch(_ => throw new InvalidOperationException("watcher boom"));
            session.AddControlFrameWatch(p => seen.TrySetResult(p.GetValueOrDefault("type")?.ToString()));
            t.Push(ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?> { ["type"] = "ping" }));
            Assert.Equal("ping", await seen.Task.WaitAsync(TimeSpan.FromSeconds(10)));
        }
    }

    private sealed class CastHandler(string body) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(body) });
    }

    private static Task<string> CastAsync(string castBody, string flags = "") =>
        Run(castBody, flags);

    private static async Task<string> Run(string castBody, string flags)
    {
        using var http = new HttpClient(new CastHandler(castBody));
        var result = await new CommandDispatcher(client: http).DispatchAsync("cast " + flags + "http://example.test/c.cast");
        return result.Animated is null ? string.Join("", result.Text) : string.Join("|", result.Animated.Frames);
    }

    [Fact]
    public async Task Cast_clamps_unordered_events_and_reports_undisplayable_and_bad_headers()
    {
        // The last line is not the latest: the 5 s event clamps into the final frame.
        var clamped = await CastAsync("{\"version\":2}\n[5.0,\"o\",\"late\"]\n[0.0,\"o\",\"first\"]\n");
        Assert.Contains("late", clamped, StringComparison.Ordinal);

        Assert.Contains("no displayable output", await CastAsync("{\"version\":2}\n[0.0,\"o\",\"\"]\n"), StringComparison.Ordinal);
        Assert.Contains("no output events", await CastAsync("{\"version\":2}\n[true,\"o\",\"x\"]\n"), StringComparison.Ordinal);
        Assert.Contains("version: None", await CastAsync("{\"version\":null}\n"), StringComparison.Ordinal);
        Assert.Contains("version: 2.5", await CastAsync("{\"version\":2.5}\n"), StringComparison.Ordinal);
        Assert.Contains("version: True", await CastAsync("{\"version\":true}\n"), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task Ushell_connector_streams_an_animation_and_ends_it_with_a_prompt()
    {
        using var http = new HttpClient(new CastHandler("{\"version\":2}\n[0.0,\"o\",\"x\"]\n[0.002,\"o\",\"y\"]\n"));
        var connector = new UshellConnector("c100-shell", new UshellConnectorConfig { HttpClient = http, PollDelay = TimeSpan.FromMilliseconds(1) });
        Assert.Equal("c100-shell", connector.SessionId);
        IConnector ic = connector;
        Assert.Throws<ArgumentException>(() => ic.SetMode("sideways"));
        connector.Start();
        Assert.Equal(2, ic.Events().Count); // welcome
        Assert.Empty(ic.Events()); // nothing pending
        Assert.Empty(connector.PollMessages()); // idle poll sleeps the (default) poll delay

        await ic.HandleInputAsync("cast --fps 1000 http://example.test/c.cast\r");
        var frames = new List<string>();
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (!frames.Any(f => f.EndsWith(ShellOutput.Prompt, StringComparison.Ordinal) && f.Length == ShellOutput.Prompt.Length))
        {
            Assert.True(DateTime.UtcNow < deadline, "animation never finished");
            frames.AddRange(ic.Events().Select(e => e.GetValueOrDefault("data")?.ToString() ?? ""));
            await Task.Delay(5);
        }

        Assert.Contains("y", frames);
        connector.Stop();
    }
}
