//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vt"
)

// conformance/snapshot/wide_resume.json holds each stream and the snapshot the
// Python emulator takes of it in a UTF-8 session. This package writes those
// bytes, and a screen painted from a snapshot at any point and fed the rest
// ends where one fed the whole stream does. The Go emulator decodes CP437, so
// the snapshot is taken here, from the screen a UTF-8 session would hold.

type wideResumeRecord struct {
	Name       string `json:"name"`
	Cols       int    `json:"cols"`
	Rows       int    `json:"rows"`
	Stream     string `json:"stream"`
	ANSIScreen string `json:"ansi_screen"`
}

// characterToken is one terminal token, over runes: a snapshot is never taken
// inside a character or an escape sequence.
var characterToken = regexp.MustCompile(`(?s)\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b.|.`)

func loadWideResume(t *testing.T) []wideResumeRecord {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	rel := filepath.Join("conformance", "snapshot", "wide_resume.json")
	for range 8 {
		if raw, readErr := os.ReadFile(filepath.Join(dir, rel)); readErr == nil {
			var corpus struct {
				Records []wideResumeRecord `json:"records"`
			}
			if err := json.Unmarshal(raw, &corpus); err != nil {
				t.Fatal(err)
			}
			return corpus.Records
		}
		dir = filepath.Dir(dir)
	}
	t.Fatalf("could not locate %s above the package directory", rel)
	return nil
}

// utf8Session is a screen fed as a UTF-8 session's is: designations and SO/SI
// honoured, which is what the snapshot's charsets flag says.
func utf8Session(cols, rows int, stream string) (*vt.Screen, *vt.Stream) {
	s := vt.NewScreen(cols, rows)
	st := vt.NewStream(s)
	st.UseUTF8 = false
	st.Feed(stream)
	return s, st
}

func snapshotOf(s *vt.Screen) string {
	return ScreenStatePrefix(s) + strings.Join(RenderScreenLines(s, s.Columns(), s.Lines()), "\n") + ScreenStateSuffix(s, true)
}

type wideState struct {
	display []string
	x, y    int
	ansi    string
}

func wideStateOf(s *vt.Screen) wideState {
	return wideState{s.Display(), s.Cursor().X, s.Cursor().Y, snapshotOf(s)}
}

func TestWideResumeWritesTheRecordedSnapshot(t *testing.T) {
	records := loadWideResume(t)
	if len(records) < 15 {
		t.Fatalf("only %d records", len(records))
	}
	for _, record := range records {
		s, _ := utf8Session(record.Cols, record.Rows, record.Stream)
		if got := snapshotOf(s); got != record.ANSIScreen {
			t.Errorf("%s:\n got %q\nwant %q", record.Name, got, record.ANSIScreen)
		}
	}
}

func TestWideResumeResumesExactlyAtEveryPoint(t *testing.T) {
	for _, record := range loadWideResume(t) {
		whole, _ := utf8Session(record.Cols, record.Rows, record.Stream)
		want := wideStateOf(whole)
		splits := []int{0}
		for _, loc := range characterToken.FindAllStringIndex(record.Stream, -1) {
			splits = append(splits, loc[1])
		}
		for _, split := range splits {
			live, _ := utf8Session(record.Cols, record.Rows, record.Stream[:split])
			viewer, st := utf8Session(record.Cols, record.Rows, strings.ReplaceAll(snapshotOf(live), "\n", "\r\n"))
			st.Feed(record.Stream[split:])
			if got := wideStateOf(viewer); !reflect.DeepEqual(got, want) {
				t.Errorf("%s: split at byte %d:\n got %+v\nwant %+v", record.Name, split, got, want)
			}
		}
	}
}

func TestRenderDrawsAWideCharacterOnceWithNothingForItsSecondCell(t *testing.T) {
	s, _ := utf8Session(6, 1, "\u4e2da")
	if got := RenderScreenLines(s, 6, 1); !reflect.DeepEqual(got, []string{"\x1b[0m\u4e2da   \x1b[0m"}) {
		t.Fatalf("got %q", got)
	}
}

func TestRenderLeavesBlankALostFirstHalfWithNoCellLeftOfIt(t *testing.T) {
	// Deleting a wide character's first half leaves its second cell empty.
	// Two such cells in the last two columns: the first is recreated and the
	// second, with no cell left of it to recreate it from, left blank.
	// An insert shifted the first of them there.
	s, _ := utf8Session(4, 1, "a\u4e2d\x1b[1;2Hb\x1b[1;3H\x1b[@\x1b[1;2H\u4e2d\x1b[1;2Hc")
	if got := RenderScreenLines(s, 4, 1); !reflect.DeepEqual(got, []string{"\x1b[0mac\u3000\x1b[3G\x1b[P\x1b[C\x1b[0m"}) {
		t.Fatalf("got %q", got)
	}
	one, _ := utf8Session(2, 1, "\u4e2d\x1b[1;1H\x1b[P")
	if got := RenderScreenLines(one, 1, 1); !reflect.DeepEqual(got, []string{"\x1b[0m"}) {
		t.Fatalf("one column: got %q", got)
	}
}

// A resize can leave the cursor deferred past the last column over two empty
// second halves; the cell redrawn there is drawn as a blank, as the reference
// draws an empty cell.
func TestCursorPlacementDrawsAnEmptyCellAsABlank(t *testing.T) {
	s, _ := utf8Session(4, 1, "\u4e2d\u4e2d\x1b[1;1H\x1b[P\x1b[1;2H\x1b[P\x1b[1;3H")
	s.Resize(1, 2)
	if got := cursorPlacement(s, 1); got != "\x1b[1;1H\x1b[0m " {
		t.Fatalf("got %q", got)
	}
}

// On a one-column screen an empty second half has no cell left of it and no
// neighbouring row: it is drawn as a blank, as the reference draws it.
func TestCursorPlacementOnAOneColumnScreen(t *testing.T) {
	s, _ := utf8Session(2, 1, "\u4e2d\x1b[1;1H\x1b[P\x1b[1;2H")
	s.Resize(1, 1)
	if got := cursorPlacement(s, 1); got != "\x1b[1;1H\x1b[0m " {
		t.Fatalf("got %q", got)
	}
}
