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
// does. A recording write that fails is returned to the bridge, which ends the
// connection on it, as the exception does in the reference; the attempt's end
// then records it as runtime_error and closes the recording. Connector output reaches the hub as term frames the bridge writes, so
// it is recorded at the same point the reference records poll_messages output.

// passwordPromptRE is the reference's test for "the screen is asking for a
// secret" (runtime.py _log_snapshot): a password or passphrase line ending in a
// colon at the very end of the right-stripped screen.
var passwordPromptRE = regexp.MustCompile(`(?i)(?:password|passphrase)[^\n]*:\s*$`)

// atPasswordPrompt reports whether text, a screen with its escape sequences
// already removed (see logSnapshot), ends at a password prompt.
func atPasswordPrompt(text string) bool {
	return passwordPromptRE.MatchString(strings.TrimRightFunc(text, unicode.IsSpace))
}

// incompleteEscapeTailRE is runtime_helpers.py _INCOMPLETE_ESCAPE_TAIL, anchored
// as its fullmatch is: an escape sequence cut off at the end of a streamed
// chunk -- a bare ESC, or a CSI (ESC [) whose parameter/intermediate bytes have
// not yet reached a final byte. The grammar is exactly that of StripANSI's
// escape pattern (screen.py _ANSI_ESCAPE_RE): anything it would strip once
// complete, and nothing else, is held back.
var incompleteEscapeTailRE = regexp.MustCompile(`\A\x1b(?:\[[0-?]*[ -/]*)?\z`)

// maxEscapeCarry is _MAX_ESCAPE_CARRY: the longest tail held back between
// chunks, ESC included. A real sequence is a handful of bytes; past this the
// "sequence" is not one (or is hostile), and it is released into the text
// rather than carried forever.
const maxEscapeCarry = 64

// splitIncompleteEscape is _split_incomplete_escape: text split into
// (complete, carry), carry being an unterminated trailing escape sequence the
// caller prepends to the next chunk. StripANSI sees one chunk at a time, so
// "\x1b[1" + "msudo" would strip to "\x1b[1" (no final byte, left alone) and
// "msudo", and \bsudo\b would miss. Only the LAST ESC can start an
// unterminated tail (CSI parameter bytes never include ESC). A tail longer
// than maxEscapeCarry is not carried. The reference measures that in code
// points and this in bytes, which agree on every tail the pattern accepts:
// it accepts ASCII only.
func splitIncompleteEscape(text string) (complete, carry string) {
	start := strings.LastIndexByte(text, 0x1b)
	if start < 0 || len(text)-start > maxEscapeCarry || !incompleteEscapeTailRE.MatchString(text[start:]) {
		return text, ""
	}
	return text[:start], text[start:]
}

// maxReadAnnotationKeys is _MAX_READ_ANNOTATION_KEYS: the bound on a
// recording's set of read-path annotation keys. Past it the set is cleared
// and starts over: simple, and the worst case is that a snapshot repeats an
// annotation the stream already recorded once more than 1024 distinct
// read-path matches ago.
const maxReadAnnotationKeys = 1024

