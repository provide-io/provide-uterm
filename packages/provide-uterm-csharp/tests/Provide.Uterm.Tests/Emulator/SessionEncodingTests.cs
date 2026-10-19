//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Emulator;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Tests.Emulator;

/// <summary>
/// conformance/snapshot/session_encoding.json: the reads a session's emulator is
/// fed, its receive encoding, and what the Python emulator holds after them. This
/// port's emulator holds the same, and a session decodes with its encoding.
/// </summary>
public class SessionEncodingTests
{
    private const string CorpusRelativePath = "conformance/snapshot/session_encoding.json";

    private static readonly JsonElement[] Records = LoadRecords();

    private static JsonElement[] LoadRecords()
    {
        var parts = CorpusRelativePath.Split('/');
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(new[] { dir.FullName }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return JsonDocument.Parse(File.ReadAllText(candidate)).RootElement.GetProperty("records").EnumerateArray().ToArray();
            }
        }

        throw new FileNotFoundException("session encoding corpus not found above " + AppContext.BaseDirectory, CorpusRelativePath);
    }

    public static TheoryData<int> RecordIndexes()
    {
        var data = new TheoryData<int>();
        for (var i = 0; i < Records.Length; i++)
        {
            data.Add(i);
        }

        return data;
    }

    [Fact]
    public void HasRecordsInBothEncodings()
    {
        Assert.True(Records.Length >= 10);
        Assert.Equal(new[] { "cp437", "utf-8" }, Records.Select(r => r.GetProperty("encoding").GetString()).Distinct().Order());
    }

    [Theory]
    [MemberData(nameof(RecordIndexes))]
    public void HoldsWhatTheReferenceHolds(int index)
    {
        var record = Records[index];
        var emulator = new TerminalEmulator(
            record.GetProperty("cols").GetInt32(),
            record.GetProperty("rows").GetInt32(),
            "",
            record.GetProperty("encoding").GetString() == "utf-8" ? ReceiveEncoding.Utf8 : ReceiveEncoding.Cp437);
        foreach (var read in record.GetProperty("reads").EnumerateArray())
        {
            emulator.Process(Convert.FromBase64String(read.GetString()!));
        }

        var snapshot = emulator.GetSnapshot();
        var cursor = record.GetProperty("cursor");
        Assert.Equal(record.GetProperty("screen").GetString(), snapshot.Screen);
        Assert.Equal((cursor.GetProperty("x").GetInt32(), cursor.GetProperty("y").GetInt32()), (snapshot.Cursor.X, snapshot.Cursor.Y));
        Assert.Equal(record.GetProperty("ansi_screen").GetString(), emulator.AnsiScreen());
    }

    [Fact]
    public void DecodesCp437ByDefaultAndKeepsAByteOrderMarkInUtf8()
    {
        var cp437 = new TerminalEmulator(4, 1);
        cp437.Process(new byte[] { 0xcd, 0xe4 });
        Assert.Equal("\u2550\u03a3  ", cp437.GetSnapshot().Screen);
        var utf8 = new TerminalEmulator(4, 1, "", ReceiveEncoding.Utf8);
        utf8.Process(new byte[] { 0xef, 0xbb, 0xbf, 0x61 });
        Assert.Equal("﻿a", utf8.RawTail);
    }

    /// <summary>Hands out queued reads, then idles.</summary>
    private sealed class Reads : IConnectionTransport
    {
        private readonly ConcurrentQueue<byte[]> _queue;

        public Reads(params byte[][] reads) => _queue = new ConcurrentQueue<byte[]>(reads);

        public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default) => Task.CompletedTask;

        public Task DisconnectAsync(CancellationToken cancellationToken = default) => Task.CompletedTask;

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default) => Task.CompletedTask;

        public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            if (_queue.TryDequeue(out var read))
            {
                return read;
            }

            await Task.Delay(5, cancellationToken);
            return Array.Empty<byte>();
        }

        public bool IsConnected() => true;
    }

    private static async Task<string> ScreenOnceItShows(TransportSessionOptions options, string expected, params byte[][] reads)
    {
        var transport = new Reads(reads);
        await using var session = new TransportSession(transport, ct => transport.ConnectAsync("", 0, null, ct), options);
        await session.ConnectAsync();
        for (var i = 0; i < 400 && !session.Snapshot().Screen.Contains(expected, StringComparison.Ordinal); i++)
        {
            await Task.Delay(5);
        }

        return session.Snapshot().Screen;
    }

    [Fact]
    public async Task AUtf8SessionJoinsACharacterSplitBetweenReads()
    {
        var data = Encoding.UTF8.GetBytes("中é!");
        var screen = await ScreenOnceItShows(
            new TransportSessionOptions { Cols = 10, Rows = 1, ReceiveEncoding = ReceiveEncoding.Utf8 },
            "中é!",
            data[..2],
            data[2..4],
            data[4..]);
        Assert.Equal("中é!      ", screen);
    }

    [Fact]
    public async Task AUtf8SessionWithControlFramesKeepsItsText()
    {
        var frame = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?> { ["type"] = "render_speed" }));
        var screen = await ScreenOnceItShows(
            new TransportSessionOptions { Cols = 10, Rows = 1, ControlFrames = true, ReceiveEncoding = ReceiveEncoding.Utf8 },
            "café",
            frame.Concat(Encoding.UTF8.GetBytes("café 中")).ToArray());
        Assert.Equal("café 中   ", screen);
    }
}
