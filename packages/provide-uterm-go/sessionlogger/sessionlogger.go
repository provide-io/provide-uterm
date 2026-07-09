//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package sessionlogger provides the JSONL session logger for recording BBS
// sessions. Port of provide.uterm.session_logger.
//
// The Logger field accepts any *slog.Logger; wire it to
// provide-telemetry's GetLogger in application code so records flow through
// the shared telemetry pipeline.
package sessionlogger

import (
	"encoding/base64"
	"encoding/json"
	"log/slog"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/redaction"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
)

// ControlChannelMode selects whether wire-level control-channel traffic is
// recorded.
type ControlChannelMode string

// Control-channel recording modes.
const (
	// ModeExclude drops wire/control records (the default).
	ModeExclude ControlChannelMode = "exclude"
	// ModeWire records raw wire chunks and decoded control frames.
	ModeWire ControlChannelMode = "wire"
)

// Options configure a SessionLogger.
type Options struct {
	// MaxBytes caps total bytes written; 0 = unlimited.
	MaxBytes int
	// ControlChannelMode defaults to ModeExclude.
	ControlChannelMode ControlChannelMode
	// Redactor, when set, rewrites logged text.
	Redactor redaction.Redactor
	// FlushInterval is the periodic flush cadence; zero selects 5s.
	FlushInterval time.Duration
	// BatchSize triggers a flush when the buffer reaches it; zero selects 100.
	BatchSize int
	// Logger receives quota/flush warnings; nil selects slog.Default().
	Logger *slog.Logger
}

// SessionLogger is an async session recorder over a pluggable
// recording.Store. Each log entry is a JSON object with at minimum
// {"ts": ..., "event": ..., "data": {...}}.
type SessionLogger struct {
	store              recording.Store
	maxBytes           int
	controlChannelMode ControlChannelMode
	redactor           redaction.Redactor
	flushInterval      time.Duration
	batchSize          int
	logger             *slog.Logger

	mu           sync.Mutex
	sessionID    string
	context      map[string]string
	bytesWritten int
	quotaWarned  bool
	buffer       []recording.Event

	flushStop chan struct{}
	flushDone chan struct{}
}

// New creates a SessionLogger over store.
func New(store recording.Store, opts Options) *SessionLogger {
	if opts.ControlChannelMode == "" {
		opts.ControlChannelMode = ModeExclude
	}
	if opts.FlushInterval == 0 {
		opts.FlushInterval = 5 * time.Second
	}
	if opts.BatchSize == 0 {
		opts.BatchSize = 100
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	return &SessionLogger{
		store:              store,
		maxBytes:           opts.MaxBytes,
		controlChannelMode: opts.ControlChannelMode,
		redactor:           opts.Redactor,
		flushInterval:      opts.FlushInterval,
		batchSize:          opts.BatchSize,
		logger:             opts.Logger,
	}
}

// Start begins a recording session.
func (l *SessionLogger) Start(sessionID string) error {
	l.mu.Lock()
	l.sessionID = sessionID
	l.mu.Unlock()

	metadata := map[string]any{"started_at": float64(time.Now().UnixNano()) / 1e9}
	if err := l.store.StartSession(sessionID, metadata); err != nil {
		return err
	}

	meta, err := l.store.RecordingMeta(sessionID)
	if err != nil {
		return err
	}
	l.mu.Lock()
	l.bytesWritten = int(meta.SizeBytes)
	l.flushStop = make(chan struct{})
	l.flushDone = make(chan struct{})
	l.mu.Unlock()

	go l.periodicFlush(l.flushStop, l.flushDone)
	return nil
}

// Stop finalizes the recording session, flushing buffered entries.
func (l *SessionLogger) Stop() error {
	l.mu.Lock()
	stop, done := l.flushStop, l.flushDone
	l.flushStop, l.flushDone = nil, nil
	l.mu.Unlock()
	if stop != nil {
		close(stop)
		<-done
	}

	if err := l.Flush(); err != nil {
		return err
	}
	l.mu.Lock()
	sessionID := l.sessionID
	l.mu.Unlock()
	if sessionID != "" {
		return l.store.EndSession(sessionID)
	}
	return nil
}

// LogSend logs sent keystrokes (redacted, with CP437 wire bytes).
func (l *SessionLogger) LogSend(keys string) error {
	keys = l.redactText(keys)
	payload := screen.EncodeCP437(keys)
	return l.writeEvent("send", map[string]any{
		"keys":      keys,
		"bytes_b64": base64.StdEncoding.EncodeToString(payload),
	})
}

// LogSendMasked logs a credential send without capturing the actual value.
func (l *SessionLogger) LogSendMasked(byteCount int) error {
	return l.writeEvent("send", map[string]any{
		"keys":       "***",
		"bytes_b64":  base64.StdEncoding.EncodeToString([]byte("***")),
		"masked":     true,
		"byte_count": byteCount,
	})
}

// LogScreen logs a screen snapshot with its raw bytes.
func (l *SessionLogger) LogScreen(snapshot session.Snapshot, raw []byte) error {
	rawText := l.redactText(screen.DecodeCP437(raw))
	rawBytes := screen.EncodeCP437(rawText)
	data := l.snapshotData(snapshot)
	data["raw"] = rawText
	data["raw_bytes_b64"] = base64.StdEncoding.EncodeToString(rawBytes)
	return l.writeEvent("read", data)
}

// snapshotData converts a snapshot to the Python dict shape with redacted
// string values.
func (l *SessionLogger) snapshotData(snap session.Snapshot) map[string]any {
	data := map[string]any{
		"screen":             l.redactText(snap.Screen),
		"screen_hash":        snap.ScreenHash,
		"cursor":             map[string]any{"x": snap.Cursor.X, "y": snap.Cursor.Y},
		"cols":               snap.Cols,
		"rows":               snap.Rows,
		"term":               snap.Term,
		"cursor_at_end":      snap.CursorAtEnd,
		"has_trailing_space": snap.HasTrailingSpace,
		"raw_tail":           l.redactText(snap.RawTail),
		"captured_at":        snap.CapturedAt,
	}
	if snap.PromptDetected != nil {
		data["prompt_detected"] = map[string]any{
			"prompt_id":  snap.PromptDetected.PromptID,
			"input_type": snap.PromptDetected.InputType,
			"is_idle":    snap.PromptDetected.IsIdle,
			"kv_data":    l.redactValue(snap.PromptDetected.KVData),
		}
	}
	return data
}

// LogEvent logs an arbitrary named event.
func (l *SessionLogger) LogEvent(event string, data map[string]any) error {
	return l.writeEvent(event, data)
}

// LogWire logs a raw wire chunk when wire-mode recording is enabled.
// direction is "send" or "recv".
func (l *SessionLogger) LogWire(direction, text string) error {
	if l.controlChannelMode != ModeWire {
		return nil
	}
	text = l.redactText(text)
	return l.writeEvent("wire_"+direction, map[string]any{
		"text":      text,
		"bytes_b64": base64.StdEncoding.EncodeToString([]byte(text)),
	})
}

// LogControl logs a decoded control frame when wire-mode recording is
// enabled. direction is "send" or "recv".
func (l *SessionLogger) LogControl(direction string, control map[string]any) error {
	if l.controlChannelMode != ModeWire {
		return nil
	}
	return l.writeEvent("control_"+direction, map[string]any{"control": control})
}

// SetContext sets metadata context attached to subsequent log entries.
func (l *SessionLogger) SetContext(context map[string]string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.context = make(map[string]string, len(context))
	for k, v := range context {
		l.context[k] = v
	}
}

// ClearContext clears the metadata context.
func (l *SessionLogger) ClearContext() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.context = nil
}

