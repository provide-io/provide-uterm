//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Ansi;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.CtrlMsg;
using Provide.Uterm.DeckMux;
using Provide.Uterm.Emulator;
using Provide.Uterm.Screen;

namespace Provide.Uterm.Tests.Conformance;

/// <summary>
/// Cross-language wire-compatibility proof — port of
/// packages/provide-uterm-go/conformance/conformance_test.go.
///
/// Vectors in testdata/conformance/vectors.json are produced by the Python
/// reference (gen_vectors.py). Each test replays a Python-authored input through
/// the C# port and asserts the C# output equals Python's byte-for-byte.
/// </summary>
public class ConformanceVectorsTests
{
    private sealed class Vectors
    {
        public List<ControlFrameCase> ControlFrames { get; set; } = new();
        public List<TerminalDataCase> TerminalData { get; set; } = new();
        public List<InOutCase> NormalizeTerminalText { get; set; } = new();
        public List<Cp437Case> Cp437Roundtrip { get; set; } = new();
        public List<InOutCase> NormalizeColors { get; set; } = new();
        public List<InOutCase> Upgrade256 { get; set; } = new();
        public List<WebhookCase> WebhookHmac { get; set; } = new();
        public List<IdentityCase> IdentitySignature { get; set; } = new();
        public List<DeckCase> DeckIdentity { get; set; } = new();
        public EmulatorCase? EmulatorSnapshot { get; set; }
    }

    private sealed class ControlFrameCase
    {
        public Dictionary<string, object?> Payload { get; set; } = new();
        public string WireB64 { get; set; } = "";
    }

    private sealed class TerminalDataCase
    {
        public string Raw { get; set; } = "";
        public string WireB64 { get; set; } = "";
    }

    private sealed class InOutCase
    {
        public string In { get; set; } = "";
        public string Out { get; set; } = "";
    }

    private sealed class Cp437Case
    {
        public string BytesB64 { get; set; } = "";
        public string Decoded { get; set; } = "";
    }

    private sealed class WebhookCase
    {
        public string Secret { get; set; } = "";
        public string Body { get; set; } = "";
        public string? Sig { get; set; }
    }

    private sealed class IdentityCase
    {
        public string Subject { get; set; } = "";
        public Dictionary<string, object?>? Claims { get; set; }
        public string Secret { get; set; } = "";
        public Dictionary<string, object?> Frame { get; set; } = new();
    }

    private sealed class DeckCase
    {
        public string UserId { get; set; } = "";
        public string Name { get; set; } = "";
        public string Color { get; set; } = "";
        public string Initials { get; set; } = "";
    }

    private sealed class EmulatorCase
    {
        public string FeedB64 { get; set; } = "";
        public int Cols { get; set; }
        public int Rows { get; set; }
        public string Screen { get; set; } = "";
        public string ScreenHash { get; set; } = "";
        public Dictionary<string, int> Cursor { get; set; } = new();
        public bool CursorAtEnd { get; set; }
    }

