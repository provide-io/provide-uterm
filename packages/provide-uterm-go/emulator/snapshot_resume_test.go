//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package emulator

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
)

// A snapshot must leave a viewer's terminal where the live stream expects it.
// Port of test_emulator_snapshot_resume.py: snapshot at a split point, paint a
// fresh emulator with ANSIScreen(), feed the remainder, and the result must
// equal the uninterrupted stream's. A program that paints with relative
// movement (vitetris never sends CUP) drew its whole board in the wrong place
// when the snapshot left the cursor where the painted rows ended.

// tokenRE is one terminal token: a CSI sequence, a charset designation, any
// other escape, or a single byte. Splits fall between tokens: a viewer handed
// half an escape sequence would print the other half whatever the snapshot
// said.
var tokenRE = regexp.MustCompile(`(?s)\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b.|.`)

func splitPoints(data []byte) []int {
	var points []int
	for _, loc := range tokenRE.FindAllIndex(data, -1) {
		points = append(points, loc[1])
	}
	return points
}

// fixturePath walks up from the package directory to the root-level fixture.
func fixturePath(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	rel := filepath.Join("conformance", "snapshot", "relative_paint_80x24.bin")
	for range 8 {
		candidate := filepath.Join(dir, rel)
		if _, statErr := os.Stat(candidate); statErr == nil {
			return candidate
		}
		dir = filepath.Dir(dir)
	}
	t.Fatalf("could not locate %s above the package directory", rel)
	return ""
}

type termState struct {
	screen string
	x, y   int
	ansi   string
}

func stateOf(e *TerminalEmulator) termState {
	snap := e.GetSnapshot()
	return termState{snap.Screen, snap.Cursor.X, snap.Cursor.Y, e.ANSIScreen()}
}

// paint is what the browser element writes for a snapshot frame's screen,
// encoded back to the CP437 bytes this emulator decodes.
func paint(snapshot string) []byte {
	return screen.EncodeCP437(strings.ReplaceAll(snapshot, "\n", "\r\n"))
}

func assertResumes(t *testing.T, data []byte, split, cols, rows int) {
	t.Helper()
	whole := New(cols, rows, "")
	whole.Process(data)
	live := New(cols, rows, "")
	live.Process(data[:split])
	viewer := New(cols, rows, "")
	viewer.Process(paint(live.ANSIScreen()))
	viewer.Process(data[split:])
	if got, want := stateOf(viewer), stateOf(whole); got != want {
		t.Fatalf("split at byte %d:\n got %+v\nwant %+v", split, got, want)
	}
}

func suffixOf(e *TerminalEmulator) string {
	ansi := e.ANSIScreen()
	return ansi[strings.LastIndex(ansi, "\n")+1:]
}

func fed(cols, rows int, stream string) *TerminalEmulator {
	e := New(cols, rows, "")
	e.Process([]byte(stream))
	return e
}

