//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/capturestate"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	uptty "github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// A PAM capture session outlives a server restart while its login does: the
// registry writes a kept session's definition whenever it changes, its screen
// as it settles and once more as it stops, and a new server re-creates every
// session whose login is still the same process, under the same id, with the
// same owner, screen and recording. A login that ended while the server was
// down leaves a previous session instead.

// selfStartTicks is this test process's own start time, field 22 of
// /proc/self/stat: a login id built from it names a live login.
func selfStartTicks(t *testing.T) int64 {
	t.Helper()
	raw, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		t.Skip("no /proc on this host")
	}
	text := string(raw)
	fields := strings.Fields(text[strings.LastIndex(text, ")")+1:])
	ticks, err := strconv.ParseInt(fields[19], 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	return ticks
}

// liveID is a capture id naming this process: a login that is alive.
func liveID(t *testing.T) string {
	return "pam-suokki-capture-" + strconv.Itoa(os.Getpid()) + "-" + strconv.FormatInt(selfStartTicks(t), 10)
}

type keptRegistry struct {
	*SessionRegistryImpl
	store *capturestate.Store
}

// newKeptRegistry builds a registry keeping capture sessions under dir, with
// the real connector factory.
func newKeptRegistry(t *testing.T, dir string, persist bool, keepDays int) keptRegistry {
	t.Helper()
	r := NewSessionRegistry(serverconfig.DefaultServerConfig())
	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
	// The state directory is private, as Build makes it.
	if err := os.Chmod(dir, 0o700); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	store := capturestate.New(dir, persist, keepDays, 100, quiet)
	r.SetCaptureState(store)
	return keptRegistry{SessionRegistryImpl: r, store: store}
}

func capturePayload(id, socket string) map[string]any {
	return map[string]any{
		"session_id": id, "display_name": "suokki (pts/3) [live]", "connector_type": "pty_capture",
		"connector_config": map[string]any{"socket_path": socket}, "input_mode": "open", "visibility": "operator",
	}
}

func liveRecord(t *testing.T, dir, id string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(dir, "live", id+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func keptSession(t *testing.T, dir, id string) map[string]any {
	t.Helper()
	return liveRecord(t, dir, id)["session"].(map[string]any)
}

func TestACreatedCaptureSessionIsWrittenAndRewrittenAsItChanges(t *testing.T) {
	dir := t.TempDir()
	r := newKeptRegistry(t, dir, true, 7)
	ctx := context.Background()
	id := "pam-suokki-capture-1-2"
	if _, err := r.CreateSession(ctx, capturePayload(id, "/run/c/s.sock")); err != nil {
		t.Fatal(err)
	}
	if got := keptSession(t, dir, id); got["visibility"] != "operator" || got["input_mode"] != "open" {
		t.Fatalf("created %v", got)
	}
	if _, err := r.UpdateSession(ctx, id, map[string]any{"visibility": "private", "display_name": "renamed"}); err != nil {
		t.Fatal(err)
	}
	if _, err := r.SetMode(ctx, id, "hijack"); err != nil {
		t.Fatal(err)
	}
	got := keptSession(t, dir, id)
	if got["visibility"] != "private" || got["display_name"] != "renamed" || got["input_mode"] != "hijack" {
		t.Fatalf("after the changes %v", got)
	}
}

func TestOtherSessionsAndAnUnwiredRegistryWriteNothing(t *testing.T) {
	dir := t.TempDir()
	r := newKeptRegistry(t, filepath.Join(dir, "state"), true, 7)
	ctx := context.Background()
	if _, err := r.CreateSession(ctx, capturePayload("not-a-pam-id", "/run/c/s.sock")); err != nil {
		t.Fatal(err)
	}
	if _, err := r.CreateSession(ctx, map[string]any{"session_id": "pam-u-capture-1-2", "connector_type": "shell"}); err != nil {
		t.Fatal(err)
	}
	unwired := NewSessionRegistry(serverconfig.DefaultServerConfig())
	if _, err := unwired.CreateSession(ctx, capturePayload("pam-u-capture-3-4", "/run/c/s.sock")); err != nil {
		t.Fatal(err)
	}
	unwired.RestoreCaptureSessions(ctx, func(string) bool { return true })
	if _, err := os.Stat(filepath.Join(dir, "state")); !os.IsNotExist(err) {
		t.Fatalf("a file was written: %v", err)
	}
}

// startKept creates and starts a kept capture session on a fresh socket.
func startKept(t *testing.T, r keptRegistry, id string) string {
	t.Helper()
	socket := shortCaptureDir(t) + "/cap.sock"
	ctx := context.Background()
	if _, err := r.CreateSession(ctx, capturePayload(id, socket)); err != nil {
		t.Fatal(err)
	}
	if _, err := r.StartSession(ctx, id); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = r.StopSession(context.Background(), id) })
	return socket
}

