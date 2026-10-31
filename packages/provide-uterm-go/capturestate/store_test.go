//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package capturestate

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

const sid = "pam-suokki-capture-4242-777"

const day = 24 * time.Hour

// clock is a settable wall clock for a store.
type clock struct{ now time.Time }

func (c *clock) Now() time.Time { return c.now }

func newStore(t *testing.T, persist bool, keepDays, lines int) (*Store, *clock, *bytes.Buffer) {
	t.Helper()
	logs := &bytes.Buffer{}
	s := New(filepath.Join(t.TempDir(), "state"), persist, keepDays, lines, slog.New(slog.NewTextHandler(logs, nil)))
	c := &clock{now: time.Unix(1_000_000, 0)}
	s.now = c.Now
	return s, c, logs
}

// save records def and writes it, as the registry does.
func save(s *Store, def serverconfig.SessionDefinition) {
	s.SetDefinition(def)
	s.Flush(def.SessionID)
}

// privateDir makes dir and its missing parents 0700, as the store makes its
// own.
func privateDir(t *testing.T, dir string) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	for d := dir; d != filepath.Dir(d) && strings.HasPrefix(d, os.TempDir()); d = filepath.Dir(d) {
		_ = os.Chmod(d, 0o700)
		if d == filepath.Dir(dir) {
			break
		}
	}
	_ = os.Chmod(dir, 0o700)
	return dir
}

func definition(id, owner string) serverconfig.SessionDefinition {
	return serverconfig.SessionDefinition{
		SessionID: id, DisplayName: "suokki (pts/3) [live]", ConnectorType: "pty_capture",
		ConnectorConfig: map[string]any{"socket_path": "/run/uterm-capture/s.sock"},
		InputMode:       "hijack", Tags: []string{"pam"}, Owner: &owner, Visibility: "private",
		CreatedAt:         time.Date(2026, 10, 19, 12, 0, 0, 0, time.UTC),
		AutoTransferIdleS: 30, KeystrokeQueue: "display",
	}
}

func liveFile(s *Store, id string) string     { return filepath.Join(s.liveDir, id+".json") }
func previousFile(s *Store, id string) string { return filepath.Join(s.previousDir, id+".json") }

func readJSON(t *testing.T, path string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return asMap(t, raw)
}

func mode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Mode().Perm()
}

func exists(path string) bool { _, err := os.Lstat(path); return err == nil }

func TestOnlyAPamCaptureSessionIsTracked(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	def := definition(sid, "alice")
	if !s.Tracks(def) {
		t.Fatal("a PAM capture is not tracked")
	}
	def.ConnectorType = "pty"
	if s.Tracks(def) || s.Tracks(definition("my-capture", "alice")) {
		t.Fatal("another session is tracked")
	}
}

func TestADefinitionIsWrittenOwnerOnlyInOwnerOnlyDirectories(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	if got := s.Track(definition(sid, "alice"), "inst-1"); got != "inst-1" {
		t.Fatalf("instance = %q", got)
	}
	if got := s.Track(definition(sid, "bob"), "inst-2"); got != "inst-1" {
		t.Fatalf("a second Track replaced the instance: %q", got)
	}
	save(s, definition(sid, "alice"))
	path := liveFile(s, sid)
	if mode(t, path) != 0o600 || mode(t, s.liveDir) != 0o700 || mode(t, s.dir) != 0o700 {
		t.Fatalf("modes %v %v %v", mode(t, path), mode(t, s.liveDir), mode(t, s.dir))
	}
	data := readJSON(t, path)
	if data["format"] != float64(1) || data["kind"] != "live" || data["recording_instance"] != "inst-1" ||
		data["screen"] != nil || !reflect.DeepEqual(data["scrollback"], []any{}) || data["saved_at"] != float64(0) {
		t.Fatalf("record = %v", data)
	}
	if _, has := data["ended_at"]; has {
		t.Fatal("a live record carries ended_at")
	}
	if !reflect.DeepEqual(data["login"], map[string]any{"pid": float64(4242), "start_ticks": float64(777)}) {
		t.Fatalf("login = %v", data["login"])
	}
}

