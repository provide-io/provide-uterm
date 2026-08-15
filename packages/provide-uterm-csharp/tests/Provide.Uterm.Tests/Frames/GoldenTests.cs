//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Frames;

namespace Provide.Uterm.Tests.Frames;

public class GoldenTests
{
    // Keys the reference emits as an explicit null and this port omits. Ordinal
    // sort order, because the assertion compares the sequence.
    private static readonly Dictionary<string, string[]> WantNullStripped = new()
    {
        ["snapshot_full"] = ["bytes_read", "chunks_read"],
        ["snapshot_minimal"] = ["bytes_read", "chunks_read", "prompt_detected", "raw_tail"],
        ["analysis_null_raw"] = ["raw"],
        ["hijack_state_off"] = ["lease_expires_at", "owner"],
    };

    /// <summary>
    /// Keys where this port deliberately advertises a different value from the
    /// reference, per <c>spec/behavior.json</c> <c>hello_defaults.csharp</c>.
    ///
    /// Declared here rather than by deleting the key from our copy of the
    /// corpus, which is what used to happen: the copy had drifted from Go's to
    /// drop `mcp_supported`/`vnc_supported` entirely, and in doing so it also
    /// dropped `bytes_read`/`chunks_read` and quietly asserted that this port's
    /// missing snapshot counters were correct. The corpus is the reference's
    /// output and is now byte-identical with Go's copy again; every difference
    /// has to be stated, and a difference that stops being true fails.
    /// </summary>
    private static readonly Dictionary<string, Dictionary<string, bool>> SpecDivergence = new()
    {
        // MCP is not part of the C# port (operator de-scope).
        ["hello"] = new() { ["mcp_supported"] = false },
    };