func keeperOf(r keptRegistry, id string) *captureKeeper {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.entries[id].keeper
}

func savedScreen(t *testing.T, dir, id string) string {
	t.Helper()
	screen, _ := liveRecord(t, dir, id)["screen"].(map[string]any)
	ansi, _ := screen["ansi"].(string)
	return ansi
}

func TestAStoppingSessionSavesItsLastScreenAndADeletedOneIsKept(t *testing.T) {
	dir := t.TempDir()
	r := newKeptRegistry(t, dir, true, 7)
	id := "pam-suokki-capture-5-6"
	socket := startKept(t, r, id)
	writeCaptureFrame(t, socket, uptty.ChannelStdout, "$ exit")
	ctx := context.Background()
	waitFor(t, "the output", func() bool {
		snap, _ := r.LastSnapshot(ctx, id)
		s, _ := snap["screen"].(string)
		return strings.Contains(s, "$ exit")
	})
	if _, err := r.StopSession(ctx, id); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(savedScreen(t, dir, id), "$ exit") {
		t.Fatalf("saved %q", savedScreen(t, dir, id))
	}
	if err := r.DeleteSession(ctx, id); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "live", id+".json")); !os.IsNotExist(err) {
		t.Fatal("the live file stayed")
	}
	rec, ok := r.store.PreviousSession(id)
	if !ok || !strings.Contains(*rec.Screen, "$ exit") {
		t.Fatalf("previous %+v %v", rec, ok)
	}
}

func TestADeletedRunningSessionSavesItsScreenFirst(t *testing.T) {
	dir := t.TempDir()
	r := newKeptRegistry(t, dir, false, 7)
	id := "pam-suokki-capture-7-8"
	socket := startKept(t, r, id)
	writeCaptureFrame(t, socket, uptty.ChannelStdout, "still running")
	ctx := context.Background()
	waitFor(t, "the output", func() bool {
		snap, _ := r.LastSnapshot(ctx, id)
		s, _ := snap["screen"].(string)
		return strings.Contains(s, "still running")
	})
	if err := r.DeleteSession(ctx, id); err != nil {
		t.Fatal(err)
	}
	if rec, ok := r.store.PreviousSession(id); !ok || !strings.Contains(*rec.Screen, "still running") {
		t.Fatalf("previous %+v %v", rec, ok)
	}
}

// The keeper saves on the recording's settle trigger: once output pauses,
// at most once a second, only when the screen changed.
func TestTheKeeperSavesAScreenOnceOutputSettles(t *testing.T) {
	dir := t.TempDir()
	r := newKeptRegistry(t, dir, true, 7)
	id := "pam-suokki-capture-9-1"
	socket := startKept(t, r, id)
	k := keeperOf(r, id)
	now := time.Unix(1000, 0)
	k.now = func() time.Time { return now }
	term := map[string]any{"type": "term", "data": "x"}

	must(t, k.RecordSettledScreen(nil)) // no output yet: nothing owed
	if got := savedScreen(t, dir, id); got != "" {
		t.Fatalf("saved before output: %q", got)
	}
	writeCaptureFrame(t, socket, uptty.ChannelStdout, "painted")
	waitFor(t, "the output", func() bool { return strings.Contains(r.entries[id].conn.Session().ANSIScreen(), "painted") })
	must(t, k.FrameSent("", term))
	must(t, k.FrameSent("", map[string]any{"type": "snapshot"})) // not output
	now = now.Add(100 * time.Millisecond)
	must(t, k.RecordSettledScreen(nil))
	if got := savedScreen(t, dir, id); got != "" {
		t.Fatalf("saved mid-burst: %q", got)
	}
	now = now.Add(200 * time.Millisecond)
	must(t, k.RecordSettledScreen(nil))
	if !strings.Contains(savedScreen(t, dir, id), "painted") {
		t.Fatalf("not saved once settled: %q", savedScreen(t, dir, id))
	}
	// Within the interval of the last save, nothing more is taken.
	writeCaptureFrame(t, socket, uptty.ChannelStdout, " again")
	waitFor(t, "the output", func() bool { return strings.Contains(r.entries[id].conn.Session().ANSIScreen(), "again") })
	must(t, k.FrameSent("", term))
	now = now.Add(500 * time.Millisecond)
	must(t, k.RecordSettledScreen(nil))
	if strings.Contains(savedScreen(t, dir, id), "again") {
		t.Fatal("saved within the interval")
	}
	now = now.Add(time.Second)
	must(t, k.RecordSettledScreen(nil))
	if !strings.Contains(savedScreen(t, dir, id), "again") {
		t.Fatal("not saved an interval on")
	}
	// The same screen is not written again.
	before := liveRecord(t, dir, id)["saved_at"]
	must(t, k.FrameSent("", term))
	now = now.Add(2 * time.Second)
	must(t, k.RecordSettledScreen(nil))
	if liveRecord(t, dir, id)["saved_at"] != before {
		t.Fatal("an unchanged screen was written again")
	}
}

