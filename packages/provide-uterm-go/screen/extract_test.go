//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package screen

import (
	"reflect"
	"testing"
)

func TestExtractMenuOptionsDefault(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want []MenuOption
	}{
		{"angle bracket menu", "<A> Option One  <B> Option Two",
			[]MenuOption{{"A", "Option One"}, {"B", "Option Two"}}},
		{"square bracket menu", "[X] Exit  [C] Continue",
			[]MenuOption{{"X", "Exit"}, {"C", "Continue"}}},
		{"paren menu", "(Q) Quit  (S) Save",
			[]MenuOption{{"Q", "Quit"}, {"S", "Save"}}},
		{"mixed bracket styles", "[X] Exit  (C) Continue",
			[]MenuOption{{"X", "Exit"}, {"C", "Continue"}}},
		{"multiple options one line", "<A> Alpha <B> Beta",
			[]MenuOption{{"A", "Alpha"}, {"B", "Beta"}}},
		{"empty screen", "", []MenuOption{}},
		{"digit key", "<1> First", []MenuOption{{"1", "First"}}},
		{"same key twice", "<Q> First\n<Q> Second",
			[]MenuOption{{"Q", "First"}, {"Q", "Second"}}},
		{"whitespace only description skipped", "<A>   <B> real option",
			[]MenuOption{{"B", "real option"}}},
		{"tab whitespace description skipped", "<A> \t <B> tab-desc",
			[]MenuOption{{"B", "tab-desc"}}},
		// Python's $ is non-multiline: text after the description on the
		// same line with no following opener means no match at all.
		{"trailing text after newline blocks match", "<A> Alpha\nMore",
			[]MenuOption{}},
		{"newline then next option matches", "<A> Alpha\n<B> Beta",
			[]MenuOption{{"A", "Alpha"}, {"B", "Beta"}}},
		{"dollar matches before final newline", "<A> Alpha\n",
			[]MenuOption{{"A", "Alpha"}}},
		{"mismatched bracket pair still matches", "<A] Weird stuff",
			[]MenuOption{{"A", "Weird stuff"}}},
		{"description may contain closers", "<A> Opt)ion] x <B> Two",
			[]MenuOption{{"A", "Opt)ion] x"}, {"B", "Two"}}},
		{"lowercase key not matched", "<a> nope", []MenuOption{}},
		{"missing closer not matched", "<AX nope", []MenuOption{}},
		{"no whitespace after closer not matched", "<A>nope", []MenuOption{}},
		{"opener at end of string", "text <", []MenuOption{}},
		{"truncated option at end", "<A", []MenuOption{}},
		{"whitespace to end of string no description", "<A>   ", []MenuOption{}},
		{"newline inside whitespace backtracks", "<A> \n<B> x",
			[]MenuOption{{"B", "x"}}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ExtractMenuOptions(tt.in, ""); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("ExtractMenuOptions(%q) = %v, want %v", tt.in, got, tt.want)
			}
		})
	}
}

func TestExtractMenuOptionsCustomPattern(t *testing.T) {
	t.Run("custom pattern", func(t *testing.T) {
		got := ExtractMenuOptions("1: First option\n2: Second option", `(\d+): (.+)`)
		want := []MenuOption{{"1", "First option"}, {"2", "Second option"}}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})
	t.Run("invalid pattern returns empty", func(t *testing.T) {
		got := ExtractMenuOptions("anything", `(invalid(?P<bad>`)
		if !reflect.DeepEqual(got, []MenuOption{}) {
			t.Errorf("got %v, want empty", got)
		}
	})
	t.Run("pattern with one group skipped", func(t *testing.T) {
		got := ExtractMenuOptions("1: First", `(\d+): .+`)
		if !reflect.DeepEqual(got, []MenuOption{}) {
			t.Errorf("got %v, want empty", got)
		}
	})
	t.Run("empty description skipped", func(t *testing.T) {
		got := ExtractMenuOptions("1:  \n2: real", `(\d+): (\w*)`)
		want := []MenuOption{{"2", "real"}}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})
}