    private static Dictionary<string, IFrame> GoldenGoFrames()
    {
        // WithDefaults, not the bare type stamp: the reference's golden hello
        // carries mcp_supported/vnc_supported, and building ours without them
        // is what made someone delete both keys from our copy of the corpus --
        // which silently took bytes_read/chunks_read with them. This asserts
        // the capabilities this port actually advertises (spec/behavior.json
        // hello_defaults.csharp), with the one difference declared below.
        var hello = FrameBuilders.MakeHelloFrameWithDefaults();
        hello.WorkerId = "w1";
        hello.CanHijack = true;
        hello.Hijacked = false;
        hello.WorkerOnline = true;
        hello.InputMode = "raw";
        hello.Protocol = new Dictionary<string, int> { ["selected"] = 2, ["server_min"] = 1, ["server_max"] = 2 };

        var identityFull = FrameBuilders.NewIdentityFrame("user:alice");
        identityFull.Claims = new Dictionary<string, object?> { ["role"] = "admin", ["n"] = 3L };
        identityFull.Fingerprint = "SHA256:fp";
        identityFull.Transport = "ws";

        return new Dictionary<string, IFrame>
        {
            ["error"] = FrameBuilders.MakeErrorFrame("boom"),
            ["pong"] = FrameBuilders.MakePongFrame(123.5),
            ["heartbeat_ack"] = FrameBuilders.MakeHeartbeatAckFrame(456.25, 123.5),
            ["worker_connected"] = FrameBuilders.MakeWorkerConnectedFrame("w1", 1.5),
            ["worker_disconnected"] = FrameBuilders.MakeWorkerDisconnectedFrame("w1", 2.5),
            ["term"] = FrameBuilders.MakeTermFrame("hi\x1b[0mé", 3.5),
            ["snapshot_minimal"] = FrameBuilders.MakeSnapshotFrame(new FrameBuilders.SnapshotParams
            {
                Screen = "line1\nline2",
                Cursor = new Dictionary<string, int> { ["x"] = 1, ["y"] = 2 },
                Cols = 80,
                Rows = 25,
                ScreenHash = "abc123",
                CursorAtEnd = true,
                HasTrailingSpace = false,
                PromptDetected = null,
                Ts = 9.5,
                RawTail = null,
            }),
            ["snapshot_full"] = FrameBuilders.MakeSnapshotFrame(new FrameBuilders.SnapshotParams
            {
                Screen = "s",
                Cursor = new Dictionary<string, int> { ["x"] = 0, ["y"] = 0 },
                Cols = 132,
                Rows = 43,
                ScreenHash = "h",
                CursorAtEnd = false,
                HasTrailingSpace = true,
                PromptDetected = new Dictionary<string, object?> { ["prompt_id"] = "shell", ["confidence"] = 0.75 },
                Ts = 10.5,
                RawTail = "tail\x1b[1m",
            }),
            ["analysis_null_raw"] = FrameBuilders.MakeAnalysisFrame("f", null, 4.5),
            ["analysis_raw"] = FrameBuilders.MakeAnalysisFrame("f", new Dictionary<string, object?> { ["k"] = new List<object?> { 1L, 2L } }, 5.5),
            ["hijack_state_off"] = FrameBuilders.MakeHijackStateFrame(false, null, null, "raw"),
            ["hijack_state_on"] = FrameBuilders.MakeHijackStateFrame(true, "alice", 99.5, "cooked"),
            ["hello"] = hello,
            ["status"] = FrameBuilders.CoerceWorkerStatusFrame(new Dictionary<string, object?> { ["cpu"] = 12.5, ["tag"] = "ok", ["ts"] = 6.5 }),
            ["identity_defaults"] = FrameBuilders.NewIdentityFrame("user:bob"),
            ["identity_full"] = identityFull,
            ["session_token"] = new SessionTokenFrame { Type = FrameTypeNames.SessionToken, Token = "tok", PlayerId = 3 },
            ["session_token_no_player"] = new SessionTokenFrame { Type = FrameTypeNames.SessionToken, Token = "tok2" },
            ["resume"] = new ResumeFrame { Type = FrameTypeNames.Resume, Token = "rtok", PlayerId = 7 },
            ["resume_ok"] = new ResumeOkFrame { Type = FrameTypeNames.ResumeOk },
            ["resume_failed"] = new ResumeFailedFrame { Type = FrameTypeNames.ResumeFailed, Reason = "expired" },
            ["link_patterns"] = new LinkPatternsFrame
            {
                Type = FrameTypeNames.LinkPatterns,
                Patterns =
                [
                    new LinkPatternEntry { Pattern = "foo(\\d+)", Action = "cmd", Id = "p1", Group = 1L, Payload = "run {1}" },
                    new LinkPatternEntry
                    {
                        Pattern = "https?://\\S+",
                        Action = "url",
                        Flags = "i",
                        Hover = "open",
                        LineContains = "http",
                        Class = "link",
                    },
                ],
            },
            ["presence_update"] = new PresenceUpdateFrame
            {
                Type = FrameTypeNames.PresenceUpdate,
                UserId = "u1",
                Extra = new Dictionary<string, object?> { ["scroll_line"] = 5L, ["typing"] = true },
            },
        };
    }

    private static List<string> StripNulls(Dictionary<string, JsonElement> m)
    {
        var stripped = new List<string>();
        foreach (var k in m.Keys.ToList())
        {
            if (m[k].ValueKind == JsonValueKind.Null)
            {
                m.Remove(k);
                stripped.Add(k);
            }
        }

        stripped.Sort(StringComparer.Ordinal);
        return stripped;
    }

