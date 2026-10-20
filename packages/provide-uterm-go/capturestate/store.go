//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package capturestate

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// PreviousPerOwner is the most previous sessions kept for one owner. Days of
// logins each leave one: past this many the oldest go first, so a user who
// logs in and out all day cannot fill the disk within the retention period,
// and twenty still covers several sessions a day for a week. A session with
// no owner (PAM's, until somebody sets one) counts against the user its PAM
// id names, so one user's logins never evict another's.
const PreviousPerOwner = 20

// PruneInterval is how often previous sessions past their retention are
// removed, besides at startup.
const PruneInterval = time.Hour

const secondsPerDay = 86400

// tmpPrefix names a write in progress; one left by a crash is removed on load.
const tmpPrefix = ".tmp-"

// missing is what checkPrivateDir says of a directory that does not exist.
const missing = "cannot stat: ENOENT"

// Store holds the live and previous capture-session files under one
// directory, and the records of the sessions it keeps. Safe for concurrent
// use. No file is read or written under mu: a write takes its session's own
// lock (writers), so one session's fsync never holds up another's save, or a
// listing.
type Store struct {
	dir, liveDir, previousDir string
	persist                   bool
	keepDays                  int
	scrollbackLines           int
	logger                    *slog.Logger
	// now is the wall clock records are dated by; swappable in tests.
	now func() time.Time

	mu        sync.Mutex
	records   map[string]*Record
	previous  map[string]*Record // each without its screen, so a listing reads no file
	restoring map[string]bool    // adopted records whose screen is yet to be painted back
	writers   map[string]*sync.Mutex
}

// New builds a store under dir, which must already be a private directory
// (Build checks it). persist writes live records; keepDays > 0 keeps
// previous ones, with up to scrollbackLines rows of scrollback.
func New(dir string, persist bool, keepDays, scrollbackLines int, logger *slog.Logger) *Store {
	if keepDays == 0 {
		// Only a previous session shows its scrollback.
		scrollbackLines = 0
	}
	return &Store{
		dir: dir, liveDir: filepath.Join(dir, "live"), previousDir: filepath.Join(dir, "previous"),
		persist: persist, keepDays: keepDays, scrollbackLines: scrollbackLines, logger: logger,
		now:     time.Now,
		records: map[string]*Record{}, previous: map[string]*Record{}, restoring: map[string]bool{},
		writers: map[string]*sync.Mutex{},
	}
}

// Build returns the store cfg asks for in session_persistence.state_dir, or
// nil when both features are off. The directory is made 0700 if it is
// missing, and refused, which stops the server starting, if it is not a 0700
// directory this user owns: the files in it hold unredacted screens.
func Build(cfg *serverconfig.UtermServerConfig, logger *slog.Logger) (*Store, error) {
	p := cfg.SessionPersistence
	if !p.PersistCapture && p.PreviousKeepDays == 0 {
		return nil, nil
	}
	if p.StateDir == nil {
		// A config built in code rather than loaded, which skipped the check.
		return nil, errors.New(serverconfig.StateDirRequired)
	}
	dir := *p.StateDir
	if err := makePrivateDir(dir); err != nil {
		var refused *RecordError
		if errors.As(err, &refused) {
			return nil, fmt.Errorf("session_persistence.state_dir %s", refused.Msg)
		}
		return nil, fmt.Errorf("session_persistence.state_dir %s: cannot create: %s", dir, errnoName(err))
	}
	return New(dir, p.PersistCapture, p.PreviousKeepDays, p.PreviousScrollbackLines, logger), nil
}

// Persist reports whether live records are written.
func (s *Store) Persist() bool { return s.persist }

// KeepsPrevious reports whether an ended session is kept.
func (s *Store) KeepsPrevious() bool { return s.keepDays > 0 }

// ScrollbackLines is how many rows of scrollback a session keeps.
func (s *Store) ScrollbackLines() int { return s.scrollbackLines }

// Tracks reports whether def is a session this store keeps: a PAM capture.
func (s *Store) Tracks(def serverconfig.SessionDefinition) bool {
	_, _, ok := LoginIdentity(def.SessionID)
	return ok && def.ConnectorType == "pty_capture"
}

