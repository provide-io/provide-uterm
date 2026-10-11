//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/annotation"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// --- operator annotations --------------------------------------------------

// An operator annotation lands in the session's open recording as an
// "annotation" entry carrying the reference's annotation_data; with no
// recording open it is not recorded, and the call still succeeds.
func TestOperatorAnnotationsAreRecorded(t *testing.T) {
	ctx := context.Background()
	r, store := recordingRegistry(t)
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	rec := r.recorderFor(r.entries["provide-shell"])
	r.mu.Unlock()
	ann := server.Annotation{Label: "note", Description: "why", Severity: "high", Principal: "ops"}

	// Not yet recording: accepted, nothing written.
	if _, _, err := r.AnnotateSession(ctx, "provide-shell", ann); err != nil {
		t.Fatal(err)
	}
	rec.AttemptStarted()
	if _, _, err := r.AnnotateSession(ctx, "provide-shell", ann); err != nil {
		t.Fatal(err)
	}
	rec.AttemptEnded(nil)
	got := entriesOf(t, store, "provide-shell", "annotation")
	want := map[string]any{"label": "note", "description": "why", "severity": "high", "source": "agent", "principal": "ops"}
	if len(got) != 1 || !reflect.DeepEqual(got[0]["data"], want) {
		t.Fatalf("annotation entries = %v", got)
	}
}

// waitFailed waits for the recorder's writer to report the store failure a
// batch it was handed ran into.
func waitFailed(t *testing.T, rec *sessionRecorder) {
	t.Helper()
	waitFor(t, "the writer to report the store failure", func() bool { return rec.writer.failure() != nil })
}

// A recording the store cannot write is reported, as the reference lets the
// logger's error out of annotate_session: here by the next annotation once
// the writer has met the failure, since the write that met it was queued.
func TestOperatorAnnotationWriteFailureIsAnError(t *testing.T) {
	ctx := context.Background()
	r := newTestRegistry(t)
	r.SetRecording(failingWriteStore{}, nil)
	r.recCfg.FlushBatchSize = 1
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	rec := r.recorderFor(r.entries["provide-shell"])
	r.mu.Unlock()
	rec.AttemptStarted()
	defer rec.AttemptEnded(nil)
	if _, _, err := r.AnnotateSession(ctx, "provide-shell", server.Annotation{Label: "x"}); err != nil {
		t.Fatalf("queueing the first annotation failed: %v", err)
	}
	waitFailed(t, rec)
	if _, _, err := r.AnnotateSession(ctx, "provide-shell", server.Annotation{Label: "x"}); err == nil {
		t.Fatal("a failed recording write was not reported")
	}
}

