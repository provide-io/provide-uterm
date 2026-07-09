//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package screen

import "regexp"

// MenuOption is a (key, description) pair extracted from a menu screen.
type MenuOption struct {
	Key         string
	Description string
}

// NumberedItem is a (number, description) pair extracted from a numbered list.
type NumberedItem struct {
	Number      string
	Description string
}

// Custom-pattern note (applies to ExtractMenuOptions, ExtractNumberedList,
// and ExtractKeyValuePairs): user-supplied patterns are compiled with Go's
// regexp package, so RE2 syntax applies — an accepted deviation from
// Python's re. Patterns that fail to compile behave like Python's re.error
// handling (the empty/partial result so far is returned, or the field is
// skipped). Where Python would raise on a missing capture group
// (IndexError) or crash on an unmatched optional group (None.strip()), the
// Go port skips the match instead; Go also reports an unmatched optional
// group as "" rather than None.

// ExtractMenuOptions extracts menu options from screen text.
//
// With pattern == "" it uses the Python default pattern
//
//	[<\[\(]([A-Z0-9])[>\]\)]\s+([^<\[\(\n]+?)(?=\s*[<\[\(]|$)
//
// which supports formats like "<A> Option", "[A] Option", "(A) Option"
// (including multiple options on one line). RE2 has no lookahead, so the
// default pattern is implemented as a manual scan replicating Python's
// backtracking. A non-empty pattern must be a Go regexp with two capture
// groups (key, description); see the custom-pattern note above.
//
// Port of extract_menu_options.
func ExtractMenuOptions(screen string, pattern string) []MenuOption {
	if pattern == "" {
		return extractMenuOptionsDefault(screen)
	}
	options := []MenuOption{}
	re, err := regexp.Compile(pattern)
	if err != nil {
		return options
	}
	for _, m := range re.FindAllStringSubmatch(screen, -1) {
		if len(m) < 3 {
			continue // fewer than two capture groups; Python would raise
		}
		if description := pyStrip(m[2]); description != "" {
			options = append(options, MenuOption{Key: m[1], Description: description})
		}
	}
	return options
}

// extractMenuOptionsDefault hand-implements the default menu pattern.
//
// Structure: opener [<[(], key [A-Z0-9], closer [>])], greedy \s+, lazy
// description [^<\[\(\n]+?, then the lookahead (?=\s*[<\[\(]|$). Python's
// backtracking order is: maximal whitespace first (shrinking on failure),
// description growing from one rune, first overall success wins. The
// lookahead is zero-width, so scanning resumes at the end of the
// description; Python's non-multiline $ matches at end of string or before
// a final '\n'. A match whose stripped description is empty is skipped but
// still advances the scan (exactly like Python).
func extractMenuOptionsDefault(screen string) []MenuOption {
	options := []MenuOption{}
	rs := []rune(screen)
	n := len(rs)
	for i := 0; i < n; {
		if !isMenuOpener(rs[i]) || i+2 >= n || !isMenuKey(rs[i+1]) || !isMenuCloser(rs[i+2]) {
			i++
			continue
		}
		wsStart := i + 3
		wsEnd := wsStart
		for wsEnd < n && isPySpace(rs[wsEnd]) {
			wsEnd++
		}
		matched := false
		for descStart := wsEnd; descStart > wsStart && !matched; descStart-- {
			for d := descStart + 1; d <= n; d++ {
				if c := rs[d-1]; c == '<' || c == '[' || c == '(' || c == '\n' {
					break // rune not allowed in the description class
				}
				if !menuLookahead(rs, d) {
					continue
				}
				if description := pyStrip(string(rs[descStart:d])); description != "" {
					options = append(options, MenuOption{Key: string(rs[i+1]), Description: description})
				}
				i = d
				matched = true
				break
			}
		}
		if !matched {
			i++
		}
	}
	return options
}

func isMenuOpener(r rune) bool { return r == '<' || r == '[' || r == '(' }
func isMenuCloser(r rune) bool { return r == '>' || r == ']' || r == ')' }