// Track starts keeping def in memory, under recordingInstance unless it is
// kept already, and returns its record's recording instance. A kept record is
// never replaced: it goes only when its session ends (End).
func (s *Store) Track(def serverconfig.SessionDefinition, recordingInstance string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec, ok := s.records[def.SessionID]
	if !ok {
		rec = &Record{Definition: def, RecordingInstance: recordingInstance}
		s.records[def.SessionID] = rec
	}
	return rec.RecordingInstance
}

// Adopt keeps a record restored from its file, its screen to be painted back
// (PendingRestore).
func (s *Store) Adopt(rec *Record) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records[rec.SessionID()] = rec
	s.restoring[rec.SessionID()] = true
}

// PendingRestore returns the screen an adopted session's next connector
// starts from, until RestoreDone; ok is false for any other session, or one
// without a screen.
func (s *Store) PendingRestore(id string) (screen string, cols, rows int, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec := s.records[id]
	if !s.restoring[id] || rec.Screen == nil {
		return "", 0, 0, false
	}
	return *rec.Screen, rec.Cols, rec.Rows, true
}

// RestoreDone is the restored screen painted back by a connector that
// started: no later start paints it again. Until then a start that fails
// leaves it for the next.
func (s *Store) RestoreDone(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.restoring, id)
}

// Scrollback returns the rows a kept session has scrolled off so far, for a
// connector started again to carry on from.
func (s *Store) Scrollback(id string) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if rec, ok := s.records[id]; ok {
		return append([]string(nil), rec.Scrollback...)
	}
	return nil
}

// SetDefinition records def, the session's definition as it now stands, in
// memory only, so a caller holding its own lock never waits on a disk; Flush
// writes it.
func (s *Store) SetDefinition(def serverconfig.SessionDefinition) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if rec, ok := s.records[def.SessionID]; ok {
		rec.Definition = def
	}
}

// Flush writes the session's record as it now stands, when persisting.
// Flushes of one session are taken in turn, each writing the latest state,
// so the file ends as the last change left it.
func (s *Store) Flush(id string) {
	if !s.persist {
		return
	}
	w := s.writer(id)
	w.Lock()
	defer w.Unlock()
	s.mu.Lock()
	rec, ok := s.records[id]
	var snapshot Record
	if ok {
		snapshot = *rec
	}
	s.mu.Unlock()
	if ok {
		s.write(s.liveDir, &snapshot)
	}
}

// SaveScreen keeps the session's screen (an ANSIScreen at cols x rows) and the
// newest of its scrollback, and writes them when persisting.
func (s *Store) SaveScreen(id, screen string, cols, rows int, scrollback []string) {
	s.mu.Lock()
	rec, ok := s.records[id]
	if ok {
		rec.Screen, rec.Cols, rec.Rows = &screen, cols, rows
		rec.Scrollback = nil
		if keep := s.scrollbackLines; keep > 0 {
			rec.Scrollback = append([]string(nil), scrollback[max(0, len(scrollback)-keep):]...)
		}
		rec.SavedAt = s.wallSeconds()
	}
	s.mu.Unlock()
	if ok {
		s.Flush(id)
	}
}

// writer is the lock id's writes take in turn.
func (s *Store) writer(id string) *sync.Mutex {
	s.mu.Lock()
	defer s.mu.Unlock()
	w, ok := s.writers[id]
	if !ok {
		w = &sync.Mutex{}
		s.writers[id] = w
	}
	return w
}

// End is the session gone: kept as a previous session, or forgotten.
func (s *Store) End(id string) {
	w := s.writer(id)
	w.Lock()
	defer w.Unlock()
	s.mu.Lock()
	rec, ok := s.records[id]
	delete(s.records, id)
	delete(s.restoring, id)
	s.mu.Unlock()
	if ok {
		s.end(rec, s.wallSeconds())
	}
	s.mu.Lock()
	delete(s.writers, id)
	s.mu.Unlock()
}

