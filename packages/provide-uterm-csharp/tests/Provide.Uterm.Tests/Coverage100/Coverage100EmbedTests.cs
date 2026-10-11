//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Embed;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// Embedded-session failure arms: a reader that faults, an upstream whose
/// disconnect throws, interceptor actions outside the enum, and client
/// handles whose queue is already closed.
/// </summary>
public sealed class Coverage100EmbedTests
{
    private sealed class FuncInterceptor(
        Func<InterceptContext, InterceptResult>? up = null,
        Func<InterceptContext, InterceptResult>? client = null) : IByteInterceptor
    {
        public ValueTask<InterceptResult> OnUpstreamAsync(InterceptContext context, CancellationToken cancellationToken = default) =>
            ValueTask.FromResult(up is null ? InterceptResult.Pass() : up(context));

        public ValueTask<InterceptResult> OnClientAsync(InterceptContext context, CancellationToken cancellationToken = default) =>
            ValueTask.FromResult(client is null ? InterceptResult.Pass() : client(context));
    }

    /// <summary>An upstream whose disconnect always fails.</summary>
    private sealed class FailingDisconnectUpstream : IUpstreamPipe
    {
        private readonly MemoryUpstream _inner = new();
        public bool IsConnected => _inner.IsConnected;
        public Task ConnectAsync(CancellationToken cancellationToken = default) => _inner.ConnectAsync(cancellationToken);
        public Task DisconnectAsync(CancellationToken cancellationToken = default) =>
            throw new InvalidOperationException("disconnect boom");
        public Task SendAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default) =>
            _inner.SendAsync(data, cancellationToken);
        public Task<byte[]> ReceiveAsync(CancellationToken cancellationToken = default) =>
            _inner.ReceiveAsync(cancellationToken);
        public ValueTask PushAsync(byte[] b) => _inner.PushFromRemoteAsync(b);
    }

    private static async Task WaitForAsync(Func<bool> cond)
    {
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(10);
        while (!cond())
        {
            Assert.True(DateTime.UtcNow < deadline, "condition not reached");
            await Task.Yield();
            await Task.Delay(1);
        }
    }

    [Fact]
    public async Task A_faulted_reader_and_a_throwing_disconnect_do_not_stop_replace_or_dispose()
    {
        var faulted = 0;
        var interceptor = new FuncInterceptor(up: _ =>
        {
            Interlocked.Increment(ref faulted);
            throw new InvalidOperationException("interceptor boom");
        });
        var session = new UtermSession("s-fault", new EmbedSessionOptions { SessionId = "s-fault", Interceptor = interceptor });
        var first = new FailingDisconnectUpstream();
        await session.ConnectUpstreamAsync(first);
        await first.PushAsync("x"u8.ToArray());
        await WaitForAsync(() => Volatile.Read(ref faulted) == 1);

        // Replace awaits the faulted reader and a disconnect that throws.
        var second = new FailingDisconnectUpstream();
        await session.ReplaceUpstreamAsync(second);
        Assert.Equal(SessionLifecycle.Connected, session.Lifecycle);

        await second.PushAsync("y"u8.ToArray());
        await WaitForAsync(() => Volatile.Read(ref faulted) == 2);
        await session.DisposeAsync();
        Assert.Equal(SessionLifecycle.Shutdown, session.Lifecycle);
    }

    [Fact]
    public async Task An_interceptor_cancelling_the_reader_ends_it_quietly()
    {
        var called = 0;
        var interceptor = new FuncInterceptor(up: _ =>
        {
            Interlocked.Increment(ref called);
            throw new OperationCanceledException();
        });
        var session = new UtermSession("s-oce", new EmbedSessionOptions { SessionId = "s-oce", Interceptor = interceptor });
        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);
        await up.PushFromRemoteAsync("z"u8.ToArray());
        await WaitForAsync(() => Volatile.Read(ref called) == 1);
        Assert.Equal(SessionLifecycle.Connected, session.Lifecycle);
        // Still Connected (not UpstreamLost): cancellation is a normal end.
        await session.DisposeAsync();
        Assert.Equal(SessionLifecycle.Shutdown, session.Lifecycle);
    }

    [Fact]
    public async Task Unknown_action_forwards_and_an_empty_replacement_forwards_nothing()
    {
        var calls = 0;
        var interceptor = new FuncInterceptor(client: _ => Interlocked.Increment(ref calls) == 1
            ? new InterceptResult { Action = (InterceptAction)99 }
            : new InterceptResult { Action = InterceptAction.Replace, Payload = null });
        var session = new UtermSession("s-act", new EmbedSessionOptions { SessionId = "s-act", Interceptor = interceptor });
        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);

        await session.SendToUpstreamAsync("A"u8.ToArray());
        await session.SendToUpstreamAsync("B"u8.ToArray());
        Assert.Single(up.Sent);
        Assert.Equal("A"u8.ToArray(), up.Sent[0]);
        await session.DisposeAsync();
    }

    [Fact]
    public async Task Client_handles_report_closed_queues_as_empty_reads()
    {
        var session = new UtermSession("s-cli", new EmbedSessionOptions { SessionId = "s-cli" });
        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);

        var meta = new ClientMetadata { ClientId = "c1", Backpressure = BackpressurePolicy.Disconnect, QueueCapacity = 1 };
        var handle = await session.AttachClientAsync(new ClientAttachOptions { Metadata = meta });
        Assert.Same(meta, handle.Metadata);

        // Fill the queue, then overflow it: the Disconnect policy completes the
        // queue without detaching the handle, so the next read after the
        // queued chunk sees a closed channel.
        await session.SendToClientsAsync("1"u8.ToArray());
        await session.SendToClientsAsync("2"u8.ToArray());
        Assert.Equal("1"u8.ToArray(), await handle.ReceiveAsync());
        Assert.Empty(await handle.ReceiveAsync());
        Assert.True(handle.IsAttached);

        // A disposed handle stays registered; deliveries to its completed queue
        // are refused, and reads short-circuit once detached.
        var h2 = await session.AttachClientAsync(new ClientAttachOptions { Metadata = new ClientMetadata { ClientId = "c2" } });
        await h2.DisposeAsync();
        await session.SendToClientsAsync("3"u8.ToArray());
        Assert.False(h2.IsAttached);
        Assert.Empty(await h2.ReceiveAsync());
        await session.DisposeAsync();
    }

    [Fact]
    public async Task Scripted_telnet_reports_subnegotiations_as_negotiation_wire_events()
    {
        var up = new ScriptedTelnetUpstream();
        var events = new List<(WireEventKind Kind, string Detail)>();
        up.OnWire = (kind, _, detail) =>
        {
            events.Add((kind, detail));
            return ValueTask.CompletedTask;
        };
        await up.ConnectAsync();
        // IAC SB TTYPE SEND IAC SE, then a byte of payload.
        await up.PushWireAsync(new byte[] { 255, 250, 24, 1, 255, 240, (byte)'k' });
        var payload = await up.ReceiveAsync();
        Assert.Equal("k"u8.ToArray(), payload);
        Assert.Contains((WireEventKind.Negotiation, "sb"), events);
        await up.DisconnectAsync();
    }
}
