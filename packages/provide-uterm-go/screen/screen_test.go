//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package screen

import (
	"reflect"
	"strings"
	"testing"
)

// Expected values in this file were cross-checked against CPython's
// provide.uterm.screen (see the differential-test note in cp437.go); the
// bare-SGR cases in particular were verified verbatim against
// normalize_terminal_text.

func TestNormalizeTerminalText(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"empty string", "", ""},
		{"plain text unchanged", "hello world", "hello world"},
		{"removes csi sequence", "\x1b[1;31mred\x1b[0m", "red"},
		{"removes ansi codes", "\x1b[1;36mhello\x1b[0m", "hello"},
		{"complex ansi sequence", "\x1b[1;2;3;4;5;6;7;8;9mtext", "text"},
		{"normalizes crlf", "a\r\nb", "a\nb"},
		{"normalizes lone cr", "line1\rline2", "line1\nline2"},
		{"mixed line endings", "a\r\nb\rc\nd", "a\nb\nc\nd"},
		{"preserves newlines", "line1\nline2\nline3", "line1\nline2\nline3"},
		{"iac bytes pass through", "\u00ff\u00fb\x01text", "\u00ff\u00fb\x01text"},
		{"esc single char sequence", "a\x1bMb", "a" + "b"},
		{"dangling esc bracket", "\x1b[", ""},
		{"esc m not a sequence", "a\x1bmb", "a\x1bmb"},

		// _BARE_SGR_LINE_PREFIX_RE cases: ^(\d{1,3}(;\d{1,3})*)m(?=[A-Z<])
		{"bare sgr prefix at start before uppercase", "1;31mSOME TEXT", "SOME TEXT"},
		{"bare sgr prefix before angle bracket", "0m<X", "<X"},
		{"bare sgr prefix after newline", "line\n42mREADY", "line\nREADY"},
		{"bare sgr prefix lowercase not stripped", "1;31msome text", "1;31msome text"},
		{"bare sgr prefix mid line not stripped", "x1;31mSOME", "x1;31mSOME"},
		{"bare sgr prefix at end of string not stripped", "1;31m", ""}, // prefix RE misses; bare RE removes ($)

		// _BARE_SGR_RE cases: isolated fragments between whitespace/start and esc/whitespace/end
		{"bare sgr alone", "1;31m", ""},
		{"bare sgr followed by newline stays isolated", "1;31m\n2;32mtext", "\n2;32mtext"},
		{"bare sgr before esc", "before\n1;31m\x1bmore", "before\n\x1bmore"},
		{"bare sgr between spaces", "x 1;31m y", "x  y"},
		{"multiple bare sgr separated by space", "1;31m 2;32m x", "  x"},
		{"adjacent bare sgr not isolated", "1;31m2;32m x", "1;31m2;32m x"},
		{"four digit group not stripped", "1234m ", "1234m "},
		{"three digit groups stripped", "12;345;6m ", " "},
		{"empty group not stripped", "1;;2m ", "1;;2m "},
		{"leading semicolon not stripped", ";1m ", ";1m "},
		{"trailing semicolon not stripped", "1;m ", "1;m "},
		{"digits without m at end not stripped", "x 12", "x 12"},
		{"bare m alone not stripped", "m ", "m "},
		{"bare sgr after tab", "\t7m\t", "\t\t"},
		{"bare sgr after nbsp", " 7m\t", " \t"},
		{"bare sgr around info separator", "\x1c7m\x1c", "\x1c\x1c"},
		{"unicode digits stripped", "١٢m ", " "},
		{"bare sgr after cr normalized first", "x\r1;31m y", "x\n y"},
		{"bare sgr before end after trailing newline", "x 31m\n", "x \n"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := NormalizeTerminalText(tt.in); got != tt.want {
				t.Errorf("NormalizeTerminalText(%q) = %q, want %q", tt.in, got, tt.want)
			}
		})
	}
}

func TestNormalizeTerminalTextBareSGRNotReplacedWithFiller(t *testing.T) {
	// Port of the Python mutation-killing tests: fragments must be REMOVED.
	for _, in := range []string{"1;31mSOME TEXT", "before\n1;31m\x1bmore"} {
		got := NormalizeTerminalText(in)
		if strings.Contains(got, "1;31m") {
			t.Errorf("NormalizeTerminalText(%q) = %q, bare SGR should be removed", in, got)
		}
	}
}

func TestStripANSI(t *testing.T) {
	if got := StripANSI("\x1b[1;31mred\x1b[0m"); got != "red" {
		t.Errorf("StripANSI = %q, want %q", got, "red")
	}
	if got := StripANSI("hello world"); got != "hello world" {
		t.Errorf("StripANSI plain = %q", got)
	}
	if got := StripANSI(""); got != "" {
		t.Errorf("StripANSI empty = %q", got)
	}
	if got := StripANSI("line1\r\nline2"); got != "line1\nline2" {
		t.Errorf("StripANSI crlf = %q", got)
	}
}

