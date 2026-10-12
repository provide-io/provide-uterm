//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"errors"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

var errLogWrite = errors.New("log write refused")

// failingLog wraps a live recording and refuses one kind of write.
type failingLog struct {
	recordingLog
	wire    bool   // LogWire fails
	control bool   // LogControl fails
	send    bool   // LogSend fails
	event   string // LogEvent fails for this event name
}

func (f failingLog) LogWire(direction, text string) error {
	if f.wire {
		return errLogWrite
	}
	return f.recordingLog.LogWire(direction, text)
}

func (f failingLog) LogControl(direction string, control map[string]any) error {
	if f.control {
		return errLogWrite
	}
	return f.recordingLog.LogControl(direction, control)
}

func (f failingLog) LogSend(keys string) error {
	if f.send {
		return errLogWrite
	}
	return f.recordingLog.LogSend(keys)
}

func (f failingLog) LogEvent(event string, data map[string]any) error {
	if event == f.event {
		return errLogWrite
	}
	return f.recordingLog.LogEvent(event, data)
}

// wrapLog replaces the open recording with one that refuses the given write.
func wrapLog(t *testing.T, rec *sessionRecorder, wrap func(recordingLog) recordingLog) {
	t.Helper()
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if rec.rec == nil {
		t.Fatal("no open recording to wrap")
	}
	rec.rec = wrap(rec.rec)
}

// A refused wire entry is returned to the bridge and the step stops there.
func TestFrameSentReportsARefusedWireEntry(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.ControlChannelMode = "wire"
	rec, _ := newTestRecorder(t, cfg)
	rec.AttemptStarted()
	wrapLog(t, rec, func(l recordingLog) recordingLog { return failingLog{recordingLog: l, wire: true} })
	if err := rec.FrameSent("x", snapshotFrame("$ ")); !errors.Is(err, errLogWrite) {
		t.Fatalf("FrameSent = %v, want the refused wire write", err)
	}
	if rec.eventSeq != 0 {
		t.Fatalf("eventSeq = %d after a failed step", rec.eventSeq)
	}
	rec.AttemptEnded(nil)
}

// A refused control entry is returned too, after its wire entry was written.
func TestFrameSentReportsARefusedControlEntry(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.ControlChannelMode = "wire"
	rec, _ := newTestRecorder(t, cfg)
	rec.AttemptStarted()
	wrapLog(t, rec, func(l recordingLog) recordingLog { return failingLog{recordingLog: l, control: true} })
	if err := rec.FrameSent("x", map[string]any{"type": "hijack_state"}); !errors.Is(err, errLogWrite) {
		t.Fatalf("FrameSent = %v, want the refused control write", err)
	}
	rec.AttemptEnded(nil)
}

// Each annotation path returns a refused annotation write.
func TestAnnotationWriteFailuresAreReturned(t *testing.T) {
	refuseAnnotations := func(l recordingLog) recordingLog { return failingLog{recordingLog: l, event: "annotation"} }
	steps := map[string]func(rec *sessionRecorder) error{
		"snapshot": func(rec *sessionRecorder) error {
			return rec.FrameSent("x", snapshotFrame("$ sudo ls\n$ "))
		},
		"stream": func(rec *sessionRecorder) error {
			data := "$ sudo ls\r\n"
			return rec.FrameSent(controlchannel.EncodeTerminalData(data), map[string]any{"type": "term", "data": data})
		},
		"send": func(rec *sessionRecorder) error { return rec.InputReceived("sudo ls\r") },
	}
	for name, step := range steps {
		t.Run(name, func(t *testing.T) {
			rec, _ := annotatedRecorder(t)
			rec.AttemptStarted()
			wrapLog(t, rec, refuseAnnotations)
			if err := step(rec); !errors.Is(err, errLogWrite) {
				t.Fatalf("%s = %v, want the refused annotation write", name, err)
			}
			rec.AttemptEnded(nil)
		})
	}
}

// A refused input entry is returned and does not advance the sequence.
func TestInputReceivedReportsARefusedSendEntry(t *testing.T) {
	rec, _ := newTestRecorder(t, testRecordingConfig())
	rec.AttemptStarted()
	wrapLog(t, rec, func(l recordingLog) recordingLog { return failingLog{recordingLog: l, send: true} })
	if err := rec.InputReceived("ls\r"); !errors.Is(err, errLogWrite) {
		t.Fatalf("InputReceived = %v, want the refused send write", err)
	}
	if rec.eventSeq != 0 {
		t.Fatalf("eventSeq = %d after a failed step", rec.eventSeq)
	}
	rec.AttemptEnded(nil)
}