func TestTheKeeperWithoutALiveConnectorSavesNothing(t *testing.T) {
	dir := t.TempDir()
	r := newKeptRegistry(t, dir, true, 7)
	ctx := context.Background()
	id := "pam-suokki-capture-2-3"
	if _, err := r.CreateSession(ctx, capturePayload(id, "/run/c/s.sock")); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	k := r.keeperFor(r.entries[id])
	r.mu.Unlock()
	k.saveNow()
	k.attach(newFakeConnector())
	k.saveNow()
	if got := savedScreen(t, dir, id); got != "" {
		t.Fatalf("saved %q", got)
	}
	k.AttemptStarted()
	k.AttemptEnded(nil)
	for _, err := range []error{k.Connected(), k.WireReceived(""), k.ControlReceived(nil), k.InputReceived("")} {
		if err != nil {
			t.Fatal(err)
		}
	}
}

// A kept session's bridge is watched by the keeper, and runs a settle loop,
// even with the recording off; with a recording both watch it.
func TestAKeptSessionsBridgeIsWatchedByTheKeeper(t *testing.T) {
	dir := t.TempDir()
	r := newKeptRegistry(t, dir, true, 7)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	sink, hubURL := newWorkerSink(t)
	r.SetHubLink(ctx, hub.NewTermHub(hub.TermHubConfig{}), hubURL, "")
	id := "pam-suokki-capture-4-5"
	socket := startKept(t, r, id)
	waitFor(t, "the worker socket", sink.helloed)
	writeCaptureFrame(t, socket, uptty.ChannelStdout, "nobody watches")
	waitFor(t, "the screen saved", func() bool { return strings.Contains(savedScreen(t, dir, id), "nobody watches") })

	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.entries[id]
	if observer, settles := r.observerFor(e); observer != e.keeper || !settles {
		t.Fatalf("an unrecorded kept session is watched by %T, settles %v", observer, settles)
	}
	r.recording = recording.NewInMemoryStore()
	if observer, settles := r.observerFor(e); len(observer.(multiObserver)) != 2 || !settles {
		t.Fatalf("a recorded kept session is watched by %v, settles %v", observer, settles)
	}
	if e.recorder.instance != e.keeper.instance {
		t.Fatalf("recording instance %q, keeper's %q", e.recorder.instance, e.keeper.instance)
	}
	plain := &sessionEntry{def: serverconfig.SessionDefinition{SessionID: "provide-shell"}}
	r.recording = nil
	if observer, settles := r.observerFor(plain); observer != nil || settles {
		t.Fatalf("an unkept unrecorded session is watched by %v, settles %v", observer, settles)
	}
}

type failingObserver struct {
	bridge.Observer
	calls *[]string
	err   error
}

func (f failingObserver) record(name string) error { *f.calls = append(*f.calls, name); return f.err }
func (f failingObserver) AttemptStarted()          { _ = f.record("started") }
func (f failingObserver) AttemptEnded(error)       { _ = f.record("ended") }
func (f failingObserver) Connected() error         { return f.record("connected") }
func (f failingObserver) FrameSent(string, map[string]any) error {
	return f.record("sent")
}
func (f failingObserver) WireReceived(string) error            { return f.record("wire") }
func (f failingObserver) ControlReceived(map[string]any) error { return f.record("control") }
func (f failingObserver) InputReceived(string) error           { return f.record("input") }
func (f failingObserver) RecordSettledScreen(func() map[string]any) error {
	return f.record("settled")
}

