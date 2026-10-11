//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Text;
using Provide.Uterm.Emulator;
using Provide.Uterm.Render;
using Provide.Uterm.Screen;
using Provide.Uterm.Transports;
using Provide.Uterm.TunnelClient;
using Provide.Uterm.Vnc;
using Provide.Uterm.Vt;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>VT, text-extraction, rendering and small codec arms.</summary>
public sealed class Coverage100VtAndTextTests
{
    private static TerminalEmulator Emu(string feed, int cols = 10, int rows = 5)
    {
        var emu = new TerminalEmulator(cols, rows);
        emu.Process(Encoding.Latin1.GetBytes(feed));
        return emu;
    }

    [Theory]
    [InlineData("é")] // é → e + U+0301 → recomposed
    [InlineData("각")] // 각: Hangul syllable with a trailing consonant
    [InlineData("ṩ")] // ṩ: two-level canonical decomposition
    public void Nfc_normalize_matches_the_platform_composition(string s)
    {
        Assert.Equal(s.Normalize(NormalizationForm.FormC), Normalize.NfcNormalize(s));
    }

    [Fact]
    public void Control_characters_inside_a_csi_sequence_are_executed()
    {
        // ESC [ 1 CR 2 C: the CR inside the CSI returns the cursor to column 0
        // before the CUF(12 → clamped) applies.
        var emu = Emu("abc\x1b[\r2C");
        Assert.Equal(2, emu.Screen.Cursor.X);
    }

    [Fact]
    public void Cursor_report_is_relative_to_the_margins_in_origin_mode()
    {
        var screen = new Provide.Uterm.Vt.Screen(10, 10);
        string? reported = null;
        screen.WriteProcessInput = s => reported = s;
        var stream = new VtStream(screen);
        stream.Feed("\x1b[3;8r\x1b[?6h\x1b[2;4H\x1b[6n");
        Assert.Equal("\x1b[2;4R", reported);
    }

    [Fact]
    public void Reverse_index_at_the_top_scrolls_blank_lines_down()
    {
        var emu = Emu("\x1b[3;1Hx\x1b[1;1H\x1bM", cols: 4, rows: 4);
        var display = emu.Screen.Display();
        Assert.Equal("x   ", display[3]);
        Assert.Equal("    ", display[0]);
    }

    [Fact]
    public void Cursor_back_from_the_pending_wrap_column_steps_inside_the_line()
    {
        var emu = Emu("abcd\x1b[D", cols: 4, rows: 2);
        Assert.Equal(2, emu.Screen.Cursor.X);
    }

    [Fact]
    public void Sgr_zero_in_the_middle_of_a_list_resets_earlier_attributes()
    {
        var emu = Emu("\x1b[1;0;31mZ");
        var c = emu.Screen.At(0, 0);
        Assert.False(c.Bold);
        Assert.Equal("red", c.FG);
    }

    [Fact]
    public void Render_uses_a_space_for_the_empty_tail_of_a_wide_character()
    {
        var screen = new Provide.Uterm.Vt.Screen(4, 1);
        new VtStream(screen).Feed("中");
        var lines = RenderBuffer.RenderScreenLines(screen, 4, 1);
        Assert.Contains("中 ", lines[0], StringComparison.Ordinal);

        var a = RenderBuffer.DefaultStyle;
        object boxed = RenderBuffer.DefaultStyle;
        Assert.True(a.Equals(boxed));
        Assert.False(a.Equals("not a style"));
        Assert.True(a == RenderBuffer.DefaultStyle);
    }

    [Fact]
    public void Menu_and_numbered_extraction_skip_entries_without_descriptions()
    {
        Assert.Empty(Extract.ExtractMenuOptions("[A]"));
        Assert.Empty(Extract.ExtractNumberedList("1. "));
        Assert.False(PyText.IsPySpace(0xD800));
    }

    [Fact]
    public void Line_editor_kill_line_and_kill_word_redraw_the_remaining_text()
    {
        var writes = new List<string>();
        var ed = new LineEditor.LineEditor(onWrite: t => writes.Add(t));
        foreach (var ch in "ab cd  xy")
        {
            ed.ProcessChar(ch);
        }

        ed.ProcessChar('\u0002'); // Ctrl+B
        ed.ProcessChar('\u0002');
        ed.ProcessChar('\x17'); // Ctrl+W: trailing spaces, then "cd"
        Assert.Equal("ab xy", ed.Buffer());
        Assert.EndsWith("\x1b[2D", writes[^1], StringComparison.Ordinal);

        ed.ProcessChar('\x15'); // Ctrl+U with "xy" remaining
        Assert.Equal("xy", ed.Buffer());
        Assert.EndsWith("\x1b[2D", writes[^1], StringComparison.Ordinal);
    }

    [Fact]
    public async Task Replay_skips_read_events_with_empty_payloads()
    {
        var log = Path.GetTempFileName();
        var outPath = Path.GetTempFileName();
        try
        {
            await File.WriteAllLinesAsync(log, new[]
            {
                """{"event":"read","data":{"raw_bytes_b64":""}}""",
                """{"event":"read","data":{"raw_bytes_b64":"aGk="}}""",
            });
            await Replay.Replay.RebuildRawStreamAsync(log, outPath);
            Assert.Equal("hi"u8.ToArray(), await File.ReadAllBytesAsync(outPath));
        }
        finally
        {
            File.Delete(log);
            File.Delete(outPath);
        }
    }

    [Fact]
    public void Known_hosts_accepts_an_rsa_sha2_512_entry_for_an_ssh_rsa_key()
    {
        var key = new byte[] { 1, 2, 3, 4 };
        var path = Path.GetTempFileName();
        try
        {
            File.WriteAllText(path, "example.test rsa-sha2-512 " + Convert.ToBase64String(key) + "\n");
            Assert.True(KnownHosts.Matches("example.test", 22, "ssh-rsa", key, new[] { path }));
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Small_codec_guards_reject_malformed_input()
    {
        Assert.Throws<ArgumentException>(() => new FramebufferTracker(2, 2).ApplyRawUpdate(0, 0, 2, 2, new byte[3]));
        Assert.Throws<FormatException>(() => TunnelCodec.DecodeControl("[1]"u8));
        Assert.Equal("2", InterceptHeaders.StringifyHeaderValue(2.0f));
        Assert.Equal("2.5", InterceptHeaders.StringifyHeaderValue(2.5m));
        Assert.Equal("2.5", InterceptHeaders.StringifyHeaderValue(2.5f));
        Assert.Equal("custom", InterceptHeaders.StringifyHeaderValue(new Custom()));
        Assert.Equal(
            "forbidden: unknown operation nope",
            new Provide.Uterm.Policy.StrictPolicyEngine().CanPerform("nope", "admin", leaseOwned: true, sessionActive: true));
        _ = CultureInfo.InvariantCulture;
    }

    private sealed class Custom
    {
        public override string ToString() => "custom";
    }
}