// A served connection that ends on an error records it as runtime_error
// before the recording closes, as the reference's run loop records the
// exception that ended _bridge_session.
func TestAConnectionEndRecordsTheError(t *testing.T) {
	rec, store := newTestRecorder(t, testRecordingConfig())
	rec.AttemptStarted()
	must(t, rec.Connected())
	rec.AttemptEnded(errors.New("failed to read frame header: EOF"))
	want := []string{"log_start", "runtime_started", "runtime_error", "log_stop"}
	if got := eventNames(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
}

// failingRecorder opens a recording on a store that takes no writes, has the
// writer meet that, and returns it.
func failingRecorder(t *testing.T, c serverconfig.RecordingConfig) *sessionRecorder {
	t.Helper()
	c.FlushBatchSize = 1
	rec := newSessionRecorder("s1", failingWriteStore{}, c, annotation.NewPatternDetector(nil), nil)
	rec.setEnabled(true)
	rec.AttemptStarted()
	must(t, rec.Connected()) // queued: the failure is the writer's to meet
	waitFailed(t, rec)
	return rec
}

// A recording write that fails is handed back to the bridge, which ends the
// connection on it; the attempt's end then tries to record runtime_error and
// closes the recording either way. Every observed point reports it.
func TestRecordingWriteFailuresAreReturned(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.ControlChannelMode = "wire"
	rec := failingRecorder(t, cfg)
	steps := map[string]func() error{
		"Connected":       rec.Connected,
		"FrameSent":       func() error { return rec.FrameSent("x", snapshotFrame("$ ")) },
		"WireReceived":    func() error { return rec.WireReceived("x") },
		"ControlReceived": func() error { return rec.ControlReceived(map[string]any{"type": "snapshot_req"}) },
		"InputReceived":   func() error { return rec.InputReceived("x") },
		"flush":           rec.flush,
		"recordAnnotation": func() error {
			return rec.recordAnnotation(map[string]any{"label": "x"})
		},
	}
	for name, step := range steps {
		if err := step(); err == nil {
			t.Errorf("%s: a failed write was not returned", name)
		}
	}
	rec.AttemptEnded(errors.New("disk full"))
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if rec.rec != nil {
		t.Fatal("the failed attempt's recording was not released")
	}
}

// A step observed while the store is failing is reported before it logs or
// acts: the sequence does not advance and the prompt flag does not move, as
// the first raising call ends the reference's method.
func TestAStepObservedWhileTheStoreFailsIsNotLogged(t *testing.T) {
	for name, cfg := range map[string]func(*serverconfig.RecordingConfig){
		"snapshot": func(*serverconfig.RecordingConfig) {},
		"wire":     func(c *serverconfig.RecordingConfig) { c.ControlChannelMode = "wire" },
	} {
		t.Run(name, func(t *testing.T) {
			c := testRecordingConfig()
			cfg(&c)
			rec := failingRecorder(t, c)
			defer rec.AttemptEnded(nil)
			if rec.FrameSent("x", snapshotFrame("Password:")) == nil {
				t.Fatal("no error")
			}
			if rec.InputReceived("DROP TABLE y;") == nil {
				t.Fatal("no error")
			}
			if rec.FrameSent("x", map[string]any{"type": "term", "data": "DROP TABLE z;"}) == nil {
				t.Fatal("no error")
			}
			rec.mu.Lock()
			defer rec.mu.Unlock()
			if rec.eventSeq != 0 || rec.atPasswordPrompt {
				t.Fatalf("eventSeq = %d, prompt = %t after steps that were refused", rec.eventSeq, rec.atPasswordPrompt)
			}
		})
	}
}

// failAfter accepts ok appends, then fails every one after until ok is
// raised again.
type failAfter struct {
	*recording.InMemoryStore
	mu sync.Mutex
	ok int
}

func (f *failAfter) AppendEvents(id string, events []recording.Event) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.ok == 0 {
		return errors.New("disk full")
	}
	f.ok--
	return f.InMemoryStore.AppendEvents(id, events)
}

func (f *failAfter) allow(n int) {
	f.mu.Lock()
	f.ok = n
	f.mu.Unlock()
}

