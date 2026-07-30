//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Provide.Uterm.ControlChannel;
using Xunit;
using Xunit.Abstractions;

namespace Provide.Uterm.Tests.ControlChannel;

/// <summary>
/// Replays the cross-language differential fuzz corpus
/// (<c>conformance/fuzz/control_channel_fuzz.json</c>, schema
/// <c>provide-uterm/control-channel-fuzz/1</c>) against the C# codec.
///
/// See <c>conformance/fuzz/README.md</c> — that file is the normative contract.
/// Four families are asserted equal to the CPython reference recording:
/// <c>encode_data</c>, <c>encode_control</c>, <c>is_control_frame</c> and
/// <c>decode</c>, plus the permanently-numbered <c>regressions</c> (same shape as
/// <c>decode</c>). <c>serializer_divergences</c> is explicitly NOT asserted equal
/// across ports; this port pins its own bytes for those instead.
///
/// Every <c>decode</c> case is driven twice — once through the recorded chunk
/// boundaries and once with the whole stream in a single feed. The two drives are
/// not required to agree with each other (39 of 192 do not), because the decoder
/// flushes buffered terminal data before it has resolved a trailing DLE. Driving
/// only one of them would prove the port parses the same while saying nothing
/// about whether it buffers the same, which is exactly where a live desync lives.
/// </summary>
public class ControlChannelFuzzCorpusReplayTests
{
    private const int ExpectedEncodeData = 96;
    private const int ExpectedEncodeControl = 96;
    private const int ExpectedIsControlFrame = 128;
    private const int ExpectedDecode = 192;
    private const int ExpectedRegressions = 5;
    private const int ExpectedSerializerDivergences = 6;

    private readonly ITestOutputHelper _out;

    public ControlChannelFuzzCorpusReplayTests(ITestOutputHelper output) => _out = output;

    /// <summary>
    /// Refuse to run on an unrecognised schema, and pin the header the rest of
    /// the replay depends on. <see cref="FuzzCorpus.Root"/> throws on an unknown
    /// schema, so merely reaching an assertion here proves version 1.
    /// </summary>
    [Fact]
    public void CorpusHeaderIsTheSchemaThisPortUnderstands()
    {
        var root = FuzzCorpus.Root;
        _out.WriteLine($"corpus: {FuzzCorpus.SourcePath}");
        Assert.Equal(FuzzCorpus.Schema, root.GetProperty("schema").GetString());
        Assert.Equal(20260729, root.GetProperty("seed").GetInt32());

        var limits = root.GetProperty("limits");
        Assert.Equal(11, limits.GetProperty("header_bytes").GetInt32());
        Assert.Equal(1_048_576, limits.GetProperty("max_control_payload_bytes").GetInt32());
        Assert.Equal(32, limits.GetProperty("max_frame_depth").GetInt32());

        var total = ExpectedEncodeData + ExpectedEncodeControl + ExpectedIsControlFrame +
                    ExpectedDecode + ExpectedRegressions + ExpectedSerializerDivergences;
        Assert.Equal(523, total);
        foreach (var (family, expected) in new[]
                 {
                     ("encode_data", ExpectedEncodeData),
                     ("encode_control", ExpectedEncodeControl),
                     ("is_control_frame", ExpectedIsControlFrame),
                     ("decode", ExpectedDecode),
                     ("regressions", ExpectedRegressions),
                     ("serializer_divergences", ExpectedSerializerDivergences),
                 })
        {
            Assert.Equal(expected, FuzzCorpus.DeclaredCount(family));
            Assert.Equal(expected, root.GetProperty(family).GetArrayLength());
        }
    }

    /// <summary>encodeTerminalData: DLE doubling over hostile byte soup.</summary>
    [Fact]
    public void EncodeDataMatchesReference()
    {
        var asserted = 0;
        foreach (var c in FuzzCorpus.Family("encode_data", ExpectedEncodeData))
        {
            var id = FuzzCorpus.CaseId(c);
            var input = FuzzCorpus.B64Str(c, "in_b64");
            var want = FuzzCorpus.B64(c, "out_b64");
            var got = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeTerminalData(input));
            if (!want.AsSpan().SequenceEqual(got))
            {
                Assert.Fail(
                    $"case {id} diverged (encode_data)\n" +
                    $"  input    : {FuzzCorpus.Show(FuzzCorpus.B64(c, "in_b64"))}\n" +
                    $"  recorded : {FuzzCorpus.Show(want)}\n" +
                    $"  csharp   : {FuzzCorpus.Show(got)}");
            }

            asserted++;
        }