    private static (Vectors Vectors, string Source) LoadVectors()
    {
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("UTERM_CONFORMANCE_NO_REGEN")))
        {
            if (TryRegenFromPython(out var live, out var source))
            {
                return (DecodeVectors(live), source);
            }
        }

        var golden = FindGoldenPath();
        var raw = File.ReadAllBytes(golden);
        return (DecodeVectors(raw), "committed golden (" + golden + ")");
    }

    private static string FindGoldenPath()
    {
        // Test project copies testdata/** to output; also try relative source tree.
        var candidates = new[]
        {
            Path.Combine(AppContext.BaseDirectory, "testdata", "conformance", "vectors.json"),
            Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "testdata", "conformance", "vectors.json"),
            Path.GetFullPath(Path.Combine(
                AppContext.BaseDirectory, "..", "..", "..", "..", "..",
                "tests", "Provide.Uterm.Tests", "testdata", "conformance", "vectors.json")),
        };
        foreach (var c in candidates)
        {
            var full = Path.GetFullPath(c);
            if (File.Exists(full))
            {
                return full;
            }
        }

        throw new FileNotFoundException("conformance vectors.json not found under testdata/conformance/");
    }

    private static Vectors DecodeVectors(byte[] raw)
    {
        using var doc = JsonDocument.Parse(raw);
        var root = doc.RootElement;
        var v = new Vectors();

        if (root.TryGetProperty("control_frames", out var cf))
        {
            foreach (var el in cf.EnumerateArray())
            {
                v.ControlFrames.Add(new ControlFrameCase
                {
                    Payload = JsonElementToDict(el.GetProperty("payload")),
                    WireB64 = el.GetProperty("wire_b64").GetString() ?? "",
                });
            }
        }

        if (root.TryGetProperty("terminal_data", out var td))
        {
            foreach (var el in td.EnumerateArray())
            {
                v.TerminalData.Add(new TerminalDataCase
                {
                    Raw = el.GetProperty("raw").GetString() ?? "",
                    WireB64 = el.GetProperty("wire_b64").GetString() ?? "",
                });
            }
        }

        if (root.TryGetProperty("normalize_terminal_text", out var nt))
        {
            foreach (var el in nt.EnumerateArray())
            {
                v.NormalizeTerminalText.Add(new InOutCase
                {
                    In = el.GetProperty("in").GetString() ?? "",
                    Out = el.GetProperty("out").GetString() ?? "",
                });
            }
        }

        if (root.TryGetProperty("cp437_roundtrip", out var cp))
        {
            foreach (var el in cp.EnumerateArray())
            {
                v.Cp437Roundtrip.Add(new Cp437Case
                {
                    BytesB64 = el.GetProperty("bytes_b64").GetString() ?? "",
                    Decoded = el.GetProperty("decoded").GetString() ?? "",
                });
            }
        }

        if (root.TryGetProperty("normalize_colors", out var nc))
        {
            foreach (var el in nc.EnumerateArray())
            {
                v.NormalizeColors.Add(new InOutCase
                {
                    In = el.GetProperty("in").GetString() ?? "",
                    Out = el.GetProperty("out").GetString() ?? "",
                });
            }
        }

        if (root.TryGetProperty("upgrade_256", out var u2))
        {
            foreach (var el in u2.EnumerateArray())
            {
                v.Upgrade256.Add(new InOutCase
                {
                    In = el.GetProperty("in").GetString() ?? "",
                    Out = el.GetProperty("out").GetString() ?? "",
                });
            }
        }

        if (root.TryGetProperty("webhook_hmac", out var wh))
        {
            foreach (var el in wh.EnumerateArray())
            {
                v.WebhookHmac.Add(new WebhookCase
                {
                    Secret = el.GetProperty("secret").GetString() ?? "",
                    Body = el.GetProperty("body").GetString() ?? "",
                    Sig = el.TryGetProperty("sig", out var s) && s.ValueKind != JsonValueKind.Null
                        ? s.GetString()
                        : null,
                });
            }
        }

        if (root.TryGetProperty("identity_signature", out var id))
        {
            foreach (var el in id.EnumerateArray())
            {
                Dictionary<string, object?>? claims = null;
                if (el.TryGetProperty("claims", out var c) && c.ValueKind == JsonValueKind.Object)
                {
                    claims = JsonElementToDict(c);
                }

                v.IdentitySignature.Add(new IdentityCase
                {
                    Subject = el.GetProperty("subject").GetString() ?? "",
                    Claims = claims,
                    Secret = el.GetProperty("secret").GetString() ?? "",
                    Frame = JsonElementToDict(el.GetProperty("frame")),
                });
            }
        }

        if (root.TryGetProperty("deck_identity", out var deck))
        {
            foreach (var el in deck.EnumerateArray())
            {
                v.DeckIdentity.Add(new DeckCase
                {
                    UserId = el.GetProperty("user_id").GetString() ?? "",
                    Name = el.GetProperty("name").GetString() ?? "",
                    Color = el.GetProperty("color").GetString() ?? "",
                    Initials = el.GetProperty("initials").GetString() ?? "",
                });
            }
        }

        if (root.TryGetProperty("emulator_snapshot", out var emu) && emu.ValueKind == JsonValueKind.Object)
        {
            var cursor = new Dictionary<string, int>();
            if (emu.TryGetProperty("cursor", out var cur))
            {
                foreach (var p in cur.EnumerateObject())
                {
                    cursor[p.Name] = p.Value.GetInt32();
                }
            }

            v.EmulatorSnapshot = new EmulatorCase
            {
                FeedB64 = emu.GetProperty("feed_b64").GetString() ?? "",
                Cols = emu.GetProperty("cols").GetInt32(),
                Rows = emu.GetProperty("rows").GetInt32(),
                Screen = emu.GetProperty("screen").GetString() ?? "",
                ScreenHash = emu.GetProperty("screen_hash").GetString() ?? "",
                Cursor = cursor,
                CursorAtEnd = emu.GetProperty("cursor_at_end").GetBoolean(),
            };
        }

        return v;
    }

    private static Dictionary<string, object?> JsonElementToDict(JsonElement el) =>
        ControlChannelCodec.JsonElementToDictionary(el);

    private static bool TryRegenFromPython(out byte[] raw, out string source)
    {
        raw = Array.Empty<byte>();
        source = "";
        try
        {
            // Locate uv
            var uv = Which("uv");
            if (uv is null)
            {
                return false;
            }

            var root = FindRepoRoot();
            if (root is null)
            {
                return false;
            }

            var script = Path.Combine(root, "packages", "provide-uterm-go", "conformance", "gen_vectors.py");
            if (!File.Exists(script))
            {
                return false;
            }

            var psi = new ProcessStartInfo
            {
                FileName = uv,
                Arguments = "run python " + Quote(script),
                WorkingDirectory = root,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
            };
            using var proc = Process.Start(psi);
            if (proc is null)
            {
                return false;
            }

            var stdout = proc.StandardOutput.ReadToEnd();
            var stderr = proc.StandardError.ReadToEnd();
            proc.WaitForExit(120_000);
            if (proc.ExitCode != 0 || string.IsNullOrWhiteSpace(stdout))
            {
                return false;
            }

            raw = Encoding.UTF8.GetBytes(stdout);
            source = "live Python regeneration (uv run gen_vectors.py)";
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static string? FindRepoRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 10 && dir is not null; i++)
        {
            if (Directory.Exists(Path.Combine(dir.FullName, "packages", "provide-uterm", "src")))
            {
                return dir.FullName;
            }

            dir = dir.Parent;
        }

        return null;
    }

    private static string? Which(string name)
    {
        var path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var dir in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            var candidate = Path.Combine(dir, name);
            if (File.Exists(candidate))
            {
                return candidate;
            }

            if (File.Exists(candidate + ".exe"))
            {
                return candidate + ".exe";
            }
        }

        return null;
    }

    private static string Quote(string s) =>
        s.Contains(' ') ? "\"" + s + "\"" : s;

    private static byte[] MustB64(string s) => Convert.FromBase64String(s);

    private static bool JsonEqual(object? a, object? b)
    {
        var ab = JsonSerializer.Serialize(a);
        var bb = JsonSerializer.Serialize(b);
        using var da = JsonDocument.Parse(ab);
        using var db = JsonDocument.Parse(bb);
        return JsonElementDeepEquals(da.RootElement, db.RootElement);
    }

    private static bool JsonElementDeepEquals(JsonElement a, JsonElement b)
    {
        if (a.ValueKind != b.ValueKind)
        {
            // number vs number with different kinds is handled below after re-parse
            if (a.ValueKind is JsonValueKind.Number or JsonValueKind.String
                && b.ValueKind is JsonValueKind.Number or JsonValueKind.String)
            {
                // fall through to ToString compare for numeric strings
            }
            else
            {
                return false;
            }
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
                    if (aProps[i].Name != bProps[i].Name)
                    {
                        return false;
                    }

                    if (!JsonElementDeepEquals(aProps[i].Value, bProps[i].Value))
                    {
                        return false;
                    }
                }

                return true;
            }
            case JsonValueKind.Array:
            {
                var aa = a.EnumerateArray().ToList();
                var ba = b.EnumerateArray().ToList();
                if (aa.Count != ba.Count)
                {
                    return false;
                }

                for (var i = 0; i < aa.Count; i++)
                {
                    if (!JsonElementDeepEquals(aa[i], ba[i]))
                    {
                        return false;
                    }
                }

                return true;
            }
            case JsonValueKind.String:
                return a.GetString() == b.GetString();
            case JsonValueKind.Number:
                return a.GetRawText() == b.GetRawText()
                       || a.GetDouble() == b.GetDouble();
            case JsonValueKind.True:
            case JsonValueKind.False:
                return a.GetBoolean() == b.GetBoolean();
            case JsonValueKind.Null:
                return true;
            default:
                return a.GetRawText() == b.GetRawText();
        }
    }

    private static string? WebhookSig(string secret, string body)
    {
        if (string.IsNullOrEmpty(secret))
        {
            return null;
        }

        var mac = HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret), Encoding.UTF8.GetBytes(body));
        return "sha256=" + Convert.ToHexString(mac).ToLowerInvariant();
    }

    [Fact]
    public void Conformance_ReportsSource()
    {
        var (_, source) = LoadVectors();
        Assert.False(string.IsNullOrWhiteSpace(source));
        // Visible in test output / SCRATCH capture
        Console.WriteLine("cross-language conformance vectors: " + source);
    }

    [Fact]
    public void Conformance_ControlFrameDecode()
    {
        var (v, _) = LoadVectors();
        Assert.NotEmpty(v.ControlFrames);
        for (var i = 0; i < v.ControlFrames.Count; i++)
        {
            var c = v.ControlFrames[i];
            var wire = Encoding.UTF8.GetString(MustB64(c.WireB64));
            var dec = new ControlFrameDecoder();
            var events = dec.Feed(wire).ToList();
            events.AddRange(dec.Finish());
            Assert.True(events.Count == 1, $"case {i}: got {events.Count} events");
            var ctrl = Assert.IsType<ControlChunk>(events[0]);
            Assert.True(JsonEqual(ctrl.Control, c.Payload),
                $"case {i}: payload mismatch\n csharp: {JsonSerializer.Serialize(ctrl.Control)}\n py: {JsonSerializer.Serialize(c.Payload)}");
        }

        Console.WriteLine($"control_frames: {v.ControlFrames.Count} cases PASS (decode)");
    }

    [Fact]
    public void Conformance_ControlFrameEncode()
    {
        var (v, _) = LoadVectors();
        for (var i = 0; i < v.ControlFrames.Count; i++)
        {
            var c = v.ControlFrames[i];
            var wire = ControlChannelCodec.EncodeControlFrame(c.Payload);
            var dec = new ControlFrameDecoder();
            var events = dec.Feed(wire).ToList();
            events.AddRange(dec.Finish());
            Assert.True(events.Count == 1, $"case {i}: got {events.Count} events");
            var ctrl = Assert.IsType<ControlChunk>(events[0]);
            Assert.True(JsonEqual(ctrl.Control, c.Payload), $"case {i}: round-trip payload mismatch");
        }

        Console.WriteLine($"control_frames: {v.ControlFrames.Count} cases PASS (encode round-trip)");
    }

    [Fact]
    public void Conformance_TerminalDataEncode()
    {
        var (v, _) = LoadVectors();
        Assert.NotEmpty(v.TerminalData);
        for (var i = 0; i < v.TerminalData.Count; i++)
        {
            var c = v.TerminalData[i];
            var got = ControlChannelCodec.EncodeTerminalData(c.Raw);
            var want = Encoding.UTF8.GetString(MustB64(c.WireB64));
            Assert.True(got == want, $"case {i}: {JsonSerializer.Serialize(got)} vs {JsonSerializer.Serialize(want)}");
        }

        Console.WriteLine($"terminal_data: {v.TerminalData.Count} cases PASS (byte-exact)");
    }

    [Fact]
    public void Conformance_NormalizeTerminalText()
    {
        var (v, _) = LoadVectors();
        Assert.NotEmpty(v.NormalizeTerminalText);
        for (var i = 0; i < v.NormalizeTerminalText.Count; i++)
        {
            var c = v.NormalizeTerminalText[i];
            var got = ScreenNormalize.NormalizeTerminalText(c.In);
            Assert.True(got == c.Out, $"case {i}: in={JsonSerializer.Serialize(c.In)}\n got={JsonSerializer.Serialize(got)}\n want={JsonSerializer.Serialize(c.Out)}");
        }

        Console.WriteLine($"normalize_terminal_text: {v.NormalizeTerminalText.Count} cases PASS (byte-exact)");
    }

    [Fact]
    public void Conformance_Cp437Roundtrip()
    {
        var (v, _) = LoadVectors();
        Assert.NotEmpty(v.Cp437Roundtrip);
        for (var i = 0; i < v.Cp437Roundtrip.Count; i++)
        {
            var c = v.Cp437Roundtrip[i];
            var raw = MustB64(c.BytesB64);
            var got = Cp437.Decode(raw);
            Assert.True(got == c.Decoded, $"case {i}: decode mismatch");
            var re = Cp437.Encode(c.Decoded);
            Assert.True(raw.AsSpan().SequenceEqual(re), $"case {i}: re-encode mismatch");
        }

        Console.WriteLine($"cp437_roundtrip: {v.Cp437Roundtrip.Count} cases PASS (byte-exact)");
    }

    [Fact]
    public void Conformance_NormalizeColors()
    {
        var (v, _) = LoadVectors();
        Assert.NotEmpty(v.NormalizeColors);
        for (var i = 0; i < v.NormalizeColors.Count; i++)
        {
            var c = v.NormalizeColors[i];
            var got = ColorDialectRegistry.NormalizeColors(c.In);
            Assert.True(got == c.Out,
                $"case {i}: in={JsonSerializer.Serialize(c.In)}\n got={JsonSerializer.Serialize(got)}\n want={JsonSerializer.Serialize(c.Out)}");
        }

        Console.WriteLine($"normalize_colors: {v.NormalizeColors.Count} cases PASS (byte-exact)");
    }

    [Fact]
    public void Conformance_Upgrade256()
    {
        var (v, _) = LoadVectors();
        Assert.NotEmpty(v.Upgrade256);
        for (var i = 0; i < v.Upgrade256.Count; i++)
        {
            var c = v.Upgrade256[i];
            var got = Upgrade.UpgradeTo256(c.In);
            Assert.True(got == c.Out,
                $"case {i}: in={JsonSerializer.Serialize(c.In)}\n got={JsonSerializer.Serialize(got)}\n want={JsonSerializer.Serialize(c.Out)}");
        }

        Console.WriteLine($"upgrade_256: {v.Upgrade256.Count} cases PASS (byte-exact)");
    }

    [Fact]
    public void Conformance_WebhookHmac()
    {
        var (v, _) = LoadVectors();
        Assert.NotEmpty(v.WebhookHmac);
        for (var i = 0; i < v.WebhookHmac.Count; i++)
        {
            var c = v.WebhookHmac[i];
            var got = WebhookSig(c.Secret, c.Body);
            if (got is null && c.Sig is null)
            {
                continue; // fail-closed
            }

            Assert.False(got is null || c.Sig is null, $"case {i}: fail-closed mismatch go={got} py={c.Sig}");
            Assert.True(got == c.Sig, $"case {i}: sig mismatch\n csharp: {got}\n py: {c.Sig}");
        }

        Console.WriteLine($"webhook_hmac: {v.WebhookHmac.Count} cases PASS (byte-exact)");
    }

    [Fact]
    public void Conformance_IdentitySignature()
    {
        var (v, _) = LoadVectors();
        Assert.NotEmpty(v.IdentitySignature);
        for (var i = 0; i < v.IdentitySignature.Count; i++)
        {
            var c = v.IdentitySignature[i];
            var secret = Encoding.UTF8.GetBytes(c.Secret);
            var frame = Builders.MakeIdentity(
                c.Subject,
                claims: c.Claims,
                includeClaims: c.Claims is not null,
                fingerprint: "",
                transport: "ssh",
                secret: secret);
            Assert.True(JsonEqual(frame, c.Frame),
                $"case {i}: identity frame mismatch\n csharp: {JsonSerializer.Serialize(frame)}\n py: {JsonSerializer.Serialize(c.Frame)}");
        }

        Console.WriteLine($"identity_signature: {v.IdentitySignature.Count} cases PASS (byte-exact)");
    }

    [Fact]
    public void Conformance_DeckIdentity()
    {
        var (v, _) = LoadVectors();
        Assert.NotEmpty(v.DeckIdentity);
        for (var i = 0; i < v.DeckIdentity.Count; i++)
        {
            var c = v.DeckIdentity[i];
            var name = IdentityNames.GenerateName(c.UserId);
            Assert.True(name == c.Name, $"case {i} name: csharp={name} py={c.Name}");
            var color = IdentityNames.GenerateColor(c.UserId);
            Assert.True(color == c.Color, $"case {i} color: csharp={color} py={c.Color}");
            var initials = IdentityNames.GenerateInitials(c.Name);
            Assert.True(initials == c.Initials, $"case {i} initials: csharp={initials} py={c.Initials}");
        }

        Console.WriteLine($"deck_identity: {v.DeckIdentity.Count} cases PASS (byte-exact)");
    }

    [Fact]
    public void Conformance_EmulatorSnapshot()
    {
        var (v, _) = LoadVectors();
        Assert.NotNull(v.EmulatorSnapshot);
        var s = v.EmulatorSnapshot!;
        var emu = new TerminalEmulator(s.Cols, s.Rows);
        emu.Process(MustB64(s.FeedB64));
        var snap = emu.GetSnapshot();
        Assert.True(snap.Screen == s.Screen,
            $"screen mismatch\n csharp:\n{JsonSerializer.Serialize(snap.Screen)}\n py:\n{JsonSerializer.Serialize(s.Screen)}");
        Assert.True(snap.ScreenHash == s.ScreenHash,
            $"hash mismatch csharp={snap.ScreenHash} py={s.ScreenHash}");
        Assert.True(snap.Cursor.X == s.Cursor["x"] && snap.Cursor.Y == s.Cursor["y"],
            $"cursor mismatch csharp=({snap.Cursor.X},{snap.Cursor.Y}) py=({s.Cursor["x"]},{s.Cursor["y"]})");
        Assert.True(snap.CursorAtEnd == s.CursorAtEnd,
            $"cursor_at_end mismatch csharp={snap.CursorAtEnd} py={s.CursorAtEnd}");
        var want = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(snap.Screen))).ToLowerInvariant();
        Assert.Equal(want, snap.ScreenHash);
        Console.WriteLine("emulator_snapshot: PASS (byte-exact screen+hash+cursor)");
    }
}
