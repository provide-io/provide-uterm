//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Sockets;
using System.Text.Json;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Tests;

/// <summary>
/// The typed close held to <c>close_cases</c> in spec/behavior_vectors.json, the fixture
/// the Python, TypeScript and Go ports are tested against too (issue #102). It is
/// generated from the Python reference by scripts/generate_behavior_vectors.py; the copy
/// under testdata/behavior is kept byte-identical by scripts/check_protocol_drift.py.
/// Port-specific cases stay in <see cref="TermSessionTransportCloseTests"/>.
/// </summary>
public class TermSessionTransportCloseVectorTests
{
    /// <summary>
    /// Vectors for behaviour this port does not have, keyed "&lt;group&gt;/&lt;name&gt;", with why.
    /// They are skipped by name rather than silently, and
    /// <see cref="EverySkippedVectorExistsAndSaysWhy"/> fails once a skip no longer
    /// matches a vector, so the list cannot outlive the gap it records.
    /// </summary>
    public static readonly IReadOnlyDictionary<string, string> NotPorted = new Dictionary<string, string>
    {
        ["telnet/rx-buffer-cap"] = "the C# TelnetTransport strips IAC per chunk and keeps no receive buffer to cap",
        ["chaos/injected-disconnect"] = "the C# port has no chaos transport",
    };

    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    private static readonly JsonElement Cases = LoadCloseCases();

    private static JsonElement LoadCloseCases()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "testdata", "behavior", "behavior_vectors.json");
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        return doc.RootElement.GetProperty("close_cases").Clone();
    }

    private static IEnumerable<JsonElement> Group(string group) => Cases.GetProperty(group).EnumerateArray();

    private static JsonElement Named(string group, string name) =>
        Group(group).Single(v => v.GetProperty("name").GetString() == name);

    private static TheoryData<string> PortedNames(string group)
    {
        var data = new TheoryData<string>();
        foreach (var vector in Group(group))
        {
            var name = vector.GetProperty("name").GetString()!;
            if (!NotPorted.ContainsKey($"{group}/{name}"))
            {
                data.Add(name);
            }
        }

        return data;
    }

    private static CloseInitiator Initiator(JsonElement vector) => vector.GetProperty("initiator").GetString() switch
    {
        "local" => CloseInitiator.Local,
        "remote" => CloseInitiator.Remote,
        "unknown" => CloseInitiator.Unknown,
        var other => throw new InvalidDataException($"unknown initiator {other}"),
    };

    private static int? Code(JsonElement vector) =>
        vector.GetProperty("code").ValueKind == JsonValueKind.Null ? null : vector.GetProperty("code").GetInt32();

    private static void AssertClose(JsonElement vector, TransportClose close)
    {
        Assert.Equal(Initiator(vector), close.Initiator);
        Assert.Equal(Code(vector), close.Code);
        Assert.Equal(vector.GetProperty("reason").GetString(), close.Reason);
    }

    [Fact]
    public void EverySkippedVectorExistsAndSaysWhy()
    {
        foreach (var (key, reason) in NotPorted)
        {
            var group = key[..key.IndexOf('/', StringComparison.Ordinal)];
            var name = key[(group.Length + 1)..];
            Assert.Single(Group(group), v => v.GetProperty("name").GetString() == name);
            Assert.False(string.IsNullOrWhiteSpace(reason));
        }
    }

    public static TheoryData<string> Summaries()
    {
        var data = new TheoryData<string>();
        foreach (var vector in Group("summary"))
        {
            data.Add(vector.GetProperty("summary").GetString()!);
        }

        return data;
    }

    [Theory]
    [MemberData(nameof(Summaries))]
    public void TheSummaryMatchesTheVector(string summary)
    {
        var vector = Group("summary").Single(v => v.GetProperty("summary").GetString() == summary);
        var close = new TransportClose(
            Initiator(vector),
            Code(vector),
            vector.GetProperty("reason").GetString()!,
            vector.GetProperty("detail").GetString()!);
        Assert.Equal(summary, close.Summary());
    }

    public static TheoryData<string> WebSocketCases() => PortedNames("websocket");

    [Theory]
    [MemberData(nameof(WebSocketCases))]
    public void AWebSocketCloseIsAttributedAsTheVectorSays(string name)
    {
        var vector = Named("websocket", name);
        static CloseFrame? Frame(JsonElement value) => value.ValueKind == JsonValueKind.Null
            ? null
            : new CloseFrame(value.GetProperty("code").GetInt32(), value.GetProperty("reason").GetString()!);
        var order = vector.GetProperty("received_then_sent");
        var detail = vector.GetProperty("detail").GetString()!;

        // Only two frames have an order: a null order is false here.
        var close = TransportClose.FromWebSocketFrames(
            Frame(vector.GetProperty("received")),
            Frame(vector.GetProperty("sent")),
            order.ValueKind == JsonValueKind.True,
            detail);

        AssertClose(vector, close);
        Assert.Equal(detail, close.Detail);
    }

    public static TheoryData<string> TelnetCases() => PortedNames("telnet");

    [Theory]
    [MemberData(nameof(TelnetCases))]
    public async Task ATelnetCloseIsAttributedAsTheVectorSays(string name)
    {
        var vector = Named("telnet", name);
        var close = vector.GetProperty("event").GetString() switch
        {
            "eof" => await EndOfStreamClose(),
            // Receive and send both hand the stream's IOException to FromSocketError.
            "reset" => TransportClose.FromSocketError(StreamError(SocketError.ConnectionReset)),
            "broken_pipe" => TransportClose.FromSocketError(StreamError(SocketError.Shutdown)),
            var other => throw new InvalidDataException($"no telnet driver for {other}"),
        };
        AssertClose(vector, close);
    }

    private static IOException StreamError(SocketError error) =>
        new("stream failed", new SocketException((int)error));

    private static async Task<TransportClose> EndOfStreamClose()
    {
        var (transport, server) = await TermSessionTransportCloseLiveTests.ConnectTelnet(s =>
        {
            s.Shutdown(SocketShutdown.Both);
            s.Close();
        });
        await server;
        var err = await Assert.ThrowsAsync<TransportClosedException>(() => transport.ReceiveAsync(1024, Wait));
        return err.Close;
    }

    public static TheoryData<string> ChaosCases() => PortedNames("chaos");

    [Fact]
    public void EveryChaosVectorIsSkippedByName() =>
        // No chaos transport, so every chaos vector must be a named skip, not a silent one.
        Assert.Empty(ChaosCases());
}