        _out.WriteLine($"encode_data: asserted {asserted} cases");
        Assert.Equal(ExpectedEncodeData, asserted);
    }

    /// <summary>encodeControlFrame: header hex + UTF-8 byte length + compact JSON.</summary>
    [Fact]
    public void EncodeControlMatchesReference()
    {
        var asserted = 0;
        foreach (var c in FuzzCorpus.Family("encode_control", ExpectedEncodeControl))
        {
            var id = FuzzCorpus.CaseId(c);
            var payload = ControlChannelCodec.JsonElementToDictionary(c.GetProperty("payload"));
            var want = FuzzCorpus.B64(c, "out_b64");
            var got = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(payload));
            if (!want.AsSpan().SequenceEqual(got))
            {
                Assert.Fail(
                    $"case {id} diverged (encode_control)\n" +
                    $"  payload  : {c.GetProperty("payload").GetRawText()}\n" +
                    $"  recorded : {FuzzCorpus.Show(want)}\n" +
                    $"  csharp   : {FuzzCorpus.Show(got)}");
            }

            asserted++;
        }

        _out.WriteLine($"encode_control: asserted {asserted} cases");
        Assert.Equal(ExpectedEncodeControl, asserted);
    }

    /// <summary>isControlFrame: the structural predicate, true for 29 of 128.</summary>
    [Fact]
    public void IsControlFrameMatchesReference()
    {
        var asserted = 0;
        var trueCount = 0;
        foreach (var c in FuzzCorpus.Family("is_control_frame", ExpectedIsControlFrame))
        {
            var id = FuzzCorpus.CaseId(c);
            var want = c.GetProperty("out").GetBoolean();
            var got = ControlChannelCodec.IsControlFrame(FuzzCorpus.B64Str(c, "in_b64"));
            if (want != got)
            {
                Assert.Fail(
                    $"case {id} diverged (is_control_frame)\n" +
                    $"  input    : {FuzzCorpus.Show(FuzzCorpus.B64(c, "in_b64"))}\n" +
                    $"  recorded : {want}\n" +
                    $"  csharp   : {got}");
            }

            if (want)
            {
                trueCount++;
            }

            asserted++;
        }

        _out.WriteLine($"is_control_frame: asserted {asserted} cases ({trueCount} true)");
        Assert.Equal(ExpectedIsControlFrame, asserted);
        Assert.Equal(29, trueCount);
    }

    /// <summary>The stateful family: every case driven chunked AND single.</summary>
    [Fact]
    public void DecodeMatchesReferenceOnBothDrives()
    {
        var (cases, drives, differing) = ReplayDecodeFamily("decode", ExpectedDecode);
        _out.WriteLine($"decode: asserted {cases} cases / {drives} drives ({differing} chunked!=single)");
        Assert.Equal(ExpectedDecode, cases);
        Assert.Equal(ExpectedDecode * 2, drives);

        // README: 39 of the 192 generated cases differ between the two drives.
        // If this ever reads 0 the replay collapsed the two drives into one.
        Assert.Equal(39, differing);
    }

    /// <summary>Permanently-numbered hand-written cases; same shape as decode.</summary>
    [Fact]
    public void RegressionsMatchReferenceOnBothDrives()
    {
        var (cases, drives, _) = ReplayDecodeFamily("regressions", ExpectedRegressions);
        _out.WriteLine($"regressions: asserted {cases} cases / {drives} drives");
        Assert.Equal(ExpectedRegressions, cases);
        Assert.Equal(ExpectedRegressions * 2, drives);
    }

    private static (int Cases, int Drives, int Differing) ReplayDecodeFamily(string family, int expected)
    {
        var cases = 0;
        var drives = 0;
        var differing = 0;
        foreach (var c in FuzzCorpus.Family(family, expected))
        {
            var id = FuzzCorpus.CaseId(c);
            var chunks = c.GetProperty("chunks_b64").EnumerateArray().Select(FuzzCorpus.B64Str).ToArray();
            var finish = c.GetProperty("finish").GetBoolean();

            AssertDrive(id, "chunked", c.GetProperty("chunked"), Drive(chunks, finish));
            drives++;

            // "Feed exactly one chunk even when the concatenation is empty."
            AssertDrive(id, "single", c.GetProperty("single"), Drive([string.Concat(chunks)], finish));
            drives++;

            if (!DriveRecordsEqual(c.GetProperty("chunked"), c.GetProperty("single")))
            {
                differing++;
            }

            cases++;
        }

        return (cases, drives, differing);
    }

    private sealed record DriveResult(List<Chunk> Events, string? Error, List<string> OnError);

    /// <summary>
    /// Feed a fresh decoder, stopping at the first protocol error and keeping the
    /// events emitted before it. Events returned by the throwing call itself are
    /// lost, matching the reference (see CCF-REG-0004).
    /// </summary>
    private static DriveResult Drive(IReadOnlyList<string> chunks, bool finish)
    {
        var onError = new List<string>();
        var decoder = new ControlFrameDecoder(new DecoderOptions { OnError = onError.Add });
        var events = new List<Chunk>();
        try
        {
            foreach (var chunk in chunks)
            {
                events.AddRange(decoder.Feed(chunk));
            }

            if (finish)
            {
                events.AddRange(decoder.Finish());
            }
        }
        catch (ProtocolException ex)
        {
            return new DriveResult(events, ex.Message, onError);
        }

        return new DriveResult(events, null, onError);
    }

    private static void AssertDrive(string id, string drive, JsonElement want, DriveResult got)
    {
        void Fail(string why) => Assert.Fail(
            $"case {id} diverged ({drive} drive): {why}\n" +
            $"  recorded : {RenderRecorded(want)}\n" +
            $"  csharp   : {RenderActual(got)}");

        var wantError = want.GetProperty("error");
        var wantErrorText = wantError.ValueKind == JsonValueKind.Null ? null : wantError.GetString();
        if (!string.Equals(wantErrorText, got.Error, StringComparison.Ordinal))
        {
            Fail($"error '{wantErrorText ?? "null"}' != '{got.Error ?? "null"}'");
        }

        var wantHook = want.GetProperty("on_error").EnumerateArray().Select(e => e.GetString() ?? "").ToArray();
        if (!wantHook.SequenceEqual(got.OnError, StringComparer.Ordinal))
        {
            Fail($"on_error [{string.Join(",", wantHook)}] != [{string.Join(",", got.OnError)}]");
        }

        var wantEvents = want.GetProperty("events").EnumerateArray().ToArray();
        if (wantEvents.Length != got.Events.Count)
        {
            Fail($"{wantEvents.Length} events recorded, {got.Events.Count} emitted");
        }

        for (var i = 0; i < wantEvents.Length; i++)
        {
            var we = wantEvents[i];
            var kind = we.GetProperty("kind").GetString();
            var ge = got.Events[i];
            if (kind != ge.Kind)
            {
                Fail($"event {i} kind '{kind}' != '{ge.Kind}'");
                return;
            }

            if (kind == "data")
            {
                var wantBytes = FuzzCorpus.B64(we, "data_b64");
                var gotBytes = Encoding.UTF8.GetBytes(((DataChunk)ge).Data);
                if (!wantBytes.AsSpan().SequenceEqual(gotBytes))
                {
                    Fail($"event {i} data '{FuzzCorpus.Show(wantBytes)}' != '{FuzzCorpus.Show(gotBytes)}'");
                }
            }
            else if (!FuzzCorpus.Matches(we.GetProperty("control"), ((ControlChunk)ge).Control))
            {
                Fail($"event {i} control {we.GetProperty("control").GetRawText()} != " +
                     FuzzCorpus.Render(((ControlChunk)ge).Control));
            }
        }
    }

    /// <summary>Structural equality of two recorded drive records (chunked vs single).</summary>
    private static bool DriveRecordsEqual(JsonElement a, JsonElement b) =>
        string.Equals(
            JsonSerializer.Serialize(a, JsonOpts),
            JsonSerializer.Serialize(b, JsonOpts),
            StringComparison.Ordinal);

    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = false };

    private static string RenderRecorded(JsonElement drive)
    {
        var events = drive.GetProperty("events").EnumerateArray().Select(e =>
            e.GetProperty("kind").GetString() == "data"
                ? "data(" + FuzzCorpus.Show(FuzzCorpus.B64(e, "data_b64")) + ")"
                : "control(" + e.GetProperty("control").GetRawText() + ")");
        var err = drive.GetProperty("error");
        return "[" + string.Join(", ", events) + "] error=" +
               (err.ValueKind == JsonValueKind.Null ? "null" : err.GetString()) +
               " on_error=[" + string.Join(",", drive.GetProperty("on_error").EnumerateArray()
                   .Select(e => e.GetString())) + "]";
    }

    private static string RenderActual(DriveResult r)
    {
        var events = r.Events.Select(e => e is DataChunk d
            ? "data(" + FuzzCorpus.Show(Encoding.UTF8.GetBytes(d.Data)) + ")"
            : "control(" + FuzzCorpus.Render(((ControlChunk)e).Control) + ")");
        return "[" + string.Join(", ", events) + "] error=" + (r.Error ?? "null") +
               " on_error=[" + string.Join(",", r.OnError) + "]";
    }

    /// <summary>
    /// <c>serializer_divergences</c> is recorded, NOT asserted equal across ports:
    /// these are inputs where the four runtimes' JSON serializers legitimately
    /// disagree. This port pins its OWN bytes so a change to .NET's escaping shows
    /// up in a diff, and asserts the recorded CPython bytes really are different
    /// (which is the claim that justifies keeping these out of `encode_control`).
    /// </summary>
    [Fact]
    public void SerializerDivergencesArePinnedNotAsserted()
    {
        var pinned = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            // CPython writes 0.0 / [1.0,1.5,2]; .NET (like Go and JS) drops the
            // trailing .0 because a whole-valued double round-trips as an integer.
            ["CCF-SD-0001"] = @"{""k0"":0}",
            ["CCF-SD-0002"] = @"{""k0"":[1,1.5,2]}",

            // .NET escapes U+2028/U+2029 even under UnsafeRelaxedJsonEscaping.
            ["CCF-SD-0003"] = @"{""k0"":""\u2028\u2029""}",

            // .NET escapes DEL; CPython/Go/JS emit it raw.
            ["CCF-SD-0004"] = @"{""k0"":""\u007F""}",

            // .NET writes \uXXXX with upper-case hex digits.
            ["CCF-SD-0005"] = @"{""k0"":""\u001F""}",

            // .NET writes astral code points as an escaped surrogate pair.
            ["CCF-SD-0006"] = @"{""k0"":""\uD834\uDD1E""}",
        };

        var asserted = 0;
        foreach (var c in FuzzCorpus.Family("serializer_divergences", ExpectedSerializerDivergences))
        {
            var id = FuzzCorpus.CaseId(c);
            Assert.False(string.IsNullOrWhiteSpace(c.GetProperty("note").GetString()), id);

            var payload = ControlChannelCodec.JsonElementToDictionary(c.GetProperty("payload"));
            var frame = ControlChannelCodec.EncodeControlFrame(payload);
            var body = frame[11..];
            var cpython = Encoding.UTF8.GetString(FuzzCorpus.B64(c, "cpython_out_b64"));

            Assert.True(pinned.TryGetValue(id, out var want), $"unpinned serializer divergence {id}");
            Assert.Equal(want, body);

            // Header still declares the UTF-8 byte length of *this port's* body.
            Assert.Equal(
                Encoding.UTF8.GetByteCount(body).ToString("x8", System.Globalization.CultureInfo.InvariantCulture),
                frame[2..10]);

            // All six are documented as .NET-divergent; if one stops diverging the
            // corpus README's table is stale and should be revisited.
            Assert.NotEqual(cpython, frame);
            _out.WriteLine($"{id}: cpython={cpython[11..]}  csharp={body}");
            asserted++;
        }

        Assert.Equal(ExpectedSerializerDivergences, asserted);
    }
}