func TestADirectoryLeftWithAWiderModeIsRefused(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	_ = os.Chmod(privateDir(t, s.liveDir), 0o755)
	s.Track(definition(sid, "alice"), "i")
	save(s, definition(sid, "alice"))
	if exists(liveFile(s, sid)) || !strings.Contains(logs.String(), "live: mode 0755, not 0700") {
		t.Fatalf("written anyway: %s", logs.String())
	}
}

func TestAScreenIsWrittenWithItsSizeAndTheNewestScrollback(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 2)
	s.Track(definition(sid, "alice"), "i")
	s.SaveScreen(sid, "a\nb", 100, 30, []string{"r1", "r2", "r3"})
	data := readJSON(t, liveFile(s, sid))
	if !reflect.DeepEqual(data["screen"], map[string]any{"ansi": "a\nb", "cols": float64(100), "rows": float64(30)}) {
		t.Fatalf("screen = %v", data["screen"])
	}
	if !reflect.DeepEqual(data["scrollback"], []any{"r2", "r3"}) || data["saved_at"] != float64(c.now.Unix()) {
		t.Fatalf("scrollback %v saved_at %v", data["scrollback"], data["saved_at"])
	}
}

func TestNoScrollbackIsKeptWhenPreviousSessionsAreOff(t *testing.T) {
	s, _, _ := newStore(t, true, 0, 50)
	if s.ScrollbackLines() != 0 {
		t.Fatalf("lines = %d", s.ScrollbackLines())
	}
	s.Track(definition(sid, "alice"), "i")
	s.SaveScreen(sid, "x", 80, 24, []string{"r1"})
	if got := readJSON(t, liveFile(s, sid))["scrollback"]; !reflect.DeepEqual(got, []any{}) {
		t.Fatalf("scrollback = %v", got)
	}
}

func TestNothingIsWrittenWithoutPersistOrForAnUntrackedSession(t *testing.T) {
	off, _, _ := newStore(t, false, 7, 10)
	off.Track(definition(sid, "alice"), "i")
	save(off, definition(sid, "alice"))
	off.SaveScreen(sid, "x", 80, 24, nil)
	untracked, _, _ := newStore(t, true, 7, 10)
	save(untracked, definition(sid, "alice"))
	untracked.SaveScreen(sid, "x", 80, 24, nil)
	untracked.End(sid)
	if exists(off.dir) || exists(untracked.dir) {
		t.Fatal("a file was written")
	}
}

func TestAFailedWriteLeavesTheOldFileWholeAndNoTemporaryFile(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	s.Track(definition(sid, "alice"), "i")
	s.SaveScreen(sid, "old", 80, 24, nil)
	before, _ := os.ReadFile(liveFile(s, sid))
	for name, fail := range map[string]func(){
		"rename": func() { renameFile = func(string, string) error { return errors.New("disk full") } },
		"sync":   func() { syncFile = func(*os.File) error { return errors.New("disk full") } },
	} {
		fail()
		s.SaveScreen(sid, "new", 80, 24, nil)
		renameFile, syncFile = os.Rename, (*os.File).Sync
		if after, _ := os.ReadFile(liveFile(s, sid)); !bytes.Equal(after, before) {
			t.Fatalf("%s: the old file changed", name)
		}
		if entries, _ := os.ReadDir(s.liveDir); len(entries) != 1 {
			t.Fatalf("%s: left %d files", name, len(entries))
		}
	}
	if !strings.Contains(logs.String(), "capture_state_write_failed") {
		t.Fatalf("no warning: %s", logs.String())
	}
}

