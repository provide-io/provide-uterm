//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"sync"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
)

// batchWriter takes a hosted session's recording writes off the bridge's
// goroutines. It wraps the session's recording.Store, and is the store its
// SessionLoggers write through.
//
// The reference awaits each full batch's flush on its own event loop
// (session_logger.py _write_event -> _flush), which costs that coroutine the
// write and nothing else. Here the SessionLogger is written from the bridge's
// send and receive goroutines, which bridge.Observer forbids to block, and the
// store may be a WebhookRecordingStore whose append is an HTTP POST. So
// AppendEvents only queues the batch and returns; one background goroutine at
// a time writes the queue, oldest first, to the real store.
//
//   - Order: SessionLogger hands batches over under its own lock, in the
//     order its entries were logged; they are queued in that order and only
//     one writer runs, so they reach the store in it.
//   - Nothing is dropped: a batch leaves the queue only once the store has
//     taken it. A failed write leaves it at the head, and the next batch
//     queued, the next periodic flush, or the next drain retries it, as
//     SessionLogger keeps a batch that failed to flush for its next attempt.
//   - Failure is reported: while the head cannot be written, failure returns
//     the error, and the recorder hands it to the bridge at the next point it
//     observes (the reference raises it from the write that filled the batch;
//     here that write has already returned).
//   - Lifecycle calls keep their place: StartSession and EndSession drain the
//     queue first, so log_start/log_stop stay outside the entries between them,
//     and flush (FlushRecording) drains it, so a reader sees what was logged.
//
// The queue is unbounded: a store that has stopped answering holds every
// batch in memory rather than losing one. The recording's own max_bytes quota,
// when set, bounds what can be logged into it.
type batchWriter struct {
	recording.Store

	mu      sync.Mutex
	settled *sync.Cond // broadcast whenever a writer goroutine finishes
	queue   []pendingBatch
	running bool
	err     error
}

// pendingBatch is one batch SessionLogger flushed, waiting for the store.
type pendingBatch struct {
	sessionID string
	events    []recording.Event
}

func newBatchWriter(store recording.Store) *batchWriter {
	w := &batchWriter{Store: store}
	w.settled = sync.NewCond(&w.mu)
	return w
}

// AppendEvents queues the batch for the background writer. It never fails:
// a failure to write it is reported by failure, later.
func (w *batchWriter) AppendEvents(sessionID string, events []recording.Event) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.queue = append(w.queue, pendingBatch{sessionID: sessionID, events: events})
	w.startLocked()
	return nil
}

// startLocked starts the writer if there is something to write and no writer
// is running. Caller holds w.mu.
func (w *batchWriter) startLocked() {
	if w.running || len(w.queue) == 0 {
		return
	}
	w.running = true
	go w.run()
}

// run writes the queue, oldest first, without holding w.mu across a store
// call, until it is empty or a write fails.
func (w *batchWriter) run() {
	w.mu.Lock()
	defer w.mu.Unlock()
	for len(w.queue) > 0 {
		head := w.queue[0]
		w.mu.Unlock()
		err := w.Store.AppendEvents(head.sessionID, head.events)
		w.mu.Lock()
		w.err = err
		if err != nil {
			break
		}
		w.queue = w.queue[1:]
	}
	w.running = false
	w.settled.Broadcast()
}

// drain writes everything queued, retrying a batch that failed before, and
// waits for it: nil when the queue is empty, else the error that stopped it.
// w.err is exactly that: run sets it from every store write, so it is nil
// after the write that emptied the queue and set only by one that left a
// batch at the head.
func (w *batchWriter) drain() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.startLocked()
	for w.running {
		w.settled.Wait()
	}
	return w.err
}

// failure is the error the head of the queue last failed with, while it is
// still unwritten; nil otherwise.
func (w *batchWriter) failure() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.err
}

// StartSession drains first, so a batch still owed from an earlier recording
// lands before this one's log_start. A drain that fails does not stop the new
// recording opening: its batches stay queued, behind which this one's follow.
func (w *batchWriter) StartSession(sessionID string, metadata map[string]any) error {
	_ = w.drain()
	return w.Store.StartSession(sessionID, metadata)
}

// EndSession drains first, so log_stop lands after every entry before it. A
// drain that fails is returned instead of closing the recording, as
// SessionLogger.Stop returns a failed flush without ending the session.
func (w *batchWriter) EndSession(sessionID string) error {
	if err := w.drain(); err != nil {
		return err
	}
	return w.Store.EndSession(sessionID)
}