// Every observer sees every call; the first error is the one returned.
func TestEveryObserverSeesEveryCallAndTheFirstErrorWins(t *testing.T) {
	var calls []string
	first, second := errors.New("first"), errors.New("second")
	m := multiObserver{failingObserver{calls: &calls, err: first}, failingObserver{calls: &calls, err: second}}
	m.AttemptStarted()
	m.AttemptEnded(nil)
	for _, err := range []error{
		m.Connected(), m.FrameSent("", nil), m.WireReceived(""), m.ControlReceived(nil),
		m.InputReceived(""), m.RecordSettledScreen(nil),
	} {
		if !errors.Is(err, first) {
			t.Fatalf("err = %v", err)
		}
	}
	if len(calls) != 16 {
		t.Fatalf("calls = %v", calls)
	}
}

// keptBeforeRestart leaves, as a stopped server does, a kept session id with
// its definition, its owner patched in, and a screen.
func keptBeforeRestart(t *testing.T, dir, id, socket string) string {
	t.Helper()
	r := newKeptRegistry(t, dir, true, 7)
	ctx := context.Background()
	if _, err := r.CreateSession(ctx, capturePayload(id, socket)); err != nil {
		t.Fatal(err)
	}
	if _, err := r.UpdateSession(ctx, id, map[string]any{"visibility": "private"}); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	instance := r.keeperFor(r.entries[id]).instance
	r.mu.Unlock()
	r.store.SaveScreen(id, "\x1b[1mdeck\x1b[0m\nline two", 30, 5, []string{"scrolled"})
	r.Shutdown(ctx)
	return instance
}

func TestALiveLoginComesBackUnderItsIdWithItsScreen(t *testing.T) {
	dir := t.TempDir()
	id := liveID(t)
	socket := shortCaptureDir(t) + "/cap.sock"
	instance := keptBeforeRestart(t, dir, id, socket)

	r := newKeptRegistry(t, dir, true, 7)
	ctx := context.Background()
	r.RestoreCaptureSessions(ctx, func(string) bool { return true })
	def, ok := r.GetDefinition(ctx, id)
	if !ok || def.Visibility != "private" || def.ConnectorConfig["socket_path"] != socket {
		t.Fatalf("restored %+v %v", def, ok)
	}
	if _, err := r.StartSession(ctx, id); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { r.Shutdown(context.Background()) })
	r.mu.Lock()
	e := r.entries[id]
	r.mu.Unlock()
	if e.keeper.instance != instance {
		t.Fatalf("instance %q, want %q", e.keeper.instance, instance)
	}
	snap := e.conn.Snapshot()
	if snap.Cols != 30 || snap.Rows != 5 || !strings.HasPrefix(snap.Screen, "deck") {
		t.Fatalf("restored screen %dx%d %q", snap.Cols, snap.Rows, snap.Screen)
	}
	// It binds the socket again, and what the login writes reaches it.
	writeCaptureFrame(t, socket, uptty.ChannelStdout, "\r\nback")
	waitFor(t, "output after the restart", func() bool { return strings.Contains(e.conn.Snapshot().Screen, "back") })
}

func TestALoginThatEndedWhileTheServerWasDownBecomesAPreviousSession(t *testing.T) {
	dir := t.TempDir()
	ticks := selfStartTicks(t)
	id := "pam-suokki-capture-" + strconv.Itoa(os.Getpid()) + "-" + strconv.FormatInt(ticks+1, 10)
	keptBeforeRestart(t, dir, id, "/run/c/s.sock")
	r := newKeptRegistry(t, dir, true, 7)
	r.RestoreCaptureSessions(context.Background(), func(string) bool { return true })
	if _, ok := r.GetDefinition(context.Background(), id); ok {
		t.Fatal("a dead login was restored")
	}
	if rec, ok := r.store.PreviousSession(id); !ok || rec.Scrollback[0] != "scrolled" {
		t.Fatalf("previous %+v %v", rec, ok)
	}
}

