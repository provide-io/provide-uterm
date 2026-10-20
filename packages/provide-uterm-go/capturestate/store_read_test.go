//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package capturestate

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"testing/iotest"
	"time"

	"golang.org/x/sys/unix"
)

// Reading a record file back: what a load skips, and what it only notes.

func TestUnknownKeysAreNotedOnceAtLoadAndNotWarned(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	var top map[string]any
	_ = json.Unmarshal(validLive(t), &top)
	session := top["session"].(map[string]any)
	session["surprise"], session["another"] = 1, 2
	data, _ := json.Marshal(top)
	writeLive(t, s, sid+".json", data)
	if got := s.LoadLive(); len(got) != 1 {
		t.Fatalf("read %v", got)
	}
	if !strings.Contains(logs.String(), "level=INFO msg=capture_state_keys_ignored") ||
		!strings.Contains(logs.String(), "keys=another,surprise") || strings.Contains(logs.String(), "level=WARN") {
		t.Fatalf("logs %s", logs.String())
	}
}

func TestANewerFormatIsSkippedWithAWarning(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	var top map[string]any
	_ = json.Unmarshal(validLive(t), &top)
	top["format"] = 2
	data, _ := json.Marshal(top)
	writeLive(t, s, sid+".json", data)
	if got := s.LoadLive(); len(got) != 0 || !strings.Contains(logs.String(), `error="newer format: 2"`) {
		t.Fatalf("read %v: %s", got, logs.String())
	}
}

// An I/O error on one file (a failing disk) costs that file, never the load.
func TestAnIOErrorOnOneFileSkipsOnlyThatFile(t *testing.T) {
	eio := &os.PathError{Op: "read", Path: "x", Err: syscall.EIO}
	for name, tc := range map[string]struct {
		swap func()
		want string
	}{
		"stat": {func() { statFile = func(*os.File) (os.FileInfo, error) { return nil, eio } }, "cannot stat: EIO"},
		"read": {func() { readerOf = func(*os.File) io.Reader { return iotest.ErrReader(eio) } }, "cannot read: EIO"},
	} {
		s, _, logs := newStore(t, true, 7, 10)
		writeLive(t, s, sid+".json", validLive(t))
		tc.swap()
		got := s.LoadLive()
		statFile, readerOf = (*os.File).Stat, func(f *os.File) io.Reader { return f }
		if len(got) != 0 || !strings.Contains(logs.String(), tc.want) {
			t.Errorf("%s: read %v: %s", name, got, logs.String())
		}
	}
}

func TestAFIFOIsRefusedWithoutWaitingForAWriter(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	if err := unix.Mkfifo(filepath.Join(privateDir(t, s.liveDir), sid+".json"), 0o600); err != nil {
		t.Fatal(err)
	}
	done := make(chan []*Record, 1)
	go func() { done <- s.LoadLive() }()
	select {
	case got := <-done:
		if len(got) != 0 || !strings.Contains(logs.String(), "not a regular file") {
			t.Fatalf("read %v: %s", got, logs.String())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the load waited on a FIFO")
	}
}

// A previous session whose file cannot be read reads as none, which its
// route answers 404, as a vanished file.
func TestAPreviousSessionThatCannotBeReadReadsAsNone(t *testing.T) {
	s, c, logs := newStore(t, true, 7, 10)
	endedAfter(t, s, c, sid, "alice", "last")
	readerOf = func(*os.File) io.Reader {
		return iotest.ErrReader(&os.PathError{Op: "read", Path: "x", Err: syscall.EIO})
	}
	defer func() { readerOf = func(f *os.File) io.Reader { return f } }()
	if rec, ok := s.PreviousSession(sid); ok || rec != nil || !strings.Contains(logs.String(), "cannot read: EIO") {
		t.Fatalf("read %v %v: %s", rec, ok, logs.String())
	}
}

func TestADirectoryThatCannotBeListedIsSkippedWithAWarning(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	writeLive(t, s, sid+".json", validLive(t))
	readDir = func(string) ([]os.DirEntry, error) {
		return nil, &os.PathError{Op: "readdirent", Path: "x", Err: syscall.EIO}
	}
	defer func() { readDir = os.ReadDir }()
	if got := s.LoadLive(); len(got) != 0 || !strings.Contains(logs.String(), `error="cannot list: EIO"`) {
		t.Fatalf("read %v: %s", got, logs.String())
	}
}

// A file that grew past the limit after it was checked is refused too.
func TestAFileThatGrowsWhileItIsReadIsRefused(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	writeLive(t, s, sid+".json", validLive(t))
	readerOf = func(*os.File) io.Reader { return strings.NewReader(strings.Repeat(" ", MaxRecordBytes+1)) }
	defer func() { readerOf = func(f *os.File) io.Reader { return f } }()
	if got := s.LoadLive(); len(got) != 0 || !strings.Contains(logs.String(), `error="too large"`) {
		t.Fatalf("read %v: %s", got, logs.String())
	}
}

// An error that carries no errno is named by its own text.
func TestAnErrorWithNoErrnoIsItsOwnText(t *testing.T) {
	if got := errnoName(errors.New("odd")); got != "odd" {
		t.Fatalf("errnoName = %q", got)
	}
}

func TestAFileWhoseDirectoryHasGoneIsNotWritten(t *testing.T) {
	err := writeFile(filepath.Join(t.TempDir(), "gone", sid+".json"), []byte("{}"))
	if !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("err = %v", err)
	}
}