func TestExtractNumberedListDefault(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want []NumberedItem
	}{
		{"dot format", "1. Alpha\n2. Beta\n3. Gamma",
			[]NumberedItem{{"1", "Alpha"}, {"2", "Beta"}, {"3", "Gamma"}}},
		{"paren format", "1) First\n2) Second",
			[]NumberedItem{{"1", "First"}, {"2", "Second"}}},
		{"no list", "no list here", []NumberedItem{}},
		{"empty screen", "", []NumberedItem{}},
		{"leading whitespace allowed", "  3)   spaced   ",
			[]NumberedItem{{"3", "spaced"}}},
		{"empty description skipped", "1.    \n2. real item",
			[]NumberedItem{{"2", "real item"}}},
		{"single trailing space no match", "1. \n2. ok",
			[]NumberedItem{{"2", "ok"}}},
		{"excludes empty descriptions", "1. Item\n2.\n3. Another",
			[]NumberedItem{{"1", "Item"}, {"3", "Another"}}},
		{"strips description", "1.  Description with trailing spaces   \n2. Another",
			[]NumberedItem{{"1", "Description with trailing spaces"}, {"2", "Another"}}},
		{"non digit prefix line skipped", "12x. foo\n4. ok",
			[]NumberedItem{{"4", "ok"}}},
		{"digits at line end no punct", "12\n1. ok",
			[]NumberedItem{{"1", "ok"}}},
		{"no space after punct", "1.x\n2. ok",
			[]NumberedItem{{"2", "ok"}}},
		{"formfeed splits lines", "5.\f6. formfeed-split",
			[]NumberedItem{{"6", "formfeed-split"}}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ExtractNumberedList(tt.in, ""); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("ExtractNumberedList(%q) = %v, want %v", tt.in, got, tt.want)
			}
		})
	}
}

func TestExtractNumberedListCustomPattern(t *testing.T) {
	t.Run("custom pattern", func(t *testing.T) {
		got := ExtractNumberedList("  Item 1 - description\n  Item 2 - other", `Item (\d+) - (.+)`)
		want := []NumberedItem{{"1", "description"}, {"2", "other"}}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})
	t.Run("invalid pattern returns empty", func(t *testing.T) {
		got := ExtractNumberedList("1. test", `(invalid(?P<bad>`)
		if !reflect.DeepEqual(got, []NumberedItem{}) {
			t.Errorf("got %v, want empty", got)
		}
	})
	t.Run("pattern with one group skipped", func(t *testing.T) {
		got := ExtractNumberedList("1. test", `(\d+)\. .+`)
		if !reflect.DeepEqual(got, []NumberedItem{}) {
			t.Errorf("got %v, want empty", got)
		}
	})
	t.Run("empty description skipped", func(t *testing.T) {
		got := ExtractNumberedList("1:  \n2: real", `(\d+): (\s*\w*)`)
		want := []NumberedItem{{"2", "real"}}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})
	t.Run("non matching lines skipped", func(t *testing.T) {
		got := ExtractNumberedList("nope\n1. yes", `^(\d+)\. (.+)$`)
		want := []NumberedItem{{"1", "yes"}}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})
}

func TestExtractKeyValuePairs(t *testing.T) {
	t.Run("basic extraction", func(t *testing.T) {
		got := ExtractKeyValuePairs("Credits: 5,000  Sector: 42", map[string]string{
			"credits": `Credits:\s*([\d,]+)`,
			"sector":  `Sector:\s*(\d+)`,
		})
		want := map[string]string{"credits": "5,000", "sector": "42"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})
	t.Run("missing field", func(t *testing.T) {
		got := ExtractKeyValuePairs("nothing here", map[string]string{"foo": `foo:\s*(\w+)`})
		if len(got) != 0 {
			t.Errorf("got %v, want empty", got)
		}
	})
	t.Run("case insensitive", func(t *testing.T) {
		got := ExtractKeyValuePairs("CREDITS: 100", map[string]string{"credits": `credits:\s*(\d+)`})
		if !reflect.DeepEqual(got, map[string]string{"credits": "100"}) {
			t.Errorf("got %v", got)
		}
	})
	t.Run("partial extraction", func(t *testing.T) {
		got := ExtractKeyValuePairs("Credits: 50  Health: not-found", map[string]string{
			"credits": `Credits:\s*(\d+)`,
			"health":  `Health:\s*(\d+)`,
		})
		want := map[string]string{"credits": "50"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})
	t.Run("empty patterns", func(t *testing.T) {
		got := ExtractKeyValuePairs("anything here", map[string]string{})
		if len(got) != 0 {
			t.Errorf("got %v, want empty", got)
		}
	})
	t.Run("invalid pattern skipped valid kept", func(t *testing.T) {
		got := ExtractKeyValuePairs("Credits: 100", map[string]string{
			"invalid": `(bad(?P<invalid>`,
			"valid":   `Credits:\s*(\d+)`,
		})
		want := map[string]string{"valid": "100"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("got %v, want %v", got, want)
		}
	})
	t.Run("pattern without capture group skipped", func(t *testing.T) {
		got := ExtractKeyValuePairs("Credits: 100", map[string]string{"credits": `Credits`})
		if len(got) != 0 {
			t.Errorf("got %v, want empty", got)
		}
	})
}
