//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"encoding/binary"
	"net"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/emulator"
	uptty "github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
)

// startedCapture builds and starts a capture connector on a fresh socket.
func startedCapture(t *testing.T, tag string, config map[string]any) (*captureConnector, string) {
	t.Helper()
	sock := captureSock(t, tag)
	config["socket_path"] = sock
	c, err := newCapture("pam-u-capture-1-2", "d", config)
	if err != nil {
		t.Fatal(err)
	}
	return c, sock
}

func start(t *testing.T, c *captureConnector) {
	t.Helper()
	ctx := context.Background()
	if err := c.Start(ctx); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = c.Stop(ctx) })
}

// sendGeometry writes a geometry frame, as the pty's owner does on a resize.
func sendGeometry(t *testing.T, path string, cols, rows int) {
	t.Helper()
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	frame := []byte{uptty.ChannelGeometry, 0, 0, 0, 4, 0, 0, 0, 0}
	binary.BigEndian.PutUint16(frame[5:], uint16(cols))
	binary.BigEndian.PutUint16(frame[7:], uint16(rows))
	if _, err := conn.Write(frame); err != nil {
		t.Fatal(err)
	}
}

func eventually(t *testing.T, what string, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatalf("never: %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestACaptureKeepsNoScrollbackUntilAskedFor(t *testing.T) {
	c, sock := startedCapture(t, "nosb", map[string]any{"cols": 10, "rows": 2})
	start(t, c)
	sendCapture(t, sock, "a\nb\nc\nd")
	eventually(t, "the output is drawn", func() bool { return strings.Contains(c.Snapshot().Screen, "d") })
	if got := c.Scrollback(); len(got) != 0 {
		t.Fatalf("scrollback = %q", got)
	}
}

func TestACaptureKeepsTheNewestScrollbackUpToItsBound(t *testing.T) {
	c, sock := startedCapture(t, "sb", map[string]any{"cols": 10, "rows": 2})
	c.KeepScrollback(2, nil)
	start(t, c)
	sendCapture(t, sock, "one\ntwo\nthree\nfour\nfive")
	eventually(t, "three rows scroll off", func() bool { return reflect.DeepEqual(c.Scrollback(), []string{"two", "three"}) })
	c.KeepScrollback(1, c.Scrollback())
	if got := c.Scrollback(); !reflect.DeepEqual(got, []string{"three"}) {
		t.Fatalf("after a smaller bound: %q", got)
	}
}

func TestAResizeDoesNotScrollTheReplayIntoTheScrollbackAgain(t *testing.T) {
	c, sock := startedCapture(t, "resize", map[string]any{"cols": 10, "rows": 2})
	c.KeepScrollback(10, nil)
	start(t, c)
	sendCapture(t, sock, "one\ntwo\nthree")
	eventually(t, "one scrolls off", func() bool { return reflect.DeepEqual(c.Scrollback(), []string{"one"}) })
	sendGeometry(t, sock, 12, 2)
	eventually(t, "the resize", func() bool { return c.Snapshot().Cols == 12 })
	sendCapture(t, sock, "\nfour")
	eventually(t, "two scrolls off", func() bool { return strings.Contains(c.Snapshot().Screen, "four") })
	if got := c.Scrollback(); !reflect.DeepEqual(got, []string{"one", "two"}) {
		t.Fatalf("scrollback = %q", got)
	}
}

func TestClearingACaptureEmptiesItsScrollback(t *testing.T) {
	c, sock := startedCapture(t, "clear", map[string]any{"cols": 10, "rows": 1})
	c.KeepScrollback(10, nil)
	start(t, c)
	sendCapture(t, sock, "a\nb")
	eventually(t, "a scrolls off", func() bool { return len(c.Scrollback()) == 1 })
	if err := c.Clear(); err != nil {
		t.Fatal(err)
	}
	if got := c.Scrollback(); len(got) != 0 {
		t.Fatalf("scrollback = %q", got)
	}
}

// A restored screen paints back exactly, at its own size, with its
// scrollback; output after it lands where the program left its cursor.
func TestARestoredCaptureStartsFromItsScreen(t *testing.T) {
	live := emulator.NewWithEncoding(20, 4, "", emulator.UTF8)
	live.Process([]byte("\x1b[1;31mred\x1b[0m line\r\n\x1b[2;5Hmoved\x1b[?25l"))
	saved := live.ANSIScreen()

	c, sock := startedCapture(t, "restore", map[string]any{})
	c.KeepScrollback(10, []string{"older", "old"})
	c.RestoreScreen(saved, 20, 4)
	start(t, c)
	if got := c.Session().ANSIScreen(); got != saved {
		t.Fatalf("restored screen = %q, want %q", got, saved)
	}
	if snap := c.Snapshot(); snap.Cols != 20 || snap.Rows != 4 {
		t.Fatalf("restored at %dx%d", snap.Cols, snap.Rows)
	}
	if got := c.Scrollback(); !reflect.DeepEqual(got, []string{"older", "old"}) {
		t.Fatalf("scrollback = %q", got)
	}
	sendCapture(t, sock, "!")
	live.Process([]byte("!"))
	eventually(t, "the output after the restore", func() bool { return c.Session().ANSIScreen() == live.ANSIScreen() })
}

// The restored screen is the output since the last clear, so a resize before
// the program clears again redraws it; and it is painted once only.
func TestARestoredScreenIsRedrawnOnAResizeAndPaintedOnce(t *testing.T) {
	c, sock := startedCapture(t, "redraw", map[string]any{})
	c.RestoreScreen("kept on screen", 20, 3)
	start(t, c)
	sendGeometry(t, sock, 30, 3)
	eventually(t, "the resize", func() bool { return c.Snapshot().Cols == 30 })
	if first := strings.Split(c.Snapshot().Screen, "\n")[0]; !strings.HasPrefix(first, "kept on screen") {
		t.Fatalf("after the resize: %q", first)
	}
	ctx := context.Background()
	_ = c.Stop(ctx)
	if err := c.Start(ctx); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(c.Snapshot().Screen, "kept on screen") {
		t.Fatal("a second start painted the restored screen again")
	}
}

func TestRowsScrolledOffAfterARestoreJoinItsScrollback(t *testing.T) {
	c, sock := startedCapture(t, "join", map[string]any{})
	c.KeepScrollback(10, []string{"earlier"})
	c.RestoreScreen("top\nmiddle\nbottom", 10, 3)
	start(t, c)
	sendCapture(t, sock, "\nnext")
	eventually(t, "top scrolls off", func() bool { return len(c.Scrollback()) == 2 })
	if got := c.Scrollback(); !reflect.DeepEqual(got, []string{"earlier", "top"}) {
		t.Fatalf("scrollback = %q", got)
	}
}

// The scrollback carries over every attach: rows kept before are seeded,
// the newest up to the bound.
func TestScrollbackKeptBeforeIsSeededUpToTheBound(t *testing.T) {
	c, _ := startedCapture(t, "seed", map[string]any{})
	c.KeepScrollback(2, []string{"a", "b", "c"})
	if got := c.Scrollback(); !reflect.DeepEqual(got, []string{"b", "c"}) {
		t.Fatalf("scrollback = %q", got)
	}
}

// A full ring overwrites its oldest row in place: a session that scrolls a
// million rows allocates nothing per row.
func TestAFullScrollbackAllocatesNothingPerRow(t *testing.T) {
	ring := newScrollbackRing(3)
	for _, row := range []string{"a", "b", "c", "d"} {
		ring.push(row)
	}
	if got := ring.rows(); !reflect.DeepEqual(got, []string{"b", "c", "d"}) {
		t.Fatalf("rows = %q", got)
	}
	if allocs := testing.AllocsPerRun(1000, func() { ring.push("row") }); allocs != 0 {
		t.Fatalf("%v allocations per row", allocs)
	}
	empty := newScrollbackRing(0)
	empty.push("x")
	if len(empty.rows()) != 0 {
		t.Fatal("a ring of no rows kept one")
	}
}

// With no scrollback kept the emulator is given no sink, so a row is not
// rendered only to be dropped.
func TestNoSinkIsSetWhenNoScrollbackIsKept(t *testing.T) {
	c, _ := startedCapture(t, "nosink", map[string]any{})
	emu := emulator.NewWithEncoding(10, 1, "", emulator.UTF8)
	var stray []string
	emu.SetScrollbackSink(func(line string) { stray = append(stray, line) })
	c.watchScrollback(emu)
	emu.Process([]byte("a\r\nb"))
	if len(stray) != 0 {
		t.Fatalf("the sink stayed: %q", stray)
	}
	c.KeepScrollback(5, nil)
	c.watchScrollback(emu)
	emu.Process([]byte("\r\nc"))
	if got := c.Scrollback(); !reflect.DeepEqual(got, []string{"b"}) {
		t.Fatalf("scrollback = %q", got)
	}
}

func TestACaptureBuiltThroughBuildOffersItsState(t *testing.T) {
	conn, err := Build("s", "n", "pty_capture", map[string]any{"socket_path": "/tmp/x.sock"})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := conn.(CaptureState); !ok {
		t.Fatalf("%T is not a CaptureState", conn)
	}
	if _, err := Build("s", "n", "pty_capture", map[string]any{}); err == nil {
		t.Fatal("a capture with no socket was built")
	}
}
