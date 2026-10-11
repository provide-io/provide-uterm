//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Buffers.Binary;
using System.Net.Sockets;
using Provide.Uterm.Gui;
using Provide.Uterm.Vnc;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// The RFB client against <see cref="FakeRfbServer"/>: each handshake refusal,
/// the framebuffer-update loop and every way it ends, and input injection.
/// </summary>
public sealed class Coverage100RfbClientTests
{
    private static async Task<string> RefusalAsync(FakeRfbServer server)
    {
        await using (server)
        {
            await using var client = new RfbClient();
            var ex = await Assert.ThrowsAnyAsync<Exception>(() => client.ConnectAsync("127.0.0.1", server.Port));
            Assert.False(client.IsConnected);
            return ex.Message;
        }
    }

    [Fact]
    public async Task Handshake_refusals_name_what_the_server_got_wrong()
    {
        Assert.Contains("no types", await RefusalAsync(new FakeRfbServer { SecurityTypes = Array.Empty<byte>() }), StringComparison.Ordinal);
        Assert.Contains("does not offer security type None", await RefusalAsync(new FakeRfbServer { SecurityTypes = new byte[] { 2 } }), StringComparison.Ordinal);
        Assert.Contains("security rejected", await RefusalAsync(new FakeRfbServer { SecurityResult = 1 }), StringComparison.Ordinal);
        Assert.Contains("unsupported RFB security type 2", await RefusalAsync(new FakeRfbServer { Version = "RFB 003.003\n", LegacySecurityType = 2 }), StringComparison.Ordinal);
        Assert.Contains("dimensions out of range: 0x2", await RefusalAsync(new FakeRfbServer { Width = 0 }), StringComparison.Ordinal);
        Assert.Contains("desktop name too long", await RefusalAsync(new FakeRfbServer { NameLength = 5000 }), StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_non_rfb_greeting_falls_back_to_3_3()
    {
        await using var server = new FakeRfbServer { Version = "HELLO WORLD\n" };
        await using var client = new RfbClient();
        await client.ConnectAsync("127.0.0.1", server.Port);
        Assert.True(client.IsConnected);
        Assert.Equal((4, 2), (client.Width, client.Height));
    }

    [Fact]
    public async Task Updates_paint_the_framebuffer_and_input_reaches_the_server()
    {
        var pixel = new byte[] { 0x11, 0x22, 0x33, 0 };
        var input = new TaskCompletionSource<byte[]>(TaskCreationOptions.RunContinuationsAsynchronously);
        var requested = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var server = new FakeRfbServer
        {
            NameLength = 0,
            AfterInit = async (s, ct) =>
            {
                await s.WriteAsync(FakeRfbServer.FramebufferUpdate(
                    (1, 0, 1, 1, RfbClient.EncodingRaw, pixel),
                    (0, 0, 1, 1, RfbClient.EncodingCopyRect, new byte[4])), ct);
                var req = await FakeRfbServer.ReadAsync(s, 10, ct); // the incremental request that follows
                Assert.Equal(new byte[] { 3, 1 }, req[..2]);
                requested.TrySetResult();
                input.TrySetResult(await FakeRfbServer.ReadAsync(s, 6 + 8, ct));
                await FakeRfbServer.ReadAsync(s, 1, ct); // hold until the client goes
            },
        };

        var client = new RfbClient();
        client.InjectPointer(1, 1, 1); // not connected: dropped
        client.InjectKey(0x61, true);
        await client.ConnectAsync("127.0.0.1", server.Port);
        await requested.Task.WaitAsync(TimeSpan.FromSeconds(10));
        var shot = client.Screenshot();
        Assert.Equal((4, 2), (shot.Width, shot.Height));
        Assert.NotEqual(new RgbaImage(4, 2).Pixels, shot.Pixels);

        client.InjectPointer(70000, -5, 1);
        client.InjectKey(0x61, false);
        var sent = await input.Task.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(RfbClient.EncodePointerEvent(65535, 0, 1), sent[..6]);
        Assert.Equal(RfbClient.EncodeKeyEvent(0x61, false), sent[6..]);

        // Disposing while the loop waits for the next message cancels it.
        await client.DisposeAsync();
        Assert.False(client.IsConnected);
        Assert.Equal((1, 1), (client.Screenshot().Width, client.Screenshot().Height));
        client.InjectKey(0x61, true); // disconnected: dropped
    }

    private static async Task LoopEndsOnAsync(byte[] message)
    {
        await using var server = new FakeRfbServer
        {
            AfterInit = async (s, ct) =>
            {
                await s.WriteAsync(message, ct);
                await FakeRfbServer.ReadAsync(s, 1, ct); // hold until the client goes
            },
        };
        await using var client = new RfbClient();
        await client.ConnectAsync("127.0.0.1", server.Port);
        await client.ReadLoop!.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.True(client.IsConnected); // the loop ends; the connection stays until disposed
    }

    private static byte[] Rect(ushort w, ushort h, int enc) => FakeRfbServer.FramebufferUpdate((0, 0, w, h, enc, Array.Empty<byte>()));

    [Fact]
    public async Task The_update_loop_stops_on_anything_it_cannot_parse()
    {
        await LoopEndsOnAsync(new byte[] { 2 }); // an unknown server message (Bell)
        var tooMany = new byte[4];
        BinaryPrimitives.WriteUInt16BigEndian(tooMany.AsSpan(2), 5000);
        await LoopEndsOnAsync(tooMany);
        await LoopEndsOnAsync(Rect(RfbClient.MaxDimension + 1, 1, RfbClient.EncodingRaw));
        await LoopEndsOnAsync(Rect(1, RfbClient.MaxDimension + 1, RfbClient.EncodingRaw));
        await LoopEndsOnAsync(Rect(1, 1, 7)); // unsupported encoding
    }

    [Fact]
    public async Task The_update_loop_ends_when_the_server_hangs_up()
    {
        await using var server = new FakeRfbServer(); // closes once the init exchange is done
        await using var client = new RfbClient();
        await client.ConnectAsync("127.0.0.1", server.Port);
        await client.ReadLoop!.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.True(client.IsConnected);
    }

    [Fact]
    public async Task Input_to_a_reset_connection_is_dropped()
    {
        await using var server = new FakeRfbServer
        {
            AfterInit = (s, _) =>
            {
                s.Socket.LingerState = new LingerOption(true, 0);
                s.Socket.Close(); // RST, before the server's TcpClient would shut down with a FIN
                return Task.CompletedTask;
            },
        };
        await using var client = new RfbClient();
        await client.ConnectAsync("127.0.0.1", server.Port);
        // The loop sees the reset; writes into the dead socket then fail inside.
        await client.ReadLoop!.WaitAsync(TimeSpan.FromSeconds(10));
        client.InjectPointer(1, 1, 0);
        client.InjectKey(0x61, true);
        Assert.True(client.IsConnected);
    }
}