func TestTheWriteIsSyncedBeforeItIsRenamedIntoPlace(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	s.Track(definition(sid, "alice"), "i")
	var order []string
	syncFile = func(f *os.File) error { order = append(order, "sync"); return f.Sync() }
	renameFile = func(from, to string) error { order = append(order, "rename"); return os.Rename(from, to) }
	defer func() { renameFile, syncFile = os.Rename, (*os.File).Sync }()
	save(s, definition(sid, "alice"))
	// The file, then the rename, then the directory that holds the rename.
	if !reflect.DeepEqual(order, []string{"sync", "rename", "sync"}) {
		t.Fatalf("order = %v", order)
	}
}

func TestADirectoryAnotherUserOwnsIsRefused(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	s.Track(definition(sid, "alice"), "i")
	privateDir(t, s.dir)
	geteuid = func() int { return os.Geteuid() + 1 }
	defer func() { geteuid = os.Geteuid }()
	save(s, definition(sid, "alice"))
	if exists(liveFile(s, sid)) || !strings.Contains(logs.String(), "state: not a directory this user owns") {
		t.Fatalf("written anyway: %s", logs.String())
	}
}

func TestADirectoryThatCannotBeMadeIsRefused(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	if err := os.WriteFile(s.dir, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	s.Track(definition(sid, "alice"), "i")
	save(s, definition(sid, "alice"))
	if !strings.Contains(logs.String(), "state: not a directory this user owns") {
		t.Fatalf("logs: %s", logs.String())
	}
	missing := New(filepath.Join(t.TempDir(), "a", "b"), true, 0, 0, slog.New(slog.NewTextHandler(logs, nil)))
	missing.Track(definition(sid, "alice"), "i")
	save(missing, definition(sid, "alice"))
	if exists(missing.dir) {
		t.Fatal("the missing parent was made")
	}
}

func TestScrollbackThatWouldOutgrowTheLimitIsDroppedBeforeTheScreen(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	s.Track(definition(sid, "alice"), "i")
	big := strings.Repeat("x", MaxRecordBytes/5)
	s.SaveScreen(sid, "kept", 80, 24, []string{big, big, big, big, big, big})
	data := readJSON(t, liveFile(s, sid))
	if data["screen"].(map[string]any)["ansi"] != "kept" || !reflect.DeepEqual(data["scrollback"], []any{}) {
		t.Fatal("the scrollback was not dropped")
	}
}

func TestAScreenTooLargeToReadBackIsNotWritten(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	s.Track(definition(sid, "alice"), "i")
	s.SaveScreen(sid, strings.Repeat("y", MaxRecordBytes), 80, 24, nil)
	if exists(liveFile(s, sid)) || !strings.Contains(logs.String(), "is too large") {
		t.Fatalf("written anyway: %s", logs.String())
	}
}

func endedAfter(t *testing.T, s *Store, c *clock, id, owner, screen string) {
	t.Helper()
	s.Track(definition(id, owner), "i")
	s.SaveScreen(id, screen, 80, 24, []string{"above"})
	c.now = c.now.Add(time.Second)
	s.End(id)
}

func TestAnEndedSessionBecomesAPreviousSession(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	endedAfter(t, s, c, sid, "alice", "last")
	if exists(liveFile(s, sid)) {
		t.Fatal("the live file stayed")
	}
	data := readJSON(t, previousFile(s, sid))
	if data["kind"] != "previous" || data["ended_at"] != float64(c.now.Unix()) || mode(t, previousFile(s, sid)) != 0o600 {
		t.Fatalf("previous = %v", data)
	}
	listed := s.PreviousSessions()
	if len(listed) != 1 || listed[0].Screen != nil || listed[0].Summary().ID != sid {
		t.Fatalf("listing = %+v", listed)
	}
	rec, ok := s.PreviousSession(sid)
	if !ok || *rec.Screen != "last" || !reflect.DeepEqual(rec.Scrollback, []string{"above"}) {
		t.Fatalf("read = %+v %v", rec, ok)
	}
}

func TestAnEndedSessionIsDeletedWhenPreviousSessionsAreOff(t *testing.T) {
	s, c, _ := newStore(t, true, 0, 10)
	endedAfter(t, s, c, sid, "alice", "last")
	if exists(liveFile(s, sid)) || exists(previousFile(s, sid)) || len(s.PreviousSessions()) != 0 {
		t.Fatal("something was kept")
	}
}

func TestASessionThatNeverShowedAScreenLeavesNoPreviousSession(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	s.Track(definition(sid, "alice"), "i")
	save(s, definition(sid, "alice"))
	s.End(sid)
	if exists(liveFile(s, sid)) || exists(previousFile(s, sid)) {
		t.Fatal("something was kept")
	}
}

func TestAPreviousSessionThatCannotBeWrittenIsNotListed(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	s.Track(definition(sid, "alice"), "i")
	s.SaveScreen(sid, "x", 80, 24, nil)
	renameFile = func(string, string) error { return errors.New("disk full") }
	defer func() { renameFile = os.Rename }()
	c.now = c.now.Add(time.Second)
	s.End(sid)
	if len(s.PreviousSessions()) != 0 {
		t.Fatal("listed")
	}
}

func TestPreviousSessionsListNewestFirst(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	for pid := 1; pid <= 3; pid++ {
		endedAfter(t, s, c, "pam-u-capture-"+strconv.Itoa(pid)+"-9", "alice", "x")
	}
	var ids []string
	for _, rec := range s.PreviousSessions() {
		ids = append(ids, rec.SessionID())
	}
	if !reflect.DeepEqual(ids, []string{"pam-u-capture-3-9", "pam-u-capture-2-9", "pam-u-capture-1-9"}) {
		t.Fatalf("order = %v", ids)
	}
}

func TestAnOwnerKeepsOnlyTheNewestPreviousSessions(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	for pid := 0; pid < PreviousPerOwner+2; pid++ {
		endedAfter(t, s, c, "pam-u-capture-"+strconv.Itoa(pid)+"-9", "alice", "x")
	}
	endedAfter(t, s, c, "pam-b-capture-1-9", "bob", "x")
	nobody := definition("pam-n-capture-1-9", "")
	nobody.Owner = nil
	s.Track(nobody, "i")
	s.SaveScreen(nobody.SessionID, "x", 80, 24, nil)
	s.End(nobody.SessionID)
	alice := 0
	for _, rec := range s.PreviousSessions() {
		if rec.Definition.Owner != nil && *rec.Definition.Owner == "alice" {
			alice++
		}
	}
	if alice != PreviousPerOwner {
		t.Fatalf("alice keeps %d", alice)
	}
	for id, kept := range map[string]bool{
		"pam-u-capture-0-9": false, "pam-u-capture-1-9": false, "pam-u-capture-2-9": true,
		"pam-b-capture-1-9": true, "pam-n-capture-1-9": true,
	} {
		if exists(previousFile(s, id)) != kept {
			t.Errorf("%s kept = %v, want %v", id, !kept, kept)
		}
	}
}

func TestPreviousSessionsPastTheirDaysArePruned(t *testing.T) {
	s, c, _ := newStore(t, true, 2, 10)
	endedAfter(t, s, c, "pam-u-capture-1-9", "alice", "x")
	c.now = c.now.Add(day)
	endedAfter(t, s, c, "pam-u-capture-2-9", "alice", "x")
	c.now = c.now.Add(day - time.Second)
	s.Prune()
	// The first ended two days ago, which is still within its days.
	if len(s.PreviousSessions()) != 2 {
		t.Fatal("pruned within its days")
	}
	c.now = c.now.Add(time.Second)
	s.Prune()
	if got := s.PreviousSessions(); len(got) != 1 || got[0].SessionID() != "pam-u-capture-2-9" {
		t.Fatalf("after its days: %v", got)
	}
	if exists(previousFile(s, "pam-u-capture-1-9")) {
		t.Fatal("its file stayed")
	}
}

func TestPreviousSessionsAreReadBackAndPrunedAtStartup(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	endedAfter(t, s, c, "pam-u-capture-1-9", "alice", "one")
	c.now = c.now.Add(6 * day)
	endedAfter(t, s, c, "pam-u-capture-2-9", "alice", "two")
	c.now = c.now.Add(6 * day)
	reader := New(s.dir, true, 7, 10, s.logger)
	reader.now = c.Now
	reader.LoadPrevious()
	if got := reader.PreviousSessions(); len(got) != 1 || got[0].SessionID() != "pam-u-capture-2-9" {
		t.Fatalf("read back %v", got)
	}
	if rec, ok := reader.PreviousSession("pam-u-capture-2-9"); !ok || *rec.Screen != "two" {
		t.Fatal("not readable")
	}
}

func TestAPreviousSessionWhoseFileHasGoneReadsAsNone(t *testing.T) {
	s, c, logs := newStore(t, true, 7, 10)
	if _, ok := s.PreviousSession(sid); ok {
		t.Fatal("an unknown id read")
	}
	endedAfter(t, s, c, sid, "alice", "x")
	_ = os.Remove(previousFile(s, sid))
	if _, ok := s.PreviousSession(sid); ok || !strings.Contains(logs.String(), "capture_state_file_ignored") {
		t.Fatalf("read a gone file: %s", logs.String())
	}
	if _, ok := s.PreviousSummary(sid); !ok {
		t.Fatal("the listing lost it")
	}
}

func writeLive(t *testing.T, s *Store, name string, content []byte) string {
	t.Helper()
	path := filepath.Join(privateDir(t, s.liveDir), name)
	if err := os.WriteFile(path, content, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func validLive(t *testing.T) []byte {
	t.Helper()
	return loadGolden(t).Records["live"]
}

func TestLiveRecordsAreReadBack(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	if got := s.LoadLive(); got != nil {
		t.Fatalf("no directory read as %v", got)
	}
	writeLive(t, s, sid+".json", validLive(t))
	got := s.LoadLive()
	if len(got) != 1 || got[0].SessionID() != sid {
		t.Fatalf("read %v", got)
	}
}

func TestAFileThatIsNotARecordIsSkippedWithAWarning(t *testing.T) {
	g := loadGolden(t)
	var withSession map[string]any
	_ = json.Unmarshal(g.Records["live"], &withSession)
	withSession["format"] = 0
	wrongFormat, _ := json.Marshal(withSession)
	for name, tc := range map[string]struct {
		file    string
		content []byte
		want    string
	}{
		"malformed":            {sid + ".json", []byte("{not json"), "not an object"},
		"another format":       {sid + ".json", wrongFormat, "unknown format"},
		"another id":           {"pam-suokki-capture-1-2.json", g.Records["live"], "session id is not the file's name"},
		"oversized":            {sid + ".json", bytes.Repeat([]byte(" "), MaxRecordBytes+1), "too large"},
		"not valid utf-8 json": {sid + ".json", []byte{0xc3, 0x28}, "not an object"},
	} {
		s, _, logs := newStore(t, true, 7, 10)
		writeLive(t, s, tc.file, tc.content)
		if got := s.LoadLive(); len(got) != 0 {
			t.Errorf("%s: read %v", name, got)
		}
		if !strings.Contains(logs.String(), "capture_state_file_ignored") || !strings.Contains(logs.String(), tc.want) {
			t.Errorf("%s: logs %s", name, logs.String())
		}
	}
}

func TestAnotherUsersFileIsSkipped(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	writeLive(t, s, sid+".json", validLive(t))
	statFile = func(f *os.File) (os.FileInfo, error) {
		info, err := f.Stat()
		return otherUsers{info}, err
	}
	defer func() { statFile = (*os.File).Stat }()
	if got := s.LoadLive(); len(got) != 0 || !strings.Contains(logs.String(), "owned by another user") {
		t.Fatalf("read %v: %s", got, logs.String())
	}
}

func TestASymlinkIsNotFollowedAndADirectoryIsNotRead(t *testing.T) {
	s, _, logs := newStore(t, true, 7, 10)
	target := filepath.Join(t.TempDir(), "elsewhere.json")
	_ = os.WriteFile(target, validLive(t), 0o600)
	writeLive(t, s, "x", nil)
	if err := os.Symlink(target, filepath.Join(s.liveDir, sid+".json")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(s.liveDir, "pam-u-capture-1-2.json"), 0o700); err != nil {
		t.Fatal(err)
	}
	if got := s.LoadLive(); len(got) != 0 {
		t.Fatalf("read %v", got)
	}
	if !strings.Contains(logs.String(), "cannot open: ELOOP") ||
		!strings.Contains(logs.String(), "not a regular file") {
		t.Fatalf("logs %s", logs.String())
	}
}

func TestTemporaryFilesACrashLeftAreRemovedAndOtherFilesIgnored(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	tmp := writeLive(t, s, tmpPrefix+"abc", []byte("half"))
	other := writeLive(t, s, "notes.txt", []byte("x"))
	if got := s.LoadLive(); len(got) != 0 || exists(tmp) || !exists(other) {
		t.Fatalf("read %v, tmp %v, other %v", got, exists(tmp), exists(other))
	}
}

func TestAnAdoptedRecordIsPaintedBackOnce(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	screen := "back"
	s.Adopt(&Record{Definition: definition(sid, "alice"), RecordingInstance: "kept", Screen: &screen, Cols: 90, Rows: 30, Scrollback: []string{"s"}})
	if got := s.Track(definition(sid, "alice"), "new"); got != "kept" {
		t.Fatalf("instance = %q", got)
	}
	// Pending, as often as asked, until a start that succeeded says it is done.
	for range 2 {
		got, cols, rows, ok := s.PendingRestore(sid)
		if !ok || got != "back" || cols != 90 || rows != 30 {
			t.Fatalf("restore = %q %d %d %v", got, cols, rows, ok)
		}
	}
	if !reflect.DeepEqual(s.Scrollback(sid), []string{"s"}) {
		t.Fatalf("scrollback = %q", s.Scrollback(sid))
	}
	s.RestoreDone(sid)
	if _, _, _, again := s.PendingRestore(sid); again {
		t.Fatal("painted twice")
	}
	s.Adopt(&Record{Definition: definition("pam-u-capture-1-2", "alice"), RecordingInstance: "i"})
	if _, _, _, ok := s.PendingRestore("pam-u-capture-1-2"); ok {
		t.Fatal("a record with no screen was painted")
	}
	if _, _, _, ok := s.PendingRestore("pam-u-capture-9-9"); ok || s.Scrollback("pam-u-capture-9-9") != nil {
		t.Fatal("an unknown session was painted")
	}
}

func TestADeadLoginEndsWhenItsScreenWasLastSaved(t *testing.T) {
	s, c, _ := newStore(t, true, 2, 10)
	screen := "x"
	cutoff := float64(c.now.Unix()) - 2*secondsPerDay
	old := &Record{Definition: definition(sid, "alice"), RecordingInstance: "i", Screen: &screen, Cols: 80, Rows: 24}
	// One that ended before its days began is not written only to be pruned.
	old.SavedAt = cutoff - 1
	s.EndRecord(old)
	if _, ok := s.PreviousSummary(sid); ok || exists(previousFile(s, sid)) {
		t.Fatal("kept a session older than its days")
	}
	old.SavedAt = cutoff
	s.EndRecord(old)
	if got, _ := s.PreviousSummary(sid); got == nil || *got.EndedAt != cutoff {
		t.Fatalf("ended %v", got)
	}
}

func TestNoStoreIsBuiltWhenBothFeaturesAreOff(t *testing.T) {
	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
	if s, err := Build(serverconfig.DefaultServerConfig(), quiet); s != nil || err != nil {
		t.Fatalf("built with both off: %v %v", s, err)
	}
}

func storeIn(dir string, persist bool, days int) (*Store, error) {
	cfg := serverconfig.DefaultServerConfig()
	cfg.SessionPersistence = serverconfig.SessionPersistenceConfig{
		PersistCapture: persist, PreviousKeepDays: days, PreviousScrollbackLines: 5, StateDir: &dir,
	}
	return Build(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
}

// A config built in code, which skipped the validators, is refused as the
// validators refuse it rather than dereferenced.
func TestAStoreWithNoStateDirectoryIsAnError(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	cfg.SessionPersistence.PersistCapture = true
	s, err := Build(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if s != nil || err == nil || err.Error() != serverconfig.StateDirRequired {
		t.Fatalf("%v %v", s, err)
	}
}

func TestTheStoreIsMadeInTheDirectoryNamed(t *testing.T) {
	for _, tc := range []struct {
		persist bool
		days    int
	}{{true, 0}, {false, 7}, {true, 7}} {
		dir := filepath.Join(t.TempDir(), "s")
		s, err := storeIn(dir, tc.persist, tc.days)
		if err != nil {
			t.Fatal(err)
		}
		if mode(t, dir) != 0o700 || s.liveDir != filepath.Join(dir, "live") || s.Persist() != tc.persist ||
			s.KeepsPrevious() != (tc.days > 0) || s.ScrollbackLines() != map[bool]int{true: 5, false: 0}[tc.days > 0] {
			t.Errorf("%+v: %+v", tc, s)
		}
	}
	existing := privateDir(t, filepath.Join(t.TempDir(), "s"))
	if _, err := storeIn(existing, true, 0); err != nil {
		t.Fatal(err)
	}
}

// A state directory that is not private stops the server, in the reference's
// words.
func TestAStateDirectoryThatIsNotPrivateStopsTheServer(t *testing.T) {
	for name, tc := range map[string]struct {
		prepare func(dir string)
		want    string
	}{
		"wider": {func(dir string) { _ = os.Mkdir(dir, 0o700); _ = os.Chmod(dir, 0o755) }, ": mode 0755, not 0700"},
		"file":  {func(dir string) { _ = os.WriteFile(dir, []byte("x"), 0o600) }, ": not a directory this user owns"},
		"link":  {func(dir string) { _ = os.Symlink(filepath.Dir(dir), dir) }, ": not a directory this user owns"},
	} {
		dir := filepath.Join(t.TempDir(), "s")
		tc.prepare(dir)
		if _, err := storeIn(dir, true, 0); err == nil || err.Error() != "session_persistence.state_dir "+dir+tc.want {
			t.Errorf("%s: err = %v", name, err)
		}
	}
	dir := filepath.Join(t.TempDir(), "a", "s")
	if _, err := storeIn(dir, true, 0); err == nil ||
		err.Error() != "session_persistence.state_dir "+dir+": cannot create: ENOENT" {
		t.Errorf("missing parent: err = %v", err)
	}
	owned := privateDir(t, filepath.Join(t.TempDir(), "s"))
	geteuid = func() int { return os.Geteuid() + 1 }
	defer func() { geteuid = os.Geteuid }()
	if _, err := storeIn(owned, true, 0); err == nil || !strings.Contains(err.Error(), "not a directory this user owns") {
		t.Errorf("another user's: err = %v", err)
	}
}

func TestADirectoryThatWillNotDoIsSkippedWithAWarning(t *testing.T) {
	for name, tc := range map[string]struct {
		prepare func(live string)
		want    string
	}{
		"wider":    {func(live string) { _ = os.Chmod(live, 0o755) }, "mode 0755, not 0700"},
		"file":     {func(live string) { _ = os.RemoveAll(live); _ = os.WriteFile(live, nil, 0o600) }, "not a directory this user owns"},
		"unlisted": {func(live string) { _ = os.Chmod(live, 0o300) }, "mode 0300, not 0700"},
	} {
		s, _, logs := newStore(t, true, 7, 10)
		writeLive(t, s, sid+".json", validLive(t))
		tc.prepare(s.liveDir)
		if got := s.LoadLive(); len(got) != 0 {
			t.Errorf("%s: read %v", name, got)
		}
		if !strings.Contains(logs.String(), "capture_state_dir_ignored") || !strings.Contains(logs.String(), tc.want) {
			t.Errorf("%s: logs %s", name, logs.String())
		}
		_ = os.Chmod(s.liveDir, 0o700)
	}
}

func TestSessionsWithNoOwnerCountAgainstThePamUserTheirIDNames(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	end := func(id string) {
		def := definition(id, "")
		def.Owner = nil
		s.Track(def, "i")
		s.SaveScreen(id, "x", 80, 24, nil)
		c.now = c.now.Add(time.Second)
		s.End(id)
	}
	for pid := 0; pid <= PreviousPerOwner; pid++ {
		end("pam-ann-capture-" + strconv.Itoa(pid) + "-9")
	}
	end("pam-ben-capture-1-9")
	kept := map[string]bool{}
	for _, rec := range s.PreviousSessions() {
		kept[rec.SessionID()] = true
	}
	if len(kept) != PreviousPerOwner+1 || !kept["pam-ben-capture-1-9"] || kept["pam-ann-capture-0-9"] {
		t.Fatalf("kept %v", kept)
	}
}

func TestPreviousSessionsThatEndedAtOnceAreOrderedByID(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	for _, id := range []string{"pam-u-capture-2-9", "pam-u-capture-1-9", "pam-u-capture-3-9"} {
		s.Track(definition(id, "alice"), "i")
		s.SaveScreen(id, "x", 80, 24, nil)
		s.End(id)
	}
	var ids []string
	for _, rec := range s.PreviousSessions() {
		ids = append(ids, rec.SessionID())
	}
	if !reflect.DeepEqual(ids, []string{"pam-u-capture-1-9", "pam-u-capture-2-9", "pam-u-capture-3-9"}) {
		t.Fatalf("order = %v", ids)
	}
}

// Writes take place outside the store's lock: a listing does not wait on a
// write in progress.
func TestAListingDoesNotWaitOnAWrite(t *testing.T) {
	s, _, _ := newStore(t, true, 7, 10)
	s.Track(definition(sid, "alice"), "i")
	entered, release := make(chan struct{}), make(chan struct{})
	syncFile = func(f *os.File) error {
		select {
		case entered <- struct{}{}:
			<-release
		default:
		}
		return f.Sync()
	}
	defer func() { syncFile = (*os.File).Sync }()
	saved := make(chan struct{})
	go func() { save(s, definition(sid, "alice")); close(saved) }()
	// The write still calls syncFile, for its directory, after it is released:
	// it must have returned before the deferred restore above swaps it back.
	defer func() { close(release); <-saved }()
	<-entered
	listed := make(chan struct{})
	go func() { _ = s.PreviousSessions(); s.SetDefinition(definition(sid, "bob")); close(listed) }()
	select {
	case <-listed:
	case <-time.After(5 * time.Second):
		t.Fatal("the store's lock was held across a write")
	}
}

func TestThePruneLoopPrunesUntilItsContextEnds(t *testing.T) {
	s, c, _ := newStore(t, true, 1, 10)
	endedAfter(t, s, c, sid, "alice", "x")
	c.now = c.now.Add(3 * day)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); s.PruneLoop(ctx, time.Millisecond) }()
	deadline := time.Now().Add(3 * time.Second)
	for len(s.PreviousSessions()) != 0 {
		if time.Now().After(deadline) {
			t.Fatal("never pruned")
		}
		time.Sleep(time.Millisecond)
	}
	cancel()
	<-done
}

// otherUsers is a file's info as if another user owned it.
type otherUsers struct{ os.FileInfo }

func (o otherUsers) Sys() any {
	st := *o.FileInfo.Sys().(*syscall.Stat_t)
	st.Uid++
	return &st
}
