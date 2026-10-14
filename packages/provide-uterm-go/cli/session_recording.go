//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
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
// A screen is recorded when one is sent, and also, unsent, when
// recording.screen_interval_s asks for it, once that output settles
// (RecordSettledScreen, on the bridge's settle tick), so a session no viewer
// asked a screen of is still recorded as it looked.

// passwordPromptRE is the reference's test for "the screen is asking for a
// secret" (runtime.py _log_snapshot): a password or passphrase line ending in a
// colon at the very end of the right-stripped screen.
var passwordPromptRE = regexp.MustCompile(`(?i)(?:password|passphrase)[^\n]*:\s*$`)

// atPasswordPrompt reports whether text, a screen with its escape sequences
// already removed (see logSnapshot), ends at a password prompt.
func atPasswordPrompt(text string) bool {
	return passwordPromptRE.MatchString(strings.TrimRightFunc(text, unicode.IsSpace))
}

// maxReadAnnotationKeys is _MAX_READ_ANNOTATION_KEYS: the bound on each of a
// recording's sets of read-path annotation keys (readAnnotationKeys,
// snapshotOnlyKeys). Past it the set is cleared and starts over: simple, and
// the worst case is one repeated (or, for the snapshot-only set, one extra)
// annotation more than 1024 distinct read-path matches after the first.
const maxReadAnnotationKeys = 1024

// addBounded is _add_bounded: key added to keys, which is cleared first when
// it holds maxReadAnnotationKeys.
func addBounded(keys map[string]struct{}, key string) {
	if len(keys) >= maxReadAnnotationKeys {
		clear(keys)
	}
	keys[key] = struct{}{}
}