func TestExtractActionTags(t *testing.T) {
	tests := []struct {
		name    string
		in      string
		maxTags int
		want    []string
	}{
		{"single tag", "Press <Move> to continue", DefaultMaxActionTags, []string{"Move"}},
		{"multiple tags", "<Attack> or <Retreat>", DefaultMaxActionTags, []string{"Attack", "Retreat"}},
		{"deduplicates", "<Move> then <Move>", DefaultMaxActionTags, []string{"Move"}},
		{"case insensitive dedup", "<Move> and <move>", DefaultMaxActionTags, []string{"Move"}},
		{"duplicate then new tag continues", "<A> then <A> then <B>", DefaultMaxActionTags, []string{"A", "B"}},
		{"empty string", "", DefaultMaxActionTags, []string{}},
		{"no tags", "no tags here", DefaultMaxActionTags, []string{}},
		{"whitespace only tag skipped", "< > <Real>", DefaultMaxActionTags, []string{"Real"}},
		{"tag surrounding space stripped", "<  Padded  >", DefaultMaxActionTags, []string{"Padded"}},
		{"max tags zero clamps to one", "<Tag1> <Tag2> <Tag3>", 0, []string{"Tag1"}},
		{"max tags zero single", "<Only>", 0, []string{"Only"}},
		{"long tag accepted", "<" + strings.Repeat("x", 80) + ">", DefaultMaxActionTags, []string{strings.Repeat("x", 80)}},
		{"tag too long rejected", "<" + strings.Repeat("x", 81) + ">", DefaultMaxActionTags, []string{}},
		{"nested angle picks inner", "<a<b>", DefaultMaxActionTags, []string{"b"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ExtractActionTags(tt.in, tt.maxTags); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("ExtractActionTags(%q, %d) = %v, want %v", tt.in, tt.maxTags, got, tt.want)
			}
		})
	}
}

func TestExtractActionTagsMaxTagsLimits(t *testing.T) {
	parts := make([]string, 0, 20)
	for i := range 20 {
		parts = append(parts, "<Tag"+string(rune('A'+i))+">")
	}
	text := strings.Join(parts, " ")
	if got := ExtractActionTags(text, 3); len(got) != 3 {
		t.Errorf("maxTags=3 returned %d tags: %v", len(got), got)
	}
	// Python default max_tags is 8, not 9 (mutation-killing assertion).
	if got := ExtractActionTags(text, DefaultMaxActionTags); len(got) != 8 {
		t.Errorf("default maxTags returned %d tags: %v", len(got), got)
	}
}

func TestCleanScreenForDisplay(t *testing.T) {
	padding := strings.Repeat(" ", 80)
	t.Run("returns content lines", func(t *testing.T) {
		got := CleanScreenForDisplay("line one\nline two\n", DefaultMaxScreenLines)
		want := []string{"line one", "line two", ""}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %q, want %q", got, want)
		}
	})
	t.Run("skips wide padding", func(t *testing.T) {
		got := CleanScreenForDisplay("content\n"+padding+"\nmore content", DefaultMaxScreenLines)
		want := []string{"content", "more content"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %q, want %q", got, want)
		}
	})
	t.Run("short whitespace lines included", func(t *testing.T) {
		got := CleanScreenForDisplay("line1\n  \nline3", DefaultMaxScreenLines)
		want := []string{"line1", "  ", "line3"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %q, want %q", got, want)
		}
	})
	t.Run("padding line with content included", func(t *testing.T) {
		got := CleanScreenForDisplay(padding+"x", DefaultMaxScreenLines)
		want := []string{padding + "x"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %q, want %q", got, want)
		}
	})
	t.Run("max lines enforced", func(t *testing.T) {
		lines := make([]string, 10)
		for i := range lines {
			lines[i] = "line " + string(rune('0'+i))
		}
		got := CleanScreenForDisplay(strings.Join(lines, "\n"), 3)
		want := []string{"line 0", "line 1", "line 2"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %q, want %q", got, want)
		}
	})
	t.Run("default max lines is 30 not 31", func(t *testing.T) {
		lines := make([]string, 31)
		for i := range lines {
			lines[i] = "content line"
		}
		got := CleanScreenForDisplay(strings.Join(lines, "\n"), DefaultMaxScreenLines)
		if len(got) != 30 {
			t.Errorf("default maxLines must cap at 30, got %d", len(got))
		}
	})
	t.Run("empty screen yields single empty line", func(t *testing.T) {
		// Python: "".split("\n") == [""] and "" passes the padding filter.
		got := CleanScreenForDisplay("", DefaultMaxScreenLines)
		if !reflect.DeepEqual(got, []string{""}) {
			t.Errorf("got %q, want [\"\"]", got)
		}
	})
}

func TestPySplitLines(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want []string
	}{
		{"empty", "", []string{}},
		{"no terminator", "abc", []string{"abc"}},
		{"lf", "a\nb", []string{"a", "b"}},
		{"crlf is one boundary", "a\r\nb", []string{"a", "b"}},
		{"lone cr", "a\rb", []string{"a", "b"}},
		{"trailing lf drops empty", "a\n", []string{"a"}},
		{"formfeed", "a\fb", []string{"a", "b"}},
		{"vertical tab", "a\vb", []string{"a", "b"}},
		{"group separator", "a\x1db", []string{"a", "b"}},
		{"nel and unicode separators", "a\u0085b\u2028c\u2029d", []string{"a", "b", "c", "d"}},
		{"cr at end", "a\r", []string{"a"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := pySplitLines(tt.in); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("pySplitLines(%q) = %q, want %q", tt.in, got, tt.want)
			}
		})
	}
}