// A batch the store refused is not dropped: it stays at the head of the
// queue, the failure is reported, and once the store takes writes again it
// lands ahead of everything logged after it.
func TestAFailedBatchIsRetriedInOrder(t *testing.T) {
	store := &failAfter{InMemoryStore: recording.NewInMemoryStore(), ok: 1}
	cfg := testRecordingConfig()
	cfg.FlushBatchSize = 1
	rec := newSessionRecorder("s1", store, cfg, nil, nil)
	rec.setEnabled(true)
	rec.AttemptStarted()
	must(t, rec.Connected())        // written
	must(t, rec.InputReceived("a")) // refused by the store
	waitFailed(t, rec)
	if rec.InputReceived("b") == nil {
		t.Fatal("input observed while the head batch is unwritten was accepted")
	}
	if err := rec.flush(); err == nil {
		t.Fatal("a flush the store refused reported success")
	}
	store.allow(100)
	must(t, rec.flush())
	if rec.writer.failure() != nil {
		t.Fatal("the failure outlived the retry that wrote the batch")
	}
	must(t, rec.InputReceived("c"))
	rec.AttemptEnded(nil)
	want := []string{"log_start", "runtime_started", "send", "send", "log_stop"}
	if got := eventNames(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
	sends := entries(t, store, "send")
	if sends[0]["data"].(map[string]any)["keys"] != "a" || sends[1]["data"].(map[string]any)["keys"] != "c" {
		t.Fatalf("sends = %v, want a then c", sends)
	}
}

// A recording that cannot be closed keeps what it could not write, for that
// recording alone; the next attempt's start retries the close, so it lands,
// log_stop included, before the new log_start. The new connection does not
// inherit the old failure.
func TestAnUnwrittenRecordingIsWrittenBeforeTheNextStarts(t *testing.T) {
	store := &failAfter{InMemoryStore: recording.NewInMemoryStore()}
	cfg := testRecordingConfig()
	rec := newSessionRecorder("s1", store, cfg, nil, nil)
	rec.setEnabled(true)
	rec.AttemptStarted()
	must(t, rec.Connected())
	rec.AttemptEnded(nil) // Stop's batch is refused, so log_stop is not written
	if got := eventNames(t, store); !reflect.DeepEqual(got, []string{"log_start"}) {
		t.Fatalf("events = %v, want the recording left open", got)
	}
	store.allow(100)
	rec.AttemptStarted()
	must(t, rec.Connected())
	rec.AttemptEnded(nil)
	want := []string{"log_start", "runtime_started", "log_stop", "log_start", "runtime_started", "log_stop"}
	if got := eventNames(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
}

// syncBuffer is a log sink safe to write from the periodic flusher.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// A recording whose close fails at its end and again at the next start is
// dropped with a warning naming what was lost -- never written after the new
// recording's log_start -- and the new connection starts clean: its
// Connected is not failed by the old recording's error.
func TestAnUnwritableRecordingIsDroppedNotInterleaved(t *testing.T) {
	store := &failAfter{InMemoryStore: recording.NewInMemoryStore()}
	logs := &syncBuffer{}
	rec := newSessionRecorder("s1", store, testRecordingConfig(), nil, slog.New(slog.NewTextHandler(logs, nil)))
	rec.setEnabled(true)
	rec.AttemptStarted()
	must(t, rec.Connected())
	must(t, rec.InputReceived("old"))
	rec.AttemptEnded(nil) // drain fails: runtime_started and send stay queued
	rec.AttemptStarted()  // the retried close fails too: dropped
	if !strings.Contains(logs.String(), "msg=hosted_session_recording_dropped session_id=s1 unwritten_entries=2") {
		t.Fatalf("the dropped recording was not reported: %s", logs)
	}
	store.allow(100)
	must(t, rec.Connected())
	must(t, rec.InputReceived("new"))
	rec.AttemptEnded(nil)
	rec.AttemptStarted() // nothing left over to settle
	rec.AttemptEnded(nil)
	want := []string{"log_start", "log_start", "runtime_started", "send", "log_stop", "log_start", "log_stop"}
	if got := eventNames(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
	if sends := entries(t, store, "send"); len(sends) != 1 || sends[0]["data"].(map[string]any)["keys"] != "new" {
		t.Fatalf("sends = %v, want only the new recording's", sends)
	}
	if n := strings.Count(logs.String(), "hosted_session_recording_dropped"); n != 1 {
		t.Fatalf("dropped reported %d times, want once", n)
	}
}

// A recording left unclosed is settled at the next attempt's start even when
// that attempt does not record.
func TestAnUnwrittenRecordingIsSettledWhenTheNextIsNotRecorded(t *testing.T) {
	store := &failAfter{InMemoryStore: recording.NewInMemoryStore()}
	rec := newSessionRecorder("s1", store, testRecordingConfig(), nil, nil)
	rec.setEnabled(true)
	rec.AttemptStarted()
	must(t, rec.Connected())
	rec.AttemptEnded(nil)
	store.allow(100)
	rec.setEnabled(false)
	rec.AttemptStarted()
	rec.AttemptEnded(nil)
	want := []string{"log_start", "runtime_started", "log_stop"}
	if got := eventNames(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
}

// blockingStore holds every append until released, as a webhook store holds
// the caller for its POST.
type blockingStore struct {
	*recording.InMemoryStore
	entered chan struct{}
	release chan struct{}
	once    sync.Once
}

func newBlockingStore() *blockingStore {
	return &blockingStore{
		InMemoryStore: recording.NewInMemoryStore(),
		entered:       make(chan struct{}),
		release:       make(chan struct{}),
	}
}

func (b *blockingStore) AppendEvents(id string, events []recording.Event) error {
	b.once.Do(func() { close(b.entered) })
	<-b.release
	return b.InMemoryStore.AppendEvents(id, events)
}

// within fails the test unless fn returns inside a deadline far below any
// store's timeout.
func within(t *testing.T, what string, fn func()) {
	t.Helper()
	done := make(chan struct{})
	go func() { defer close(done); fn() }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatalf("%s blocked on the store", what)
	}
}

// A store write that blocks holds up neither the bridge's goroutines nor an
// operator's annotation: every observed point returns while the writer waits
// on the store, and what they logged lands in order once it is released.
func TestABlockingStoreDoesNotBlockTheBridge(t *testing.T) {
	store := newBlockingStore()
	cfg := testRecordingConfig()
	cfg.FlushBatchSize = 1
	cfg.ControlChannelMode = "wire"
	rec := newSessionRecorder("s1", store, cfg, annotation.NewPatternDetector(nil), nil)
	rec.setEnabled(true)
	rec.AttemptStarted()
	must(t, rec.Connected())
	<-store.entered // the writer is now inside the store, holding runtime_started

	within(t, "the observed points", func() {
		must(t, rec.FrameSent("x", snapshotFrame("$ ")))
		must(t, rec.WireReceived("ls\r"))
		must(t, rec.ControlReceived(map[string]any{"type": "snapshot_req"}))
		must(t, rec.InputReceived("ls\r"))
		for i := range 20 {
			must(t, rec.FrameSent(fmt.Sprint(i), map[string]any{"type": "term", "data": fmt.Sprint(i)}))
		}
		must(t, rec.recordAnnotation(map[string]any{"label": "note"}))
	})

	// flush waits for the store, and does so outside the recorder's lock.
	flushed := make(chan error, 1)
	go func() { flushed <- rec.flush() }()
	within(t, "an input observed during a flush", func() { must(t, rec.InputReceived("x")) })
	select {
	case err := <-flushed:
		t.Fatalf("flush returned (%v) before the store took the batches", err)
	case <-time.After(20 * time.Millisecond):
	}
	close(store.release)
	must(t, <-flushed)
	rec.AttemptEnded(nil)

	want := []string{"log_start", "runtime_started", "wire_send", "control_send", "read", "wire_recv", "control_recv", "send"}
	for range 20 {
		want = append(want, "wire_send")
	}
	want = append(want, "annotation", "send", "log_stop")
	if got := eventNames(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v\nwant %v", got, want)
	}
	for i, e := range entries(t, store, "wire_send")[1:] {
		if e["data"].(map[string]any)["text"] != fmt.Sprint(i) {
			t.Fatalf("wire_send %d = %v, out of order", i, e)
		}
	}
}

// Every goroutine the bridge observes from, an operator annotating and a
// route flushing, all at once, with the periodic flusher running: nothing is
// lost, and -race sees no unguarded state.
func TestConcurrentRecordingLosesNothing(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.FlushBatchSize = 3
	cfg.FlushIntervalS = 0.001
	rec, store := newTestRecorder(t, cfg)
	rec.AttemptStarted()
	var wg sync.WaitGroup
	const n = 50
	for _, step := range []func(int) error{
		func(i int) error { return rec.InputReceived(fmt.Sprint(i)) },
		func(i int) error { return rec.FrameSent("x", snapshotFrame(fmt.Sprint(i))) },
		func(i int) error { return rec.recordAnnotation(map[string]any{"n": i}) },
		func(int) error { return rec.flush() },
	} {
		wg.Go(func() {
			for i := range n {
				if err := step(i); err != nil {
					t.Error(err)
				}
			}
		})
	}
	wg.Wait()
	rec.AttemptEnded(nil)
	for event, want := range map[string]int{"send": n, "read": n, "annotation": n, "log_stop": 1} {
		if got := len(entries(t, store, event)); got != want {
			t.Errorf("%s entries = %d, want %d", event, got, want)
		}
	}
}

// --- batchWriter ------------------------------------------------------------

// Nothing queued: drain has nothing to wait for and nothing failed.
func TestAnIdleWriterDrainsAtOnce(t *testing.T) {
	w := newBatchWriter(failingWriteStore{})
	must(t, w.drain())
	must(t, w.failure())
}

// A refused batch: AppendEvents still succeeds (it only queues), drain
// reports the store's error with the batch kept, and EndSession does not close
// a recording it could not write.
func TestAWriterKeepsWhatTheStoreRefuses(t *testing.T) {
	ends := &endCounter{failAfter: failAfter{InMemoryStore: recording.NewInMemoryStore()}}
	w := newBatchWriter(ends)
	must(t, w.AppendEvents("s1", []recording.Event{{"event": "a"}}))
	if err := w.drain(); err == nil || err.Error() != "disk full" {
		t.Fatalf("drain = %v, want the store's error", err)
	}
	if len(w.queue) != 1 {
		t.Fatalf("queue = %v, want the refused batch kept", w.queue)
	}
	if w.EndSession("s1") == nil || ends.ended != 0 {
		t.Fatalf("EndSession closed a recording with %d batch unwritten", len(w.queue))
	}
	if n := w.unwritten(); n != 1 {
		t.Fatalf("unwritten = %d, want the refused batch's one entry", n)
	}
	ends.allow(1)
	must(t, w.EndSession("s1"))
	if ends.ended != 1 || w.unwritten() != 0 {
		t.Fatalf("ended = %d, queue = %v after the store recovered", ends.ended, w.queue)
	}
}

type endCounter struct {
	failAfter
	ended int
}

func (e *endCounter) EndSession(id string) error {
	e.ended++
	return e.InMemoryStore.EndSession(id)
}

// must fails the test on an unexpected recording error.
func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// A frame the recording cannot encode is the one write failure still met on
// the caller's goroutine (the entry is sized as it is logged): it is reported
// at once, by the control entry in wire mode and the read entry otherwise,
// and the step stops there.
func TestAnUnencodableFrameIsReported(t *testing.T) {
	bad := map[string]any{"type": "snapshot", "screen": "$ ", "fn": func() {}}
	for _, mode := range []string{"exclude", "wire"} {
		cfg := testRecordingConfig()
		cfg.ControlChannelMode = mode
		rec, _ := newTestRecorder(t, cfg)
		rec.AttemptStarted()
		if rec.FrameSent("x", bad) == nil {
			t.Fatalf("%s: an unencodable frame was not reported", mode)
		}
		if rec.eventSeq != 0 {
			t.Fatalf("%s: eventSeq = %d after a failed step", mode, rec.eventSeq)
		}
		rec.AttemptEnded(nil)
	}
}

// --- settling an unclosed recording at the session's final stop ------------

// leaveUnclosed opens a recording on rec, logs into it and ends it while the
// store refuses every append, leaving it unclosed.
func leaveUnclosed(t *testing.T, rec *sessionRecorder) {
	t.Helper()
	rec.AttemptStarted()
	must(t, rec.Connected())
	rec.AttemptEnded(nil)
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if rec.unclosed == nil || rec.unclosedWriter == nil {
		t.Fatal("the failed close did not leave the recording unclosed")
	}
}

// settle retries the close once: on a recovered store what was owed lands,
// then log_stop; a second settle has nothing left to do.
func TestSettleClosesAnUnclosedRecording(t *testing.T) {
	store := &failAfter{InMemoryStore: recording.NewInMemoryStore()}
	logs := &syncBuffer{}
	rec := newSessionRecorder("s1", store, testRecordingConfig(), nil, slog.New(slog.NewTextHandler(logs, nil)))
	rec.setEnabled(true)
	leaveUnclosed(t, rec)
	store.allow(100)
	rec.settle()
	rec.settle()
	if got := eventNames(t, store); !reflect.DeepEqual(got, []string{"log_start", "runtime_started", "log_stop"}) {
		t.Fatalf("events = %v", got)
	}
	if strings.Contains(logs.String(), "hosted_session_recording_dropped") {
		t.Fatalf("a settled recording was reported dropped: %s", logs)
	}
	if rec.unclosed != nil || rec.unclosedWriter != nil {
		t.Fatal("the settled recording is still held")
	}
}

// settle on a store still refusing drops the recording, reported once with
// what was lost, and lets it go.
func TestSettleDropsWhatStillCannotBeWritten(t *testing.T) {
	store := &failAfter{InMemoryStore: recording.NewInMemoryStore()}
	logs := &syncBuffer{}
	rec := newSessionRecorder("s1", store, testRecordingConfig(), nil, slog.New(slog.NewTextHandler(logs, nil)))
	rec.setEnabled(true)
	leaveUnclosed(t, rec)
	rec.settle()
	store.allow(100)
	rec.settle()
	if !strings.Contains(logs.String(), "msg=hosted_session_recording_dropped session_id=s1 unwritten_entries=1") ||
		strings.Count(logs.String(), "hosted_session_recording_dropped") != 1 {
		t.Fatalf("the drop was not reported exactly once: %s", logs)
	}
	if got := eventNames(t, store); !reflect.DeepEqual(got, []string{"log_start"}) {
		t.Fatalf("events = %v, want the dropped entries never written", got)
	}
	if rec.unclosed != nil || rec.unclosedWriter != nil {
		t.Fatal("the dropped recording is still held")
	}
}

// unclosedRegistry is a registry whose provide-shell recorder holds a
// recording its last connection could not close, and the store, now taking
// writes again.
func unclosedRegistry(t *testing.T) (*SessionRegistryImpl, *failAfter, *sessionRecorder) {
	t.Helper()
	ctx := context.Background()
	store := &failAfter{InMemoryStore: recording.NewInMemoryStore()}
	r := newTestRegistry(t)
	r.SetRecording(store, nil)
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	rec := r.recorderFor(r.entries["provide-shell"])
	r.mu.Unlock()
	leaveUnclosed(t, rec)
	store.allow(100)
	return r, store, rec
}

// A session stopped for good, deleted, or shut down with the server settles
// its unclosed recording then and there: no next attempt is coming to do it.
func TestTheFinalStopSettlesAnUnclosedRecording(t *testing.T) {
	for name, stop := range map[string]func(*SessionRegistryImpl){
		"StopSession": func(r *SessionRegistryImpl) {
			if _, err := r.StopSession(context.Background(), "provide-shell"); err != nil {
				t.Fatal(err)
			}
		},
		"DeleteSession": func(r *SessionRegistryImpl) { must(t, r.DeleteSession(context.Background(), "provide-shell")) },
		"Shutdown":      func(r *SessionRegistryImpl) { r.Shutdown(context.Background()) },
	} {
		t.Run(name, func(t *testing.T) {
			r, store, rec := unclosedRegistry(t)
			stop(r)
			want := []string{"log_start", "runtime_started", "log_stop"}
			var got []string
			for _, e := range entriesOf(t, store, "provide-shell", "") {
				got = append(got, e["event"].(string))
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("events = %v, want %v", got, want)
			}
			rec.mu.Lock()
			defer rec.mu.Unlock()
			if rec.unclosed != nil {
				t.Fatal("the recording is still held after the final stop")
			}
		})
	}
}

// Shutdown stops every session, not only the first.
func TestShutdownStopsEverySession(t *testing.T) {
	ctx := context.Background()
	r := newTestRegistry(t)
	r.mu.Lock()
	r.seed(serverconfig.SessionDefinition{SessionID: "second", DisplayName: "Second", ConnectorType: "shell"})
	ids := append([]string(nil), r.order...)
	r.mu.Unlock()
	for _, id := range ids {
		if _, err := r.StartSession(ctx, id); err != nil {
			t.Fatal(err)
		}
	}
	r.Shutdown(ctx)
	for _, id := range ids {
		st, err := r.GetSession(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		if st.LifecycleState != server.LifecycleStopped {
			t.Fatalf("%s: state = %q after shutdown", id, st.LifecycleState)
		}
	}
}

// A session that never recorded has no recorder to settle.
func TestSettleRecordingWithoutARecorder(t *testing.T) {
	settleRecording(nil)
}

// --- an empty match key is never deduped -----------------------------------

func TestAnEmptyMatchKeyIsNeverDeduped(t *testing.T) {
	rec, _ := annotatedRecorder(t)
	for range 2 {
		if !rec.admitSnapshotMatch("") || !rec.admitStreamMatch("") {
			t.Fatal("a match with an empty key was not recorded")
		}
	}
	if len(rec.readAnnotationKeys) != 0 || len(rec.snapshotOnlyKeys) != 0 {
		t.Fatalf("an empty key entered a set: %v, %v", rec.readAnnotationKeys, rec.snapshotOnlyKeys)
	}
	// A non-empty key still follows the rule.
	if !rec.admitSnapshotMatch("k") || rec.admitSnapshotMatch("k") {
		t.Fatal("a snapshot key was not deduped")
	}
	if rec.admitStreamMatch("k") || !rec.admitStreamMatch("k") {
		t.Fatal("a pending key was not consumed exactly once")
	}
}