// readAnnotationKey is _read_annotation_key: a read-path annotation's identity
// for stream/snapshot dedupe, the detector's MatchKey -- SHA-256 of rule id +
// NUL + full matched text (annotation.AnnotationMatchKey). It names the rule
// AND the exact text matched, so two different AWS keys, which share a label
// and a description (credential rules never embed the match), are two keys;
// and it is a digest, so the dedupe sets never hold a raw secret. It is not
// part of the recorded annotation (ToDict omits it).
func readAnnotationKey(a annotation.Annotation) string {
	return a.MatchKey
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
// mu: the recording's SessionLogger writes through its own writer, which hands
// full batches to a background goroutine (see batchWriter). Opening and
// closing a recording do their store I/O on the bridge's run goroutine,
// between connections, outside mu, as the reference awaits them in its run
// loop.
// recordingLog is the part of *sessionlogger.SessionLogger the recorder writes
// through. An interface only so a test can fail a single write of a live
// recording: in production every write lands on a batchWriter that queues it
// and never fails, so these error arms are otherwise reached only by a store
// or encoder fault the recorder does not control.
type recordingLog interface {
	LogWire(direction, text string) error
	LogControl(direction string, control map[string]any) error
	LogEvent(event string, data map[string]any) error
	LogScreenFrame(frame map[string]any, raw []byte) error
	LogSend(keys string) error
	LogSendMasked(byteCount int) error
	QuotaReached() bool
	Flush() error
	Stop() error
}

type sessionRecorder struct {
	sessionID string
	// instance names this recorder's recordings to the store. A session id
	// outlives the session it names -- a PAM capture id is a pid, and pids are
	// reused -- so a new connection continues the recording only under the
	// same instance, and a later session reusing the id starts its own. Port
	// of HostedSessionRuntime._recording_instance.
	instance string
	store    recording.Store
	cfg      serverconfig.RecordingConfig
	logger   *slog.Logger
	// retired is set when the session was deleted and its recording retired
	// before this recorder had stopped (the delete's stop timed out): every
	// write still on its way is dropped rather than reopening the retired
	// id's file, which by then may be the next holder's. See retire.
	retired atomic.Bool

	detector   *annotation.PatternDetector
	sendStream *annotation.StreamingDetector
	readStream *annotation.StreamingDetector

	mu      sync.Mutex
	enabled bool
	rec     recordingLog
	// writer is rec's batchWriter, set and cleared with it: a new recording
	// starts with nothing queued and no failure.
	writer *batchWriter
	// unclosed is the last recording, and its writer, when closing it failed:
	// its unwritten entries are still queued for that recording alone. The
	// next attempt's start retries the close before it opens anything (see
	// AttemptStarted).
	unclosed         recordingLog
	unclosedWriter   *batchWriter
	eventSeq         int
	atPasswordPrompt bool
	// escapeCarry is _escape_carry: an escape sequence the last streamed chunk
	// ended inside of, held back and prepended to the next chunk before
	// StripANSI (see scanOutput).
	escapeCarry string
	// Read-path rules run twice over the same output -- once over each
	// streamed term chunk, once over each snapshot screen. Dedupe between the
	// two is keyed by readAnnotationKey (a digest; never the raw text). THE
	// RULE (runtime.py HostedSessionRuntime.__init__, mirrored exactly):
	//   - readAnnotationKeys (_read_annotation_keys): every key recorded in
	//     this recording, by either path.
	//   - snapshotOnlyKeys (_snapshot_only_keys): keys the SNAPSHOT path
	//     recorded since the last processed stream chunk.
	//   Snapshot match: key in readAnnotationKeys -> skip. Otherwise record it
	//   and add the key to BOTH sets.
	//   Stream chunk (non-empty, while recording): for each match, key in
	//   snapshotOnlyKeys -> remove it and skip (the snapshot already recorded
	//   this occurrence). Otherwise record it and add the key to
	//   readAnnotationKeys. AFTER the chunk's matches, snapshotOnlyKeys is
	//   CLEARED: a pending key dedupes only the one stream chunk right after
	//   the snapshot(s).
	// So stream-then-snapshot records once, snapshot-then-next-chunk records
	// once, a run of identical snapshots records once, and a command streamed
	// twice is annotated twice; a pending key never suppresses a later,
	// genuinely new match. Accepted cost: if the snapshot's text reaches the
	// stream only in a LATER chunk, it is recorded a second time -- a
	// duplicate is preferred over a missed annotation. An EMPTY key (an
	// annotation with no match identity) is never deduped: it is recorded and
	// enters neither set. Both sets are bounded
	// (addBounded), scoped to one recording and reset with it: see
	// AttemptEnded.
	readAnnotationKeys map[string]struct{}
	snapshotOnlyKeys   map[string]struct{}

	// now is the recorder's clock (time.Now; a test's own clock in tests).
	now func() time.Time
	// Output not yet in the recording as a screen: when the first of it came
	// (zero: none), and when the latest did; and the last screen recorded,
	// and when (screenRecorded false: none yet, so the reference's -inf, and
	// the first screen is never held back). Port of the reference's
	// _unrecorded_since, _last_output_at, _last_screen_recorded_at and
	// _last_screen_recorded. They live here rather than on the bridge because
	// the recorder is what knows whether a recording is open and when a
	// screen was last logged, whoever sent it; the bridge only ticks (see
	// RecordSettledScreen). Like the event sequence, they outlive a
	// connection, as they do on the reference's runtime.
	unrecordedSince time.Time
	lastOutputAt    time.Time
	// outputs counts the term frames noted, so RecordSettledScreen can tell
	// whether any came while it built a screen even on a clock too coarse to
	// tell two times apart.
	outputs              uint64
	screenRecorded       bool
	lastScreenRecordedAt time.Time
	lastScreenRecorded   string
}

var _ bridge.Observer = (*sessionRecorder)(nil)

// newRecordingInstance returns a fresh random recording instance name.
func newRecordingInstance() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

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
		sessionID: sessionID, instance: newRecordingInstance(), store: store, cfg: cfg, detector: detector, logger: logger,
		readAnnotationKeys: map[string]struct{}{}, snapshotOnlyKeys: map[string]struct{}{},
		now: time.Now,
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
//
// A recording the last attempt could not close is settled first, whether or
// not this attempt records: its close is retried, writing what it still owes
// into it and then its log_stop, so all of it lands before this recording's
// log_start. If the store refuses it again it is dropped, with a warning
// naming how many entries were lost; it is never written after, or into, the
// recording opened here. Each recording gets a fresh writer, so a failure the
// last one met is not this connection's.
func (s *sessionRecorder) AttemptStarted() {
	s.settle()
	s.mu.Lock()
	skip := s.rec != nil || !s.enabled
	s.mu.Unlock()
	if skip {
		return
	}
	writer := newRetirableBatchWriter(s.store, &s.retired)
	rec := sessionlogger.New(writer, sessionlogger.Options{
		MaxBytes:           int(s.cfg.MaxBytes),
		ControlChannelMode: sessionlogger.ControlChannelMode(s.cfg.ControlChannelMode),
		Redactor:           recordingRedactor(s.cfg.RedactSensitive),
		FlushInterval:      time.Duration(s.cfg.FlushIntervalS * float64(time.Second)),
		BatchSize:          s.cfg.FlushBatchSize,
		Logger:             s.logger,
		Instance:           s.instance,
	})
	if err := rec.Start(s.sessionID); err != nil {
		// The session runs either way; it just goes unrecorded.
		s.logger.Warn("hosted_session_recording_start_failed", "session_id", s.sessionID, "error", err.Error())
		return
	}
	s.mu.Lock()
	s.rec, s.writer = rec, writer
	s.mu.Unlock()
}

// AttemptEnded closes the attempt's recording, logging what ended it first
// when something did (the reference logs runtime_error before its finally
// block stops the recording). A recording that cannot take even that is
// detached all the same, with a warning. The recording is detached under mu
// and stopped outside it: Stop drains the writer and closes the recording in
// the store, on the bridge's run goroutine, after the connection's send and
// receive goroutines are done with it. A Stop that fails leaves the recording
// open in the store with entries still queued; it is kept as unclosed for
// settle: the next attempt's start, or the session's final stop.
func (s *sessionRecorder) AttemptEnded(err error) {
	s.mu.Lock()
	rec, writer := s.rec, s.writer
	if rec == nil {
		s.mu.Unlock()
		return
	}
	if err != nil {
		s.warn(s.logEvent("runtime_error", map[string]any{"error": err.Error()}))
	}
	s.rec, s.writer = nil, nil
	// All belong to the recording that just ended (_stop_recording): the next
	// one annotates what it sees afresh, and does not begin with a stale
	// half-sequence.
	s.escapeCarry = ""
	clear(s.readAnnotationKeys)
	clear(s.snapshotOnlyKeys)
	s.mu.Unlock()
	if err := rec.Stop(); err != nil {
		s.warn(err)
		s.mu.Lock()
		s.unclosed, s.unclosedWriter = rec, writer
		s.mu.Unlock()
	}
}

// settle retries the close of a recording the last attempt could not close,
// once: what it still owes is written into it, then its log_stop. If the store
// refuses again it is dropped, with a hosted_session_recording_dropped warning
// naming how many entries were lost. AttemptStarted settles before it opens
// the next recording; the registry settles when the session stops for good
// (StopSession, DeleteSession, Shutdown), after its bridge has stopped, so a
// recording is never left owing for the recorder's lifetime nor closed
// arbitrarily late by a much later restart. The store I/O runs on the
// caller's goroutine, outside mu: the bridge's run goroutine, or the
// registry's caller once the bridge is gone -- never a send/receive
// goroutine.
// retire marks the recorder retired (see sessionRecorder.retired).
func (s *sessionRecorder) retire() {
	s.retired.Store(true)
}

func (s *sessionRecorder) settle() {
	if s.retired.Load() {
		s.mu.Lock()
		s.unclosed, s.unclosedWriter = nil, nil // its recording is retired: nothing to settle into
		s.mu.Unlock()
		return
	}
	s.mu.Lock()
	unclosed, writer := s.unclosed, s.unclosedWriter
	s.unclosed, s.unclosedWriter = nil, nil
	s.mu.Unlock()
	if unclosed == nil {
		return
	}
	if err := unclosed.Stop(); err != nil {
		s.logger.Warn("hosted_session_recording_dropped", "session_id", s.sessionID,
			"unwritten_entries", writer.unwritten(), "error", err.Error())
	}
}

// failed is the open recording's store failure, if its writer is stuck on
// one: a batch already handed over could not be written. Reported at the next
// observed point, before anything else is logged or acted on, as the write
// that raised ends the reference's method. It is the open recording's alone:
// one that ended unwritten is not this connection's to report. Caller holds
// s.mu.
func (s *sessionRecorder) failed() error {
	if s.writer == nil {
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
		// term frames are the wire text itself, and a snapshot is recorded in
		// full as a "read" event by logSnapshot below, as the reference's
		// _log_wire_send skips both.
		if mtype != "term" && mtype != "snapshot" {
			if err := s.rec.LogControl("send", frame); err != nil {
				return err
			}
		}
	}
	switch mtype {
	case "snapshot":
		return s.logSnapshot(frame)
	case "term":
		s.noteOutput()
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
	return s.logScreen(frame, true)
}

// logScreen is _log_snapshot with its annotate flag. annotate is false only
// for a screen the recording took for itself (RecordSettledScreen): its
// output was already scanned as it streamed, and running the read-path rules
// over the screen too, once an interval, would re-annotate whatever stays on
// it each time the bounded dedupe set is cleared, where before only a
// viewer's read could. The "read" entry is the same either way. Caller holds
// s.mu.
func (s *sessionRecorder) logScreen(frame map[string]any, annotate bool) error {
	raw, _ := frame["screen"].(string)
	// Whoever asked for it, this screen is now in the recording (the
	// reference sets _unrecorded_since, _last_screen_recorded_at and
	// _last_screen_recorded here): a viewer's snapshot settles the output,
	// and starts the interval, as well as RecordSettledScreen does.
	s.unrecordedSince = time.Time{}
	s.screenRecorded = true
	s.lastScreenRecordedAt = s.now()
	s.lastScreenRecorded = raw
	text := screen.StripANSI(raw)
	s.atPasswordPrompt = atPasswordPrompt(text)
	if s.rec == nil {
		return nil
	}
	if err := s.rec.LogScreenFrame(frame, screen.EncodeCP437(raw)); err != nil {
		return err
	}
	s.eventSeq++
	if s.detector == nil || !annotate {
		return nil
	}
	// The snapshot path DEDUPES (rule on sessionRecorder): a match already
	// recorded by the stream or an earlier snapshot is skipped; a new one is
	// recorded and left pending for the next stream chunk.
	for _, a := range s.detector.Detect("read", text, s.eventSeq) {
		if !s.admitSnapshotMatch(readAnnotationKey(a)) {
			continue
		}
		if err := s.logEvent("annotation", a.ToDict()); err != nil {
			return err
		}
	}
	return nil
}

// admitSnapshotMatch is the snapshot half of the dedupe rule (see
// sessionRecorder): whether a snapshot match with this key is recorded, the
// key sets updated to say it was. An empty key is never deduped: it is
// recorded and enters neither set. Caller holds s.mu.
func (s *sessionRecorder) admitSnapshotMatch(key string) bool {
	if key == "" {
		return true
	}
	if _, seen := s.readAnnotationKeys[key]; seen {
		return false
	}
	s.rememberReadAnnotation(key)
	addBounded(s.snapshotOnlyKeys, key)
	return true
}

// admitStreamMatch is the stream half: a key a snapshot left pending is
// consumed and not recorded; any other is recorded and remembered. An empty
// key is never deduped: it is recorded and enters neither set. Caller holds
// s.mu.
func (s *sessionRecorder) admitStreamMatch(key string) bool {
	if key == "" {
		return true
	}
	if _, pending := s.snapshotOnlyKeys[key]; pending {
		delete(s.snapshotOnlyKeys, key)
		return false
	}
	s.rememberReadAnnotation(key)
	return true
}

// rememberReadAnnotation is _remember_read_annotation: key added to the bounded
// read-path set, which is cleared first when full. Caller holds s.mu.
func (s *sessionRecorder) rememberReadAnnotation(key string) {
	addBounded(s.readAnnotationKeys, key)
}

// scanOutput is _scan_output: read-path rules over streamed output, escape
// sequences removed, only while recording. An escape sequence split across
// chunks ("...\x1b[1" | "msudo ...") would leave "msudo" behind if each chunk
// were stripped alone, so an unterminated trailing sequence is held back in
// escapeCarry and prepended to the next chunk (screen.SplitIncompleteEscape,
// bounded by screen.MaxEscapeCarry).
//
// The stream path records every match -- a command run twice is annotated
// twice -- EXCEPT a match in the first chunk after a snapshot that already
// recorded it (a key in snapshotOnlyKeys), which it consumes once instead;
// the pending keys are then cleared, matched or not. Every match it records
// is remembered so the snapshot path, which sees the same text again, does
// not record it a second time. Full rule: sessionRecorder. Caller holds s.mu.
func (s *sessionRecorder) scanOutput(data string) error {
	if s.rec == nil || s.readStream == nil || data == "" {
		return nil
	}
	var text string
	text, s.escapeCarry = screen.SplitIncompleteEscape(s.escapeCarry+data, screen.MaxEscapeCarry)
	for _, a := range s.readStream.Detect("read", screen.StripANSI(text), s.eventSeq) {
		if !s.admitStreamMatch(readAnnotationKey(a)) {
			continue
		}
		if err := s.logEvent("annotation", a.ToDict()); err != nil {
			return err
		}
	}
	// Pending snapshot keys expire with the first chunk after the snapshot,
	// matched or not, so they never suppress a later, new match.
	clear(s.snapshotOnlyKeys)
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
	rec, writer := s.rec, s.writer
	s.mu.Unlock()
	if rec == nil {
		return nil
	}
	// Flush cannot fail here: it only hands the buffer to the writer, whose
	// AppendEvents queues it and never fails. drain is where a write fails.
	_ = rec.Flush()
	return writer.drain()
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