// Flush manually flushes buffered log entries.
func (l *SessionLogger) Flush() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.flushLocked()
}

func (l *SessionLogger) writeEvent(event string, data map[string]any) error {
	l.mu.Lock()
	defer l.mu.Unlock()

	if l.maxBytes > 0 && l.bytesWritten >= l.maxBytes {
		if !l.quotaWarned {
			l.quotaWarned = true
			l.logger.Warn("session_logger_quota_reached — further writes suppressed")
		}
		return nil
	}

	record := recording.Event{
		"ts":    float64(time.Now().UnixNano()) / 1e9,
		"event": event,
		"data":  data,
	}
	if l.sessionID != "" {
		record["session_id"] = l.sessionID
	}
	if len(l.context) > 0 {
		ctx := make(map[string]string, len(l.context))
		for k, v := range l.context {
			ctx[k] = v
		}
		record["ctx"] = ctx
	}

	l.buffer = append(l.buffer, record)
	line, err := json.Marshal(record)
	if err != nil {
		return err
	}
	l.bytesWritten += len(line) + 1

	if len(l.buffer) >= l.batchSize {
		return l.flushLocked()
	}
	return nil
}

// flushLocked flushes the buffer while holding l.mu. The buffer is cleared
// only after AppendEvents succeeds, so a failed flush keeps the batch for the
// next attempt.
func (l *SessionLogger) flushLocked() error {
	if len(l.buffer) == 0 || l.sessionID == "" {
		return nil
	}
	batch := make([]recording.Event, len(l.buffer))
	copy(batch, l.buffer)
	if err := l.store.AppendEvents(l.sessionID, batch); err != nil {
		return err
	}
	l.buffer = l.buffer[:0]
	return nil
}

// periodicFlush retries flushes on a fixed cadence until stopped. A transient
// store failure must not kill the flusher: the batch stays buffered and the
// next tick retries it.
func (l *SessionLogger) periodicFlush(stop <-chan struct{}, done chan<- struct{}) {
	defer close(done)
	ticker := time.NewTicker(l.flushInterval)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			if err := l.Flush(); err != nil {
				l.mu.Lock()
				sessionID := l.sessionID
				l.mu.Unlock()
				l.logger.Warn("session_logger_periodic_flush_failed",
					"session_id", sessionID, "error", err.Error())
			}
		}
	}
}

func (l *SessionLogger) redactText(value string) string {
	return redaction.RedactText(value, l.redactor)
}

func (l *SessionLogger) redactValue(value any) any {
	switch v := value.(type) {
	case string:
		return l.redactText(v)
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = l.redactValue(item)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(v))
		for k, item := range v {
			out[k] = l.redactValue(item)
		}
		return out
	default:
		return value
	}
}