// EndRecord ends a login found gone at startup: at the latest, when its screen
// was last saved.
func (s *Store) EndRecord(rec *Record) {
	s.end(rec, rec.SavedAt)
}

func (s *Store) end(rec *Record, endedAt float64) {
	// One that ended longer ago than it would be kept (a login gone during a
	// long downtime) is not written only to be pruned.
	if s.KeepsPrevious() && rec.Screen != nil && endedAt >= s.cutoff() {
		ended := *rec
		ended.EndedAt = &endedAt
		if s.write(s.previousDir, &ended) {
			s.mu.Lock()
			s.previous[ended.SessionID()] = withoutScreen(&ended)
			dropped := s.pruneOwnerLocked(ownerKey(&ended))
			s.mu.Unlock()
			s.removePrevious(dropped)
		}
	}
	_ = os.Remove(filepath.Join(s.liveDir, rec.SessionID()+".json"))
}

func (s *Store) wallSeconds() float64 { return float64(s.now().UnixNano()) / 1e9 }

// cutoff is the end time a previous session must not be older than.
func (s *Store) cutoff() float64 { return s.wallSeconds() - float64(s.keepDays*secondsPerDay) }

// write writes rec into dir, reporting whether it was. A file that cannot be
// written costs a restart its copy of the session, never the session itself,
// so the failure is logged rather than returned.
func (s *Store) write(dir string, rec *Record) bool {
	content, err := rec.encode()
	if err == nil && len(content) > MaxRecordBytes && len(rec.Scrollback) > 0 {
		// A screen alone fits; it is the scrollback that can outgrow the
		// limit, and the screen is what matters most.
		trimmed := *rec
		trimmed.Scrollback = nil
		content, err = trimmed.encode()
	}
	if err == nil && len(content) > MaxRecordBytes {
		err = fmt.Errorf("%d bytes is too large", len(content))
	}
	if err == nil {
		if err = makePrivateDir(s.dir); err == nil {
			if err = makePrivateDir(dir); err == nil {
				err = writeFile(filepath.Join(dir, rec.SessionID()+".json"), content)
			}
		}
	}
	if err != nil {
		s.logger.Warn("capture_state_write_failed", "session_id", rec.SessionID(), "error", err)
		return false
	}
	return true
}

// checkPrivateDir says why dir is not a 0700 directory this user owns, or ""
// when it is. Not followed through a symlink; a missing one is missing.
func checkPrivateDir(dir string) string {
	info, err := os.Lstat(dir)
	if err != nil {
		return "cannot stat: " + errnoName(err)
	}
	if !info.IsDir() || !ownedByMe(info) {
		return "not a directory this user owns"
	}
	if mode := info.Mode().Perm(); mode != 0o700 {
		return fmt.Sprintf("mode %04o, not 0700", mode)
	}
	return ""
}

// makePrivateDir creates dir 0700, or refuses one that is not a 0700
// directory this user owns. Its parent must exist: MkdirAll would create
// missing ones with the umask's mode instead.
func makePrivateDir(dir string) error {
	if err := os.Mkdir(dir, 0o700); err == nil {
		// Mkdir's mode is narrowed by the umask, never widened; this makes it
		// exact. Should it fail, the check below refuses the mode left.
		_ = os.Chmod(dir, 0o700)
	} else if !os.IsExist(err) {
		return err
	}
	if problem := checkPrivateDir(dir); problem != "" {
		return refuse(dir + ": " + problem)
	}
	return nil
}

// errnoName names an OS error by its errno (ELOOP, EACCES), the same words in
// every port; an error that carries none is its own text.
func errnoName(err error) string {
	var errno syscall.Errno
	if errors.As(err, &errno) {
		return unix.ErrnoName(errno)
	}
	return err.Error()
}

// ownedByMe reports whether info is owned by this process's user.
func ownedByMe(info os.FileInfo) bool {
	st, ok := info.Sys().(*syscall.Stat_t)
	return ok && int(st.Uid) == geteuid()
}

// geteuid is os.Geteuid, swappable in tests: a file another user owns cannot
// be made without being root.
var geteuid = os.Geteuid