func TestSnapshotResumesTheRelativePaintFixture(t *testing.T) {
	data, err := os.ReadFile(fixturePath(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(data) != 4634 {
		t.Fatalf("fixture is %d bytes, want 4634", len(data))
	}
	points := splitPoints(data)
	if len(points) < 1500 {
		t.Fatalf("only %d split points", len(points))
	}
	for _, split := range append([]int{0}, points...) {
		assertResumes(t, data, split, 80, 24)
	}
}

func TestSnapshotRestoresCursorAndPen(t *testing.T) {
	stream := "top\r\n\x1b[2Bmid\x1b[3D"
	if got := suffixOf(fed(20, 5, stream)); !strings.Contains(got, "\x1b[4;1H") {
		t.Fatalf("suffix %q", got)
	}
	assertResumes(t, []byte(stream+"\x1b[1Aup"), len(stream), 20, 5)

	if got := suffixOf(fed(20, 5, "\x1b[?25l")); !strings.HasSuffix(got, "\x1b[?25l") {
		t.Fatalf("suffix %q", got)
	}
	pen := fed(20, 5, "\x1b[1;3;4;5;7;9;38;5;208;48;2;1;2;3mX")
	if got := suffixOf(pen); !strings.Contains(got, "\x1b[0;1;3;4;5;7;9;38;2;255;135;0;48;2;1;2;3m") {
		t.Fatalf("suffix %q", got)
	}
	before := "\x1b[44;1;37m\x1b[5C"
	assertResumes(t, []byte(before+"\x1b[1Bboard"), len(before), 20, 5)
}

func TestSnapshotRestoresTheScrollRegionAndOriginMode(t *testing.T) {
	if got := suffixOf(fed(20, 10, "\x1b[3;7r\x1b[?6h\x1b[2;4H")); !strings.Contains(got, "\x1b[3;7r\x1b[?6h\x1b[?7h\x1b[4l\x1b[2;4H") {
		t.Fatalf("suffix %q", got)
	}
	if got := suffixOf(fed(20, 10, "\x1b[1;10r")); !strings.Contains(got, " \x1b[0m\x1b[r\x1b[?6l") {
		t.Fatalf("suffix %q", got)
	}
	for _, origin := range []string{"", "\x1b[?6h"} {
		stream := []byte("line1\r\n\x1b[2;5r" + origin + "\x1b[3Bin\r\n\r\n\r\nscrolled\r\nagain")
		for _, split := range splitPoints(stream) {
			assertResumes(t, stream, split, 20, 8)
		}
	}
}

func TestSnapshotKeepsTheDeferredWrap(t *testing.T) {
	stream := "\x1b[2;1H" + strings.Repeat("x", 19) + "\x1b[32mY"
	live := fed(20, 4, stream)
	if c := live.GetSnapshot().Cursor; c.X != 20 || c.Y != 1 {
		t.Fatalf("cursor %+v", c)
	}
	if got := suffixOf(live); !strings.Contains(got, "\x1b[2;20H\x1b[0;32mY") {
		t.Fatalf("suffix %q", got)
	}
	full := []byte(stream + "\x1b[35mwrapped")
	for _, split := range splitPoints(full) {
		assertResumes(t, full, split, 20, 4)
	}
	origin := "\x1b[2;4r\x1b[?6h\x1b[2;1H" + strings.Repeat("z", 20)
	if got := suffixOf(fed(20, 6, origin)); !strings.Contains(got, "\x1b[2;20H") {
		t.Fatalf("suffix %q", got)
	}
	assertResumes(t, []byte(origin+"next"), len(origin), 20, 6)
	noWrap := "\x1b[?7l" + strings.Repeat("a", 20)
	if got := suffixOf(fed(20, 3, noWrap)); !strings.Contains(got, "\x1b[?7l") {
		t.Fatalf("suffix %q", got)
	}
	assertResumes(t, []byte(noWrap+"bc"), len(noWrap), 20, 3)
}

func TestSnapshotRestoresModes(t *testing.T) {
	insert := "abcdef\r\x1b[4h"
	if got := suffixOf(fed(20, 3, insert)); !strings.Contains(got, "\x1b[4h") {
		t.Fatalf("suffix %q", got)
	}
	assertResumes(t, []byte(insert+"XY"), len(insert), 20, 3)
	lnm := "\x1b[20h\x1b[5Cab"
	if got := suffixOf(fed(20, 3, lnm)); !strings.Contains(got, "\x1b[20h") {
		t.Fatalf("suffix %q", got)
	}
	assertResumes(t, []byte(lnm+"\ncd"), len(lnm), 20, 3)
	input := fed(20, 3, "\x1b[?1h\x1b[?2004h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h")
	if got := suffixOf(input); !strings.Contains(got, "\x1b[?1h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h") {
		t.Fatalf("suffix %q", got)
	}
	alt := fed(20, 3, "\x1b[?1049hgame")
	if got := alt.ANSIScreen(); !strings.HasPrefix(got, "\x1b[?1049h\x1b[H") {
		t.Fatalf("ansi %q", got)
	}
	alt.Process([]byte("\x1b[?1049l"))
	if got := alt.ANSIScreen(); strings.HasPrefix(got, "\x1b[?") {
		t.Fatalf("ansi %q", got)
	}
}

func TestSnapshotOfAFreshScreen(t *testing.T) {
	want := fmt.Sprintf("\x1b[0m%s\x1b[0m\x1b[r\x1b[?6l\x1b[?7h\x1b[4l\x1b[1;1H\x1b[20l\x1b[?1l\x1b[0m\x1b[?25h", strings.Repeat(" ", 20))
	if got := suffixOf(New(20, 3, "")); got != want {
		t.Fatalf("suffix %q want %q", got, want)
	}
}
