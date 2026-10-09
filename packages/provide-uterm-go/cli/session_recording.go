//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"log/slog"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode"

	ptel "github.com/provide-io/provide-telemetry/go"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/annotation"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/redaction"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/sessionlogger"
)

// Hosted-session recording: the port of HostedSessionRuntime's recording
// lifecycle (runtime.py _start_recording, _stop_recording, flush_recording and
// the _log_* helpers).
//
// The reference runs its own worker socket loop and logs from inside it. Here
// the socket belongs to bridge.TermBridge, so the session reaches the same
// points through a bridge.Observer: a recording is opened before each dial and
// closed when that attempt ends (the reference's recording "belongs to the
// connection", while the connector belongs to the session), every frame the
// bridge writes is logged after the write as _send_outbound_frame logs it, and
// inbound text is logged raw and then per decoded event as _process_inbound
// does. Connector output reaches the hub as term frames the bridge writes, so
// it is recorded at the same point the reference records poll_messages output.

// passwordPromptRE is the reference's test for "the screen is asking for a
// secret" (runtime.py _log_snapshot): a password or passphrase line ending in a
// colon at the very end of the right-stripped screen.
var passwordPromptRE = regexp.MustCompile(`(?i)(?:password|passphrase)[^\n]*:\s*$`)

// atPasswordPrompt reports whether screen ends at a password prompt. The
// screen is matched as sent, as the reference matches it.
func atPasswordPrompt(text string) bool {
	return passwordPromptRE.MatchString(strings.TrimRightFunc(text, unicode.IsSpace))
}

// recordingRedactor is the reference's _build_recording_redactor: the hub's
// default redaction rules when redact_sensitive is set, none otherwise.
func recordingRedactor(enabled bool) redaction.Redactor {
	if !enabled {
		return nil
	}
	return hub.NewStreamRedactor(hub.DefaultRules()).Redact
}

// sessionRecorder records one hosted session. It lives as long as the
// session's registry entry, across stops and restarts, as the reference's
// runtime does, and so does what the reference keeps on the runtime: the event
// sequence, the password-prompt flag and the two streaming detectors. Each
// worker connection attempt gets its own SessionLogger.
//
// With a detector, each match is recorded as an "annotation" entry carrying
// Annotation.ToDict, while recording only, as the reference does: read-path
// rules over each snapshot's screen as sent, and over streamed terminal
// output with escape sequences removed; send-path rules over each chunk of
// input, masked or not. The streams carry a partial match from one chunk to
// the next, one per direction so input is never joined to output; the
// detector itself is stateless and shared by every session.
type sessionRecorder struct {
	sessionID string
	store     recording.Store
	cfg       serverconfig.RecordingConfig
	logger    *slog.Logger

	detector   *annotation.PatternDetector
	sendStream *annotation.StreamingDetector
	readStream *annotation.StreamingDetector

	mu               sync.Mutex
	enabled          bool
	rec              *sessionlogger.SessionLogger
	eventSeq         int
	atPasswordPrompt bool
}

var _ bridge.Observer = (*sessionRecorder)(nil)

// newSessionRecorder builds a session's recorder. detector may be nil, for a
// session that records without annotating.
func newSessionRecorder(
	sessionID string,
	store recording.Store,
	cfg serverconfig.RecordingConfig,
	detector *annotation.PatternDetector,
	logger *slog.Logger,
) *sessionRecorder {
	if logger == nil {
		logger = ptel.GetLogger(context.Background(), "provide.uterm.server.runtime")
	}
	s := &sessionRecorder{sessionID: sessionID, store: store, cfg: cfg, detector: detector, logger: logger}
	if detector != nil {
		s.sendStream = annotation.NewStreamingDetector(detector, 0)
		s.readStream = annotation.NewStreamingDetector(detector, 0)
	}
	return s
}

// setEnabled records the session's resolved recording flag. It is set each
// time the session starts and read when a connection attempt opens its
// recording, as the reference reads _recording_enabled() in _start_recording.
func (s *sessionRecorder) setEnabled(enabled bool) {
	s.mu.Lock()
	s.enabled = enabled
	s.mu.Unlock()
}

func (s *sessionRecorder) isEnabled() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.enabled
}

// AttemptStarted opens a recording for the connection about to be dialled.
// Port of _start_recording, including both flush knobs.
func (s *sessionRecorder) AttemptStarted() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec != nil || !s.enabled {
		return
	}
	rec := sessionlogger.New(s.store, sessionlogger.Options{
		MaxBytes:           int(s.cfg.MaxBytes),
		ControlChannelMode: sessionlogger.ControlChannelMode(s.cfg.ControlChannelMode),
		Redactor:           recordingRedactor(s.cfg.RedactSensitive),
		FlushInterval:      time.Duration(s.cfg.FlushIntervalS * float64(time.Second)),
		BatchSize:          s.cfg.FlushBatchSize,
		Logger:             s.logger,
	})
	if err := rec.Start(s.sessionID); err != nil {
		// The session runs either way; it just goes unrecorded.
		s.logger.Warn("hosted_session_recording_start_failed", "session_id", s.sessionID, "error", err.Error())
		return
	}
	s.rec = rec
}

// AttemptEnded closes the attempt's recording, logging the dial failure first
// when there was one (the reference logs runtime_error before its finally
// block stops the recording).
func (s *sessionRecorder) AttemptEnded(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec == nil {
		return
	}
	if err != nil {
		s.logEvent("runtime_error", map[string]any{"error": err.Error()})
	}
	s.check(s.rec.Stop())
	s.rec = nil
}