func TestASocketOutsideTheCaptureDirectoryIsNotBound(t *testing.T) {
	dir := t.TempDir()
	id := liveID(t)
	keptBeforeRestart(t, dir, id, "/run/c/s.sock")
	r := newKeptRegistry(t, dir, true, 7)
	var asked string
	r.RestoreCaptureSessions(context.Background(), func(socket string) bool { asked = socket; return false })
	if _, ok := r.GetDefinition(context.Background(), id); ok || asked != "/run/c/s.sock" {
		t.Fatalf("restored, asked %q", asked)
	}
	if _, ok := r.store.PreviousSummary(id); !ok {
		t.Fatal("not kept as a previous session")
	}
}

func TestASessionThatExistsOrIsBeingTornDownIsLeftAsItIs(t *testing.T) {
	dir := t.TempDir()
	id := liveID(t)
	keptBeforeRestart(t, dir, id, "/run/c/s.sock")
	ctx := context.Background()

	r := newKeptRegistry(t, dir, true, 7)
	payload := capturePayload(id, "/run/c/s.sock")
	payload["display_name"] = "made first"
	if _, err := r.CreateSession(ctx, payload); err != nil {
		t.Fatal(err)
	}
	r.RestoreCaptureSessions(ctx, func(string) bool { return true })
	if def, _ := r.GetDefinition(ctx, id); def.DisplayName != "made first" {
		t.Fatalf("replaced: %+v", def)
	}

	held := newKeptRegistry(t, dir, true, 7)
	held.retiring[id] = make(chan struct{})
	held.RestoreCaptureSessions(ctx, func(string) bool { return true })
	if _, ok := held.GetDefinition(ctx, id); ok {
		t.Fatal("restored over a teardown")
	}
}

func TestWithoutPersistCaptureOnlyPreviousSessionsAreRead(t *testing.T) {
	dir := t.TempDir()
	id := liveID(t)
	keptBeforeRestart(t, dir, id, "/run/c/s.sock")
	r := newKeptRegistry(t, dir, false, 7)
	r.RestoreCaptureSessions(context.Background(), func(string) bool { return true })
	if _, ok := r.GetDefinition(context.Background(), id); ok {
		t.Fatal("restored without persist_capture")
	}
	if _, err := os.Stat(filepath.Join(dir, "live", id+".json")); err != nil {
		t.Fatalf("the live file went: %v", err)
	}
}

// A restored screen stays pending until a start succeeds: one whose socket
// another process still serves paints nothing, and the retry paints it.
func TestARestoredScreenSurvivesAStartThatFailed(t *testing.T) {
	dir := t.TempDir()
	id := liveID(t)
	socket := shortCaptureDir(t) + "/cap.sock"
	keptBeforeRestart(t, dir, id, socket)
	r := newKeptRegistry(t, dir, true, 7)
	ctx := context.Background()
	r.RestoreCaptureSessions(ctx, func(string) bool { return true })
	held, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	if st, err := r.StartSession(ctx, id); err != nil || st.LifecycleState != server.LifecycleStopped {
		t.Fatalf("started on a socket another process serves: %v %v", st, err)
	}
	_ = held.Close()
	if _, err := r.StartSession(ctx, id); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { r.Shutdown(context.Background()) })
	r.mu.Lock()
	e := r.entries[id]
	r.mu.Unlock()
	if snap := e.conn.Snapshot(); !strings.HasPrefix(snap.Screen, "deck") {
		t.Fatalf("the retry painted %q", snap.Screen)
	}
	if _, _, _, pending := r.store.PendingRestore(id); pending {
		t.Fatal("still pending after a start that succeeded")
	}
}

// The scrollback carries over every attach, not only the first after a
// restart: a stop and start in this process keeps it too.
func TestTheScrollbackCarriesOverAStopAndStart(t *testing.T) {
	dir := t.TempDir()
	id := liveID(t)
	socket := shortCaptureDir(t) + "/cap.sock"
	keptBeforeRestart(t, dir, id, socket)
	r := newKeptRegistry(t, dir, true, 7)
	ctx := context.Background()
	r.RestoreCaptureSessions(ctx, func(string) bool { return true })
	t.Cleanup(func() { r.Shutdown(context.Background()) })
	for attach := range 2 {
		if _, err := r.StartSession(ctx, id); err != nil {
			t.Fatal(err)
		}
		r.mu.Lock()
		state := r.entries[id].conn.(connectors.CaptureState)
		r.mu.Unlock()
		if got := state.Scrollback(); len(got) == 0 || got[0] != "scrolled" {
			t.Fatalf("attach %d: scrollback = %q", attach, got)
		}
		if _, err := r.StopSession(ctx, id); err != nil {
			t.Fatal(err)
		}
	}
}