func isMenuKey(r rune) bool {
	return (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9')
}

// menuLookahead evaluates (?=\s*[<\[\(]|$) at rune index p.
func menuLookahead(rs []rune, p int) bool {
	q := p
	for q < len(rs) && isPySpace(rs[q]) {
		q++
	}
	if q < len(rs) && isMenuOpener(rs[q]) {
		return true
	}
	// Python's non-multiline $: end of string, or just before a final '\n'.
	return p == len(rs) || (p == len(rs)-1 && rs[p] == '\n')
}

// ExtractNumberedList extracts numbered lists from screen text, one match
// per line (lines split like Python's str.splitlines()).
//
// With pattern == "" it uses the Python default pattern
//
//	^\s*(\d+)[\.\)]\s+(.+)$
//
// supporting formats like "1. Item" and "1) Item". A non-empty pattern must
// be a Go regexp with two capture groups (number, description); see the
// custom-pattern note above.
//
// Port of extract_numbered_list.
func ExtractNumberedList(screen string, pattern string) []NumberedItem {
	items := []NumberedItem{}
	if pattern == "" {
		for _, line := range pySplitLines(screen) {
			number, rawDesc, ok := matchNumberedLine(line)
			if !ok {
				continue
			}
			if description := pyStrip(rawDesc); description != "" {
				items = append(items, NumberedItem{Number: number, Description: description})
			}
		}
		return items
	}
	re, err := regexp.Compile(pattern)
	if err != nil {
		return items
	}
	for _, line := range pySplitLines(screen) {
		m := re.FindStringSubmatch(line)
		if len(m) < 3 {
			continue // no match, or fewer than two capture groups
		}
		if description := pyStrip(m[2]); description != "" {
			items = append(items, NumberedItem{Number: m[1], Description: description})
		}
	}
	return items
}

// matchNumberedLine hand-implements re.search of ^\s*(\d+)[\.\)]\s+(.+)$ on
// a single line (which contains no line terminators). The leading ^ pins
// the only viable start to index 0. Backtracking collapses to: maximal
// digit run (digits cannot satisfy the following [\.\)], so shrinking never
// helps), then greedy \s+; if the remainder after the punctuation is all
// whitespace, \s+ gives one rune back so (.+) can match it — Python then
// yields a single-space description (stripped to "" by the caller).
func matchNumberedLine(line string) (number string, rawDesc string, ok bool) {
	rs := []rune(line)
	n := len(rs)
	i := 0
	for i < n && isPySpace(rs[i]) {
		i++
	}
	digStart := i
	for i < n && isPyDigit(rs[i]) {
		i++
	}
	if i == digStart || i >= n || (rs[i] != '.' && rs[i] != ')') {
		return "", "", false
	}
	number = string(rs[digStart:i])
	restStart := i + 1
	w := restStart
	for w < n && isPySpace(rs[w]) {
		w++
	}
	if w == restStart {
		return "", "", false // \s+ needs at least one whitespace rune
	}
	if w < n {
		return number, string(rs[w:]), true
	}
	if w-restStart < 2 {
		return "", "", false // one trailing space: nothing left for (.+)
	}
	return number, string(rs[n-1:]), true
}

// ExtractKeyValuePairs extracts key-value pairs from screen text using the
// provided patterns (field name → regex with one capture group), each
// searched case-insensitively. Patterns are Go regexps; see the
// custom-pattern note above. Invalid patterns are skipped, as are matches
// without a first capture group.
//
// Port of extract_key_value_pairs.
func ExtractKeyValuePairs(screen string, patterns map[string]string) map[string]string {
	data := map[string]string{}
	for field, pat := range patterns {
		re, err := regexp.Compile("(?i)" + pat)
		if err != nil {
			continue
		}
		m := re.FindStringSubmatch(screen)
		if len(m) >= 2 {
			data[field] = m[1]
		}
	}
	return data
}