// statFile is (*os.File).Stat, swappable in tests for the same reason.
var statFile = (*os.File).Stat

// readDir is os.ReadDir, swappable in tests: a 0700 directory this user owns
// lists, short of a failing disk.
var readDir = os.ReadDir

// readerOf is the file itself, swappable in tests: a read error (EIO) cannot
// be had on demand from a healthy disk.
var readerOf = func(f *os.File) io.Reader { return f }

// renameFile and syncFile are os.Rename and (*os.File).Sync, swappable in
// tests: neither fails on demand on a healthy disk.
var (
	renameFile = os.Rename
	syncFile   = (*os.File).Sync
)

// writeFile replaces path with content: a synced 0600 temporary file renamed
// over it, then the directory synced so the rename is durable.
func writeFile(path string, content []byte) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), tmpPrefix)
	if err != nil {
		return err
	}
	_, err = tmp.Write(content)
	if err == nil {
		err = tmp.Chmod(0o600)
	}
	if err == nil {
		err = syncFile(tmp)
	}
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = renameFile(tmp.Name(), path)
	}
	if err != nil {
		_ = os.Remove(tmp.Name())
		return err
	}
	if dir, err := os.Open(filepath.Dir(path)); err == nil {
		_ = syncFile(dir)
		_ = dir.Close()
	}
	return nil
}

// readFile returns the bytes of a record file this process owns, refusing
// anything else: opened without following a symlink, and without blocking (a
// FIFO planted there would otherwise hold startup until something wrote to
// it), then checked on the open descriptor for a regular file this user
// owns, no larger than MaxRecordBytes.
func readFile(path string) ([]byte, error) {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, refuse("cannot open: " + errnoName(err))
	}
	defer func() { _ = f.Close() }()
	info, err := statFile(f)
	switch {
	case err != nil:
		return nil, refuse("cannot stat: " + errnoName(err))
	case !info.Mode().IsRegular():
		return nil, refuse("not a regular file")
	case !ownedByMe(info):
		return nil, refuse("owned by another user")
	case info.Size() > MaxRecordBytes:
		return nil, refuse("too large")
	}
	// One byte past the limit, so a file that grew after the check is refused too.
	content, err := io.ReadAll(io.LimitReader(readerOf(f), MaxRecordBytes+1))
	if err != nil {
		// A failing disk (EIO) costs this file, never startup or a read.
		return nil, refuse("cannot read: " + errnoName(err))
	}
	if len(content) > MaxRecordBytes {
		return nil, refuse("too large")
	}
	return content, nil
}

// load reads every record of kind in dir. A directory or file that will not
// do is skipped with a warning, and a write a crash left half done is
// removed: a server whose state cannot be read starts without it.
func (s *Store) load(dir, kind string) []*Record {
	problem := checkPrivateDir(dir)
	if problem == missing {
		return nil
	}
	var entries []os.DirEntry
	if problem == "" {
		var err error
		if entries, err = readDir(dir); err != nil {
			problem = "cannot list: " + errnoName(err)
		}
	}
	if problem != "" {
		s.logger.Warn("capture_state_dir_ignored", "path", dir, "error", problem)
		return nil
	}
	var out []*Record
	for _, entry := range entries {
		name := entry.Name()
		path := filepath.Join(dir, name)
		if strings.HasPrefix(name, tmpPrefix) {
			_ = os.Remove(path)
			continue
		}
		id, isJSON := strings.CutSuffix(name, ".json")
		if !isJSON {
			continue
		}
		rec, err := parseFile(path, id, kind)
		if err != nil {
			s.logger.Warn("capture_state_file_ignored", "path", path, "error", err)
			continue
		}
		if len(rec.IgnoredKeys) > 0 {
			// A newer build of the same format wrote it: expected on a
			// downgrade, so not a warning. Once a file, at load.
			s.logger.Info("capture_state_keys_ignored", "path", path, "keys", strings.Join(rec.IgnoredKeys, ","))
		}
		out = append(out, rec)
	}
	return out
}

func parseFile(path, id, kind string) (*Record, error) {
	data, err := readFile(path)
	if err != nil {
		return nil, err
	}
	return ParseBytes(data, id, kind)
}