// readAnnotationKey is _read_annotation_key: a read-path annotation's identity
// for snapshot dedupe, rule + matched text. Annotation carries no rule id;
// Label is the rule's category label and Description the rule's own template
// formatted with the match (cut at 80 characters), so the pair names the rule
// and, for every rule whose template embeds {match}, the matched text.
// Credential rules deliberately never embed the match, so for them the key is
// the rule alone.
func readAnnotationKey(a annotation.Annotation) string {
	return a.Label + "\x00" + a.Description
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
// rules over each snapshot's screen and over streamed terminal output, both
// with escape sequences removed; send-path rules over each chunk of input,
// masked or not. The streams carry a partial match from one chunk to the
// next, one per direction so input is never joined to output; the detector
// itself is stateless and shared by every session.
//
// Store writes never happen on the bridge's send/receive goroutines nor under
// mu: the recording's SessionLogger writes through writer, which hands full
// batches to a background goroutine (see batchWriter). Opening and closing a
// recording do their store I/O on the bridge's run goroutine, between
// connections, outside mu, as the reference awaits them in its run loop.
type sessionRecorder struct {
	sessionID string
	writer    *batchWriter
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
	// escapeCarry is _escape_carry: an escape sequence the last streamed chunk
	// ended inside of, held back and prepended to the next chunk before
	// StripANSI (see scanOutput).
	escapeCarry string
	// readAnnotationKeys is _read_annotation_keys. Read-path rules run twice
	// over the same output -- once over each streamed term chunk, once over
	// each snapshot screen -- so every read-path annotation recorded is
	// remembered here (by readAnnotationKey) and the snapshot path skips one
	// already in it. Both are scoped to one recording and reset with it: see
	// AttemptEnded.
	readAnnotationKeys map[string]struct{}
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
	s := &sessionRecorder{
		sessionID: sessionID, writer: newBatchWriter(store), cfg: cfg, detector: detector, logger: logger,
		readAnnotationKeys: map[string]struct{}{},
	}
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
// Port of _start_recording, including both flush knobs. The store calls Start
// makes run outside mu; the bridge calls this from its run goroutine before
// it dials, so nothing else observes the attempt until it returns.
func (s *sessionRecorder) AttemptStarted() {
	s.mu.Lock()
	skip := s.rec != nil || !s.enabled
	s.mu.Unlock()
	if skip {
		return
	}
	rec := sessionlogger.New(s.writer, sessionlogger.Options{
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
	s.mu.Lock()
	s.rec = rec
	s.mu.Unlock()
}

// AttemptEnded closes the attempt's recording, logging what ended it first
// when something did (the reference logs runtime_error before its finally
// block stops the recording). A recording that cannot take even that is
// closed all the same; the warning is all that is left to say. The recording
// is detached under mu and stopped outside it: Stop drains the writer and
// closes the recording in the store, on the bridge's run goroutine, after the
// connection's send and receive goroutines are done with it.
func (s *sessionRecorder) AttemptEnded(err error) {
	s.mu.Lock()
	rec := s.rec
	if rec == nil {
		s.mu.Unlock()
		return
	}
	if err != nil {
		s.warn(s.logEvent("runtime_error", map[string]any{"error": err.Error()}))
	}
	s.rec = nil
	// Both belong to the recording that just ended (_stop_recording): the next
	// one annotates what it sees afresh, and does not begin with a stale
	// half-sequence.
	s.escapeCarry = ""
	clear(s.readAnnotationKeys)
	s.mu.Unlock()
	s.warn(rec.Stop())
}

// failed is the open recording's store failure, if its writer is stuck on
// one: a batch already handed over could not be written. Reported at the next
// observed point, before anything else is logged or acted on, as the write
// that raised ends the reference's method. Caller holds s.mu.
func (s *sessionRecorder) failed() error {
	if s.rec == nil {
		return nil
	}
	return s.writer.failure()
}

// Connected logs runtime_started, as _bridge_session does once the socket is up.
func (s *sessionRecorder) Connected() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.failed(); err != nil {
		return err
	}
	return s.logEvent("runtime_started", map[string]any{"session_id": s.sessionID})
}

// FrameSent is _send_outbound_frame's logging half: the wire text, the control
// frame (anything but terminal output), and a snapshot as a "read". The first
// write that fails is returned, and nothing after it is logged, as the
// reference's first raising call ends the method.
func (s *sessionRecorder) FrameSent(payload string, frame map[string]any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.failed(); err != nil {
		return err
	}
	mtype, _ := frame["type"].(string)
	if s.rec != nil {
		if err := s.rec.LogWire("send", payload); err != nil {
			return err
		}
		if mtype != "term" {
			if err := s.rec.LogControl("send", frame); err != nil {
				return err
			}
		}
	}
	switch mtype {
	case "snapshot":
		return s.logSnapshot(frame)
	case "term":
		data, _ := frame["data"].(string)
		return s.scanOutput(data)
	}
	return nil
}

// logSnapshot is _log_snapshot: the password-prompt flag is updated whether or
// not anything is recorded, then the snapshot is logged as a "read" and the
// event sequence advanced. The screen is read as text for both the prompt
// check and the read-path rules: a rendered screen carries SGR codes and ends
// each row with a reset, which hides a trailing "Password:" from the prompt
// check and splits a styled match from the rules. The "read" entry keeps the
// screen as sent. Caller holds s.mu.
func (s *sessionRecorder) logSnapshot(frame map[string]any) error {
	raw, _ := frame["screen"].(string)
	text := screen.StripANSI(raw)
	s.atPasswordPrompt = atPasswordPrompt(text)
	if s.rec == nil {
		return nil
	}
	if err := s.rec.LogScreenFrame(frame, screen.EncodeCP437(raw)); err != nil {
		return err
	}
	s.eventSeq++
	if s.detector == nil {
		return nil
	}
	// The snapshot path DEDUPES: a match the stream (or an earlier snapshot of
	// the same screen) already recorded is skipped, so neither a
	// stream+snapshot pair nor a run of identical snapshots records it twice.
	for _, a := range s.detector.Detect("read", text, s.eventSeq) {
		key := readAnnotationKey(a)
		if _, seen := s.readAnnotationKeys[key]; seen {
			continue
		}
		s.rememberReadAnnotation(key)
		if err := s.logEvent("annotation", a.ToDict()); err != nil {
			return err
		}
	}
	return nil
}

// rememberReadAnnotation is _remember_read_annotation: key added to the bounded
// read-path set, which is cleared first when full. Caller holds s.mu.
func (s *sessionRecorder) rememberReadAnnotation(key string) {
	if len(s.readAnnotationKeys) >= maxReadAnnotationKeys {
		clear(s.readAnnotationKeys)
	}
	s.readAnnotationKeys[key] = struct{}{}
}

// scanOutput is _scan_output: read-path rules over streamed output, escape
// sequences removed, only while recording. An escape sequence split across
// chunks ("...\x1b[1" | "msudo ...") would leave "msudo" behind if each chunk
// were stripped alone, so an unterminated trailing sequence is held back in
// escapeCarry and prepended to the next chunk (bounded by maxEscapeCarry).
// The stream path NEVER suppresses a match -- a command run twice is
// annotated twice -- but remembers each one so the snapshot path, which sees
// the same text again, does not record it a second time. Caller holds s.mu.
func (s *sessionRecorder) scanOutput(data string) error {
	if s.rec == nil || s.readStream == nil || data == "" {
		return nil
	}
	var text string
	text, s.escapeCarry = splitIncompleteEscape(s.escapeCarry + data)
	for _, a := range s.readStream.Detect("read", screen.StripANSI(text), s.eventSeq) {
		s.rememberReadAnnotation(readAnnotationKey(a))
		if err := s.logEvent("annotation", a.ToDict()); err != nil {
			return err
		}
	}
	return nil
}

// annotate records each match as an annotation entry. Caller holds s.mu.
func (s *sessionRecorder) annotate(anns []annotation.Annotation) error {
	for _, a := range anns {
		if err := s.logEvent("annotation", a.ToDict()); err != nil {
			return err
		}
	}
	return nil
}

// WireReceived is _log_wire_recv.
func (s *sessionRecorder) WireReceived(text string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec == nil {
		return nil
	}
	if err := s.writer.failure(); err != nil {
		return err
	}
	return s.rec.LogWire("recv", text)
}

// ControlReceived is _log_control_recv.
func (s *sessionRecorder) ControlReceived(msg map[string]any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec == nil {
		return nil
	}
	if err := s.writer.failure(); err != nil {
		return err
	}
	return s.rec.LogControl("recv", msg)
}

// InputReceived is _log_send: masked, keeping only its CP437 length, while the
// last snapshot sent ended at a password prompt.
func (s *sessionRecorder) InputReceived(data string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec == nil {
		return nil
	}
	if err := s.writer.failure(); err != nil {
		return err
	}
	var err error
	if s.atPasswordPrompt {
		err = s.rec.LogSendMasked(len(screen.EncodeCP437(data)))
	} else {
		err = s.rec.LogSend(data)
	}
	if err != nil {
		return err
	}
	s.eventSeq++
	if s.sendStream != nil {
		return s.annotate(s.sendStream.Detect("send", data, s.eventSeq))
	}
	return nil
}

// recordAnnotation writes an operator annotation into the open recording, if
// there is one, returning a failed write: the store failure the recording is
// stuck on, if any, as the reference's annotate_session raises its logger's.
func (s *sessionRecorder) recordAnnotation(data map[string]any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.failed(); err != nil {
		return err
	}
	return s.logEvent("annotation", data)
}

// flush is flush_recording: write out whatever the open recording has
// buffered, so a reader sees it now rather than at the next interval. The
// buffer is handed to the writer under mu and the store write is waited for
// outside it, so a slow store holds up this caller and nothing on the bridge.
func (s *sessionRecorder) flush() error {
	s.mu.Lock()
	rec := s.rec
	s.mu.Unlock()
	if rec == nil {
		return nil
	}
	// Flush cannot fail here: it only hands the buffer to the writer, whose
	// AppendEvents queues it and never fails. drain is where a write fails.
	_ = rec.Flush()
	return s.writer.drain()
}

// logEvent is _log_event. Caller holds s.mu.
func (s *sessionRecorder) logEvent(event string, data map[string]any) error {
	if s.rec == nil {
		return nil
	}
	return s.rec.LogEvent(event, data)
}

// warn logs a recording failure there is no one left to report to.
func (s *sessionRecorder) warn(err error) {
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

// FlushRecording flushes a session's open recording, if it has one, returning
// a failed write. Port of the registry's _flush_runtime_recording, which the
// recording routes call before they read.
func (r *SessionRegistryImpl) FlushRecording(id string) error {
	r.mu.Lock()
	var rec *sessionRecorder
	if e, ok := r.entries[id]; ok {
		rec = e.recorder
	}
	r.mu.Unlock()
	if rec == nil {
		return nil
	}
	return rec.flush()
}