// Connected logs runtime_started, as _bridge_session does once the socket is up.
func (s *sessionRecorder) Connected() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.logEvent("runtime_started", map[string]any{"session_id": s.sessionID})
}

// FrameSent is _send_outbound_frame's logging half: the wire text, the control
// frame (anything but terminal output), and a snapshot as a "read".
func (s *sessionRecorder) FrameSent(payload string, frame map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	mtype, _ := frame["type"].(string)
	if s.rec != nil {
		s.check(s.rec.LogWire("send", payload))
		if mtype != "term" {
			s.check(s.rec.LogControl("send", frame))
		}
	}
	switch mtype {
	case "snapshot":
		s.logSnapshot(frame)
	case "term":
		data, _ := frame["data"].(string)
		s.scanOutput(data)
	}
}

// logSnapshot is _log_snapshot: the password-prompt flag is updated whether or
// not anything is recorded, then the snapshot is logged as a "read" and the
// event sequence advanced. Caller holds s.mu.
func (s *sessionRecorder) logSnapshot(frame map[string]any) {
	text, _ := frame["screen"].(string)
	s.atPasswordPrompt = atPasswordPrompt(text)
	if s.rec == nil {
		return
	}
	s.check(s.rec.LogScreenFrame(frame, screen.EncodeCP437(text)))
	s.eventSeq++
	if s.detector != nil {
		s.annotate(s.detector.Detect("read", text, s.eventSeq))
	}
}

// scanOutput is _scan_output: read-path rules over streamed output, escape
// sequences removed, only while recording. Caller holds s.mu.
func (s *sessionRecorder) scanOutput(data string) {
	if s.rec == nil || s.readStream == nil || data == "" {
		return
	}
	s.annotate(s.readStream.Detect("read", screen.StripANSI(data), s.eventSeq))
}

// annotate records each match as an annotation entry. Caller holds s.mu.
func (s *sessionRecorder) annotate(anns []annotation.Annotation) {
	for _, a := range anns {
		s.logEvent("annotation", a.ToDict())
	}
}

// WireReceived is _log_wire_recv.
func (s *sessionRecorder) WireReceived(text string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec != nil {
		s.check(s.rec.LogWire("recv", text))
	}
}

// ControlReceived is _log_control_recv.
func (s *sessionRecorder) ControlReceived(msg map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec != nil {
		s.check(s.rec.LogControl("recv", msg))
	}
}

// InputReceived is _log_send: masked, keeping only its CP437 length, while the
// last snapshot sent ended at a password prompt.
func (s *sessionRecorder) InputReceived(data string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec == nil {
		return
	}
	if s.atPasswordPrompt {
		s.check(s.rec.LogSendMasked(len(screen.EncodeCP437(data))))
	} else {
		s.check(s.rec.LogSend(data))
	}
	s.eventSeq++
	if s.sendStream != nil {
		s.annotate(s.sendStream.Detect("send", data, s.eventSeq))
	}
}

// recordAnnotation writes an operator annotation into the open recording, if
// there is one, returning a failed write.
func (s *sessionRecorder) recordAnnotation(data map[string]any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec == nil {
		return nil
	}
	return s.rec.LogEvent("annotation", data)
}

// flush is flush_recording: write out whatever the open recording has
// buffered, so a reader sees it now rather than at the next interval.
func (s *sessionRecorder) flush() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec != nil {
		s.check(s.rec.Flush())
	}
}

// logEvent is _log_event. Caller holds s.mu.
func (s *sessionRecorder) logEvent(event string, data map[string]any) {
	if s.rec != nil {
		s.check(s.rec.LogEvent(event, data))
	}
}

// check logs a recording write that failed. The reference lets such an error
// end the connection attempt; a recording is not worth a session here, so it
// is logged and the session carries on.
func (s *sessionRecorder) check(err error) {
	if err != nil {
		s.logger.Warn("hosted_session_recording_write_failed", "session_id", s.sessionID, "error", err.Error())
	}
}

// SetRecording wires the store hosted sessions record into — the same store
// the recording routes read, so what a session writes is what they serve —
// and the detector every session annotates its recording with (nil: none). A
// registry without a store records nothing. Safe to call once, at server
// boot, before anything is started.
func (r *SessionRegistryImpl) SetRecording(store recording.Store, detector *annotation.PatternDetector) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.recording = store
	r.detector = detector
}

// recorderFor returns the session's recorder, building it on first use, with
// its enabled flag refreshed from the definition; nil when no store is wired.
// Caller holds r.mu.
func (r *SessionRegistryImpl) recorderFor(e *sessionEntry) *sessionRecorder {
	if r.recording == nil {
		return nil
	}
	if e.recorder == nil {
		e.recorder = newSessionRecorder(e.def.SessionID, r.recording, r.recCfg, r.detector, nil)
	}
	e.recorder.setEnabled(r.recordingEnabled(e.def))
	return e.recorder
}

// FlushRecording flushes a session's open recording, if it has one. Port of
// the registry's _flush_runtime_recording, which the recording routes call
// before they read.
func (r *SessionRegistryImpl) FlushRecording(id string) {
	r.mu.Lock()
	var rec *sessionRecorder
	if e, ok := r.entries[id]; ok {
		rec = e.recorder
	}
	r.mu.Unlock()
	if rec != nil {
		rec.flush()
	}
}