// LoadLive returns every live record on disk.
func (s *Store) LoadLive() []*Record { return s.load(s.liveDir, "live") }

// LoadPrevious reads the previous records into the listing, then prunes them.
func (s *Store) LoadPrevious() {
	records := s.load(s.previousDir, "previous")
	s.mu.Lock()
	for _, rec := range records {
		s.previous[rec.SessionID()] = withoutScreen(rec)
	}
	s.mu.Unlock()
	s.Prune()
}

// Prune removes previous records past previous_keep_days, then past the
// per-owner cap.
func (s *Store) Prune() {
	cutoff := s.cutoff()
	s.mu.Lock()
	var dropped []string
	owners := map[string]bool{}
	for id, rec := range s.previous {
		if *rec.EndedAt < cutoff {
			delete(s.previous, id)
			dropped = append(dropped, id)
			continue
		}
		owners[ownerKey(rec)] = true
	}
	for owner := range owners {
		dropped = append(dropped, s.pruneOwnerLocked(owner)...)
	}
	s.mu.Unlock()
	s.removePrevious(dropped)
}

// ownerKey is whose previous sessions rec counts among: its owner, else the
// PAM user its id names.
func ownerKey(rec *Record) string {
	if owner := rec.Definition.Owner; owner != nil {
		return "owner:" + *owner
	}
	user, _ := PamUser(rec.SessionID())
	return "pam_user:" + user
}

// pruneOwnerLocked drops owner's records past the cap from the listing and
// returns their ids, for their files to go once mu is released. Caller holds
// s.mu.
func (s *Store) pruneOwnerLocked(owner string) []string {
	var mine []*Record
	for _, rec := range s.previous {
		if ownerKey(rec) == owner {
			mine = append(mine, rec)
		}
	}
	sortNewestFirst(mine)
	var dropped []string
	for _, rec := range mine[min(len(mine), PreviousPerOwner):] {
		delete(s.previous, rec.SessionID())
		dropped = append(dropped, rec.SessionID())
	}
	return dropped
}

func (s *Store) removePrevious(ids []string) {
	for _, id := range ids {
		_ = os.Remove(filepath.Join(s.previousDir, id+".json"))
	}
}

// sortNewestFirst orders records by end time, newest first, ties by id, as
// the reference orders them.
func sortNewestFirst(records []*Record) {
	sort.SliceStable(records, func(i, j int) bool {
		if *records[i].EndedAt != *records[j].EndedAt {
			return *records[i].EndedAt > *records[j].EndedAt
		}
		return records[i].SessionID() < records[j].SessionID()
	})
}

// PreviousSessions returns every previous record, newest first, without its
// screen.
func (s *Store) PreviousSessions() []*Record {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*Record, 0, len(s.previous))
	for _, rec := range s.previous {
		out = append(out, rec)
	}
	sortNewestFirst(out)
	return out
}

// PreviousSummary returns the listing's copy of id's previous record (no
// screen).
func (s *Store) PreviousSummary(id string) (*Record, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec, ok := s.previous[id]
	return rec, ok
}

// PreviousSession returns id's previous record, screen included, read from
// its file; ok is false when it is not kept or the file has turned
// unreadable.
func (s *Store) PreviousSession(id string) (*Record, bool) {
	if _, kept := s.PreviousSummary(id); !kept {
		return nil, false
	}
	path := filepath.Join(s.previousDir, id+".json")
	rec, err := parseFile(path, id, "previous")
	if err != nil {
		s.logger.Warn("capture_state_file_ignored", "path", path, "error", err)
		return nil, false
	}
	return rec, true
}

// PruneLoop prunes the previous records every every (PruneInterval in the
// server) until ctx ends.
func (s *Store) PruneLoop(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.Prune()
		}
	}
}

func withoutScreen(rec *Record) *Record {
	return &Record{
		Definition: rec.Definition, RecordingInstance: rec.RecordingInstance,
		Cols: rec.Cols, Rows: rec.Rows, SavedAt: rec.SavedAt, EndedAt: rec.EndedAt,
	}
}