    private static bool JsonEqual(JsonElement a, JsonElement b)
    {
        if (a.ValueKind != b.ValueKind)
        {
            // number int/float
            if (a.ValueKind == JsonValueKind.Number && b.ValueKind == JsonValueKind.Number)
            {
                return Math.Abs(a.GetDouble() - b.GetDouble()) < 1e-12;
            }

            return false;
        }

        switch (a.ValueKind)
        {
            case JsonValueKind.Object:
            {
                var aProps = a.EnumerateObject().OrderBy(p => p.Name, StringComparer.Ordinal).ToList();
                var bProps = b.EnumerateObject().OrderBy(p => p.Name, StringComparer.Ordinal).ToList();
                if (aProps.Count != bProps.Count)
                {
                    return false;
                }

                for (var i = 0; i < aProps.Count; i++)
                {
                    if (aProps[i].Name != bProps[i].Name || !JsonEqual(aProps[i].Value, bProps[i].Value))
                    {
                        return false;
                    }
                }

                return true;
            }
            case JsonValueKind.Array:
            {
                var aa = a.EnumerateArray().ToList();
                var bb = b.EnumerateArray().ToList();
                if (aa.Count != bb.Count)
                {
                    return false;
                }

                for (var i = 0; i < aa.Count; i++)
                {
                    if (!JsonEqual(aa[i], bb[i]))
                    {
                        return false;
                    }
                }

                return true;
            }
            case JsonValueKind.String:
                return a.GetString() == b.GetString();
            case JsonValueKind.Number:
                return Math.Abs(a.GetDouble() - b.GetDouble()) < 1e-12;
            case JsonValueKind.True:
            case JsonValueKind.False:
                return a.GetBoolean() == b.GetBoolean();
            case JsonValueKind.Null:
                return true;
            default:
                return a.GetRawText() == b.GetRawText();
        }
    }

    [Fact]
    public void GoldenAgainstPythonBuilders()
    {
        var path = TestData.PathTo("frames", "python_golden.json");
        Assert.True(File.Exists(path), path);
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        var frames = GoldenGoFrames();
        Assert.Equal(doc.RootElement.EnumerateObject().Count(), frames.Count);

        foreach (var (name, frame) in frames)
        {
            Assert.True(doc.RootElement.TryGetProperty(name, out var goldenEl), name);
            var wantMap = goldenEl.EnumerateObject().ToDictionary(p => p.Name, p => p.Value);
            var stripped = StripNulls(wantMap);
            var expectedStripped = WantNullStripped.GetValueOrDefault(name) ?? Array.Empty<string>();
            Assert.Equal(expectedStripped, stripped);

            // Rebuild want JSON without nulls
            using var wantDoc = JsonDocument.Parse(goldenEl.GetRawText());
            var wantFiltered = wantDoc.RootElement.EnumerateObject()
                .Where(p => p.Value.ValueKind != JsonValueKind.Null)
                .ToDictionary(p => p.Name, p => p.Value);

            var encoded = FrameCodec.EncodeFrame(frame);
            using var gotDoc = JsonDocument.Parse(encoded);
            var gotMap = gotDoc.RootElement.EnumerateObject().ToDictionary(p => p.Name, p => p.Value);

            Assert.Equal(wantFiltered.Keys.OrderBy(k => k), gotMap.Keys.OrderBy(k => k));
            var divergent = SpecDivergence.GetValueOrDefault(name);
            foreach (var k in wantFiltered.Keys)
            {
                if (divergent is not null && divergent.TryGetValue(k, out var ours))
                {
                    Assert.Equal(ours, gotMap[k].GetBoolean());
                    // If the reference ever adopts our value the entry is stale
                    // and must go, exactly like a stale warning-baseline entry.
                    Assert.NotEqual(ours, wantFiltered[k].GetBoolean());
                    continue;
                }

                Assert.True(JsonEqual(gotMap[k], wantFiltered[k]), $"{name}.{k}: got {gotMap[k]} want {wantFiltered[k]}");
            }
        }
    }

    [Fact]
    public void GoldenFramesDecode()
    {
        var path = TestData.PathTo("frames", "python_golden.json");
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        foreach (var prop in doc.RootElement.EnumerateObject())
        {
            var raw = prop.Value.GetRawText();
            var got = FrameCodec.DecodeFrame(raw);
            Assert.Equal(prop.Value.GetProperty("type").GetString(), got.FrameType);
        }
    }
}
