//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Tests.ControlChannel;

/// <summary>
/// The frame length header is an <em>unsigned</em> 32-bit wire value, and this
/// port used to accumulate it into a signed <c>int</c>. Headers from
/// <c>80000000</c> up therefore wrapped negative, slipped past a size guard that
/// only ever looks upward, and reached an index and a slice — throwing
/// <see cref="IndexOutOfRangeException"/> and
/// <see cref="ArgumentOutOfRangeException"/> where the reference reports
/// "control payload too large".
///
/// Thirteen bytes from a peer, and the exception type is one no caller catches:
/// the reference's invariant, which the weekly fuzz explorer asserts, is that
/// arbitrary input only ever produces the protocol error.
///
/// Found by the cross-language fuzz corpus once its generator was taught to
/// follow a high-bit header with ':' — before that, every high-bit header it
/// produced was followed by some other separator and was rejected as malformed
/// before its length was ever parsed. Pinned there as CCF-REG-0005; pinned here
/// as well, so the boundary is asserted directly and not only through a corpus
/// that could be regenerated.
/// </summary>
public sealed class ControlChannelLengthHeaderOverflowTests
{
    private static string Frame(string lengthHex, string payload = "{\"k\":1}") =>
        ControlChannelCodec.Dle + ControlChannelCodec.Stx + lengthHex + ":" + payload;

    [Theory]
    // The largest value a signed 32-bit accumulator still holds, and the first
    // that wraps it. Both are far above the payload ceiling, so both are "too
    // large" — the point is that they are *reported* as such rather than
    // reaching an index.
    [InlineData("7fffffff")]
    [InlineData("80000000")]
    [InlineData("80000001")]
    [InlineData("8cef96eb")]
    [InlineData("fffffffe")]
    [InlineData("ffffffff")]
    // Merely over the ceiling, overflowing nothing: the ordinary path, which
    // must stay reachable and must report the same thing.
    [InlineData("00100001")]
    public void ADeclaredLengthAboveTheCeilingIsReportedNotThrownRaw(string lengthHex)
    {
        var decoder = new ControlFrameDecoder();

        var error = Assert.Throws<ProtocolException>(() => decoder.Feed(Frame(lengthHex)));

        Assert.Equal("control payload too large", error.Message);
    }

    [Theory]
    [InlineData("7fffffff")]
    [InlineData("80000000")]
    [InlineData("ffffffff")]
    [InlineData("00100001")]
    public void ThePredicateAnswersFalseRatherThanThrowing(string lengthHex)
    {
        // IsControlFrame is a structural test. A predicate that throws on input
        // it is being asked to classify is unusable for the one job it has.
        Assert.False(ControlChannelCodec.IsControlFrame(Frame(lengthHex)));
    }

    [Fact]
    public void AHeaderAtTheCeilingIsStillAccepted()
    {
        // The guard is "> ceiling", so the ceiling itself must pass it. This is
        // the off-by-one a wider accumulator could have introduced.
        const int ceiling = 1_048_576;
        var payload = "{\"k\":\"" + new string('a', ceiling - 8) + "\"}";
        Assert.Equal(ceiling, System.Text.Encoding.UTF8.GetByteCount(payload));

        var decoder = new ControlFrameDecoder();
        var chunks = decoder.Feed(Frame($"{ceiling:x8}", payload));

        var chunk = Assert.Single(chunks);
        Assert.IsType<ControlChunk>(chunk);
    }

    [Fact]
    public void TheAccumulatorHoldsTheWholeUnsignedRange()
    {
        // Directly: the parse itself must not lose the high bit, whatever any
        // caller then does with the value.
        Assert.True(ControlChannelCodec.TryParseHex32("ffffffff", out var all));
        Assert.Equal(0xFFFFFFFFL, all);

        Assert.True(ControlChannelCodec.TryParseHex32("80000000", out var high));
        Assert.Equal(0x80000000L, high);

        Assert.True(ControlChannelCodec.TryParseHex32("7fffffff", out var positive));
        Assert.Equal(0x7FFFFFFFL, positive);
    }

    [Fact]
    public void UpperCaseHexIsAcceptedAsTheReferenceAcceptsIt()
    {
        // CPython's string.hexdigits spans both cases. The corpus now pairs
        // upper-case hex with ':' so this agreement is tested rather than
        // assumed, and it is asserted here too.
        var payload = "{\"k\":\"" + new string('a', 20) + "\"}";
        var byteCount = System.Text.Encoding.UTF8.GetByteCount(payload);
        var decoder = new ControlFrameDecoder();

        var chunks = decoder.Feed(Frame(byteCount.ToString("X8", System.Globalization.CultureInfo.InvariantCulture), payload));

        var chunk = Assert.Single(chunks);
        Assert.IsType<ControlChunk>(chunk);
    }
}
