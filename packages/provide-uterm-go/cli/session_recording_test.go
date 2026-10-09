//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/annotation"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// The hosted-session recording lifecycle, against the reference's
// HostedSessionRuntime (runtime.py): what a recorded session writes, and when.

// testRecordingConfig is the default recording section with the periodic
// flusher pushed out of the way, so a test sees exactly what Stop or a full
// batch flushed.
func testRecordingConfig() serverconfig.RecordingConfig {
	cfg := serverconfig.DefaultServerConfig().Recording
	cfg.FlushIntervalS = 3600
	return cfg
}

func newTestRecorder(t *testing.T, cfg serverconfig.RecordingConfig) (*sessionRecorder, *recording.InMemoryStore) {
	t.Helper()
	store := recording.NewInMemoryStore()
	rec := newSessionRecorder("s1", store, cfg, nil, nil)
	rec.setEnabled(true)
	return rec, store
}

// entriesOf reads back what the store holds for id, through JSON as a reader
// would, with the wall-clock timestamps dropped.
func entriesOf(t *testing.T, store recording.Store, id, event string) []map[string]any {
	t.Helper()
	got, err := store.GetEntries(id, recording.Query{Limit: 500, Event: event})
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	out := []map[string]any{}
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	for _, e := range out {
		if _, ok := e["ts"].(float64); !ok {
			t.Fatalf("entry without a numeric ts: %v", e)
		}
		delete(e, "ts")
	}
	return out
}

func entries(t *testing.T, store recording.Store, event string) []map[string]any {
	t.Helper()
	return entriesOf(t, store, "s1", event)
}

func eventNames(t *testing.T, store recording.Store) []string {
	t.Helper()
	var names []string
	for _, e := range entries(t, store, "") {
		names = append(names, e["event"].(string))
	}
	return names
}

func snapshotFrame(screen string) map[string]any {
	return map[string]any{"type": "snapshot", "screen": screen, "ts": 1.5}
}

// sendFrame reports an outbound frame the way the bridge does: after the
// write, with the payload encoded as the bridge encodes it.
func sendFrame(t *testing.T, rec *sessionRecorder, frame map[string]any) {
	t.Helper()
	if frame["type"] == "term" {
		data, _ := frame["data"].(string)
		must(t, rec.FrameSent(controlchannel.EncodeTerminalData(data), frame))
		return
	}
	payload, err := controlchannel.EncodeControlFrame(frame)
	if err != nil {
		t.Fatal(err)
	}
	must(t, rec.FrameSent(payload, frame))
}

// --- the CPython corpus ----------------------------------------------------

// testdata/session_recording_golden.json is what a real HostedSessionRuntime
// records when its own logging methods are driven through a fixed script
// (regenerate with testdata/gen_session_recording_golden.py from the
// repository root). The script is in the corpus, so this replays exactly the
// reference's steps through the recorder's Observer methods.
type sessionRecordingGolden struct {
	SessionID       string                      `json:"session_id"`
	Script          [][]any                     `json:"script"`
	AnnotatedScript [][]any                     `json:"annotated_script"`
	Configs         map[string]map[string]any   `json:"configs"`
	Recorded        map[string][]map[string]any `json:"recorded"`
}

func loadSessionRecordingGolden(t *testing.T) sessionRecordingGolden {
	t.Helper()
	raw, err := os.ReadFile("testdata/session_recording_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g sessionRecordingGolden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

// replayScript drives one recorder through a corpus script, the way the
// bridge would report each step, and returns what it recorded.
func replayScript(t *testing.T, g sessionRecordingGolden, script [][]any, rec *sessionRecorder, store *recording.InMemoryStore) []map[string]any {
	t.Helper()
	rec.AttemptStarted()
	for _, step := range script {
		switch step[0] {
		case "event":
			if step[1] != "runtime_started" {
				t.Fatalf("unexpected script event %v", step[1])
			}
			must(t, rec.Connected())
		case "outbound":
			sendFrame(t, rec, step[1].(map[string]any))
		case "send":
			must(t, rec.InputReceived(step[1].(string)))
		case "wire_recv":
			must(t, rec.WireReceived(step[1].(string)))
		case "control_recv":
			must(t, rec.ControlReceived(step[1].(map[string]any)))
		default:
			t.Fatalf("unknown script step %v", step)
		}
	}
	rec.AttemptEnded(nil)
	got := entriesOf(t, store, g.SessionID, "")
	for _, e := range got {
		if e["event"] == "log_start" {
			e["data"] = map[string]any{"stripped": true}
		}
	}
	return got
}

// decodedWire replaces a wire entry's control-frame text by its header and
// parsed JSON body, after checking bytes_b64 is that text's UTF-8. Go encodes
// a control frame's keys sorted where CPython keeps insertion order, so the
// two wire texts carry the same frame, at the same length, in a different key
// order. The body is parsed on its own rather than through the decoder:
// redaction runs over the whole wire text, header included, so a redacted
// frame's length header no longer matches its body — in the reference too.
func decodedWire(t *testing.T, entry map[string]any) map[string]any {
	t.Helper()
	data := entry["data"].(map[string]any)
	text := data["text"].(string)
	if data["bytes_b64"] != base64.StdEncoding.EncodeToString([]byte(text)) {
		t.Fatalf("bytes_b64 is not the text's bytes: %v", data)
	}
	const headerLen = 11 // DLE STX, eight hex digits, ':'
	if !strings.HasPrefix(text, "\x10\x02") {
		return entry
	}
	var body any
	if err := json.Unmarshal([]byte(text[headerLen:]), &body); err != nil {
		t.Fatalf("wire control body is not JSON: %q", text)
	}
	out := map[string]any{}
	for k, v := range entry {
		out[k] = v
	}
	out["data"] = map[string]any{"header": text[:headerLen], "body": body, "length": len(text)}
	return out
}

func compareRecorded(t *testing.T, name string, got, want []map[string]any) {
	t.Helper()
	if len(got) != len(want) {
		g, _ := json.MarshalIndent(got, "", " ")
		t.Fatalf("%s: %d entries, the reference wrote %d\n got: %s", name, len(got), len(want), g)
	}
	for i := range want {
		g, w := got[i], want[i]
		if w["event"] == "wire_send" || w["event"] == "wire_recv" {
			g, w = decodedWire(t, g), decodedWire(t, w)
		}
		if !reflect.DeepEqual(g, w) {
			gj, _ := json.Marshal(g)
			wj, _ := json.Marshal(w)
			t.Fatalf("%s entry %d diverges from the reference\n got: %s\nwant: %s", name, i, gj, wj)
		}
	}
}

func goldenConfig(t *testing.T, overrides map[string]any) serverconfig.RecordingConfig {
	t.Helper()
	cfg := testRecordingConfig()
	cfg.EnabledByDefault = true
	for k, v := range overrides {
		switch k {
		case "control_channel_mode":
			cfg.ControlChannelMode = v.(string)
		case "redact_sensitive":
			cfg.RedactSensitive = v.(bool)
		default:
			t.Fatalf("unhandled config override %q", k)
		}
	}
	return cfg
}

// Every recording configuration the corpus covers — default exclude mode,
// wire mode, and redaction off — entry for entry.
func TestRecordingMatchesTheReferenceCorpus(t *testing.T) {
	g := loadSessionRecordingGolden(t)
	for name, overrides := range g.Configs {
		t.Run(name, func(t *testing.T) {
			cfg := goldenConfig(t, overrides)
			store := recording.NewInMemoryStore()
			rec := newSessionRecorder(g.SessionID, store, cfg, nil, nil)
			rec.setEnabled(true)
			compareRecorded(t, name, replayScript(t, g, g.Script, rec, store), g.Recorded[name])
		})
	}
}

// --- lifecycle --------------------------------------------------------------

// A failed attempt is recorded too: the reference opens the recording before
// it dials, logs the failure as runtime_error, and closes it.
func TestAFailedAttemptRecordsTheError(t *testing.T) {
	rec, store := newTestRecorder(t, testRecordingConfig())
	rec.AttemptStarted()
	rec.AttemptEnded(errors.New("dial refused"))
	want := []string{"log_start", "runtime_error", "log_stop"}
	if got := eventNames(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
	if got := entries(t, store, "runtime_error")[0]["data"]; !reflect.DeepEqual(got, map[string]any{"error": "dial refused"}) {
		t.Fatalf("runtime_error data = %v", got)
	}
}

// Recording disabled means nothing is written — not even the lifecycle pair.
func TestADisabledRecorderWritesNothing(t *testing.T) {
	rec, store := newTestRecorder(t, testRecordingConfig())
	rec.setEnabled(false)
	rec.AttemptStarted()
	must(t, rec.Connected())
	sendFrame(t, rec, snapshotFrame("Password:"))
	sendFrame(t, rec, map[string]any{"type": "term", "data": "x"})
	must(t, rec.InputReceived("x"))
	must(t, rec.WireReceived("x"))
	must(t, rec.ControlReceived(map[string]any{"type": "snapshot_req"}))
	must(t, rec.flush())
	rec.AttemptEnded(errors.New("boom"))
	if got := entries(t, store, ""); len(got) != 0 {
		t.Fatalf("a disabled recorder wrote %v", got)
	}
}

// The password-prompt flag follows the snapshots sent whether or not they are
// recorded, as the reference sets it before its "no logger" early return.
func TestThePasswordPromptIsTrackedWhileUnrecorded(t *testing.T) {
	rec, store := newTestRecorder(t, testRecordingConfig())
	rec.setEnabled(false)
	sendFrame(t, rec, snapshotFrame("Password: "))
	rec.setEnabled(true)
	rec.AttemptStarted()
	must(t, rec.InputReceived("hunter2"))
	rec.AttemptEnded(nil)
	if got := entries(t, store, "send")[0]["data"].(map[string]any); got["masked"] != true {
		t.Fatalf("input at a prompt seen before the recording opened was not masked: %v", got)
	}
}

// The reference's prompt test, right-stripped and anchored at the end.
func TestPasswordPromptMustEndTheScreen(t *testing.T) {
	for screen, want := range map[string]bool{
		"Password:":                   true,
		"PASSWORD:   \n\n":            true,
		"Password: ok\n$ ":            false,
		"password reset\nuser:":       false,
		"Your passphrase (again) :\t": true,
		"no prompt here":              false,
		// Matched as sent, escape sequences and all, as runtime.py does.
		"Password: \x1b[0m": false,
	} {
		if got := atPasswordPrompt(screen); got != want {
			t.Errorf("atPasswordPrompt(%q) = %t, want %t", screen, got, want)
		}
	}
}

// The flush knobs come from the recording config — the reference passes both
// (and once did not, which went unnoticed because the defaults matched).
func TestRecordingHonoursTheFlushBatchSize(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.FlushBatchSize = 1
	rec, store := newTestRecorder(t, cfg)
	rec.AttemptStarted()
	must(t, rec.Connected())
	if got := eventNames(t, store); !reflect.DeepEqual(got, []string{"log_start", "runtime_started"}) {
		t.Fatalf("a batch of one is flushed at once, got %v", got)
	}
	rec.AttemptEnded(nil)

	rec, store = newTestRecorder(t, testRecordingConfig())
	rec.AttemptStarted()
	must(t, rec.Connected())
	if got := eventNames(t, store); !reflect.DeepEqual(got, []string{"log_start"}) {
		t.Fatalf("a partial batch waits for a flush, got %v", got)
	}
	must(t, rec.flush())
	if got := eventNames(t, store); !reflect.DeepEqual(got, []string{"log_start", "runtime_started"}) {
		t.Fatalf("flush writes the buffered batch, got %v", got)
	}
	rec.AttemptEnded(nil)
}

func TestRecordingHonoursTheFlushInterval(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.FlushIntervalS = 0.01
	rec, store := newTestRecorder(t, cfg)
	rec.AttemptStarted()
	defer rec.AttemptEnded(nil)
	must(t, rec.Connected())
	waitFor(t, "the periodic flush", func() bool { return len(eventNames(t, store)) == 2 })
}

// The byte quota is passed through: a recording at its limit stops growing.
func TestRecordingHonoursMaxBytes(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.MaxBytes = 1
	rec, store := newTestRecorder(t, cfg)
	rec.AttemptStarted()
	must(t, rec.Connected())
	rec.AttemptEnded(nil)
	if got := eventNames(t, store); !reflect.DeepEqual(got, []string{"log_start", "log_stop"}) {
		t.Fatalf("events = %v, want only the lifecycle pair", got)
	}
}

// A store that cannot open a recording leaves the session running unrecorded.
func TestAStoreThatCannotStartLeavesTheSessionUnrecorded(t *testing.T) {
	rec := newSessionRecorder("s1", brokenStore{}, testRecordingConfig(), nil, nil)
	rec.setEnabled(true)
	rec.AttemptStarted()
	must(t, rec.Connected())
	must(t, rec.InputReceived("x"))
	rec.AttemptEnded(nil)
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if rec.rec != nil {
		t.Fatal("a recording the store refused was kept open")
	}
}

type brokenStore struct{ recording.NullStore }

func (brokenStore) StartSession(string, map[string]any) error { return errors.New("disk full") }

// failingWriteStore opens recordings but can neither write to nor close them.
type failingWriteStore struct{ recording.NullStore }

func (failingWriteStore) AppendEvents(string, []recording.Event) error {
	return errors.New("disk full")
}
func (failingWriteStore) EndSession(string) error { return errors.New("disk full") }

// A store failing mid-recording costs the recording, never the session.
func TestARecordingThatCannotBeWrittenDoesNotStopTheSession(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.FlushBatchSize = 1
	rec := newSessionRecorder("s1", failingWriteStore{}, cfg, nil, nil)
	rec.setEnabled(true)
	rec.AttemptStarted()
	_ = rec.Connected() // fails: the store takes no writes
	rec.AttemptEnded(nil)
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if rec.rec != nil {
		t.Fatal("the attempt's recording was not released")
	}
}

// The event sequence lasts the recorder's life, across recordings, as the
// reference keeps it on the runtime rather than on a connection.
func TestTheEventSequenceOutlivesARecording(t *testing.T) {
	rec, _ := newTestRecorder(t, testRecordingConfig())
	rec.AttemptStarted()
	sendFrame(t, rec, snapshotFrame("$ "))
	must(t, rec.InputReceived("a"))
	rec.AttemptEnded(nil)
	rec.AttemptStarted()
	must(t, rec.InputReceived("b"))
	rec.AttemptEnded(nil)
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if rec.eventSeq != 3 {
		t.Fatalf("eventSeq = %d, want 3", rec.eventSeq)
	}
}

// --- the registry -----------------------------------------------------------

func newHubForTest() *hub.TermHub { return hub.NewTermHub(hub.TermHubConfig{}) }

// recordingRegistry is a test registry whose sessions record into a memory
// store.
func recordingRegistry(t *testing.T) (*SessionRegistryImpl, *recording.InMemoryStore) {
	t.Helper()
	r := newTestRegistry(t)
	store := recording.NewInMemoryStore()
	r.SetRecording(store, nil)
	return r, store
}

// recording_available follows recording_enabled once a store is wired, as the
// reference reports it; without one there is nothing to make available. A
// created session's own recording_enabled overrides the default.
func TestRecordingAvailableFollowsEnabledWhenAStoreIsWired(t *testing.T) {
	ctx := context.Background()
	r := newTestRegistry(t)
	st, _ := r.GetSession(ctx, "provide-shell")
	if !st.RecordingEnabled || st.RecordingAvailable {
		t.Fatalf("no store: enabled=%t available=%t, want true/false", st.RecordingEnabled, st.RecordingAvailable)
	}
	r, _ = recordingRegistry(t)
	st, _ = r.GetSession(ctx, "provide-shell")
	if !st.RecordingAvailable {
		t.Fatal("a store is wired and recording is enabled, so a recording is available")
	}
	for id, flag := range map[string]bool{"quiet": false, "loud": true} {
		if _, err := r.CreateSession(ctx, map[string]any{
			"session_id": id, "connector_type": "shell", "recording_enabled": flag,
		}); err != nil {
			t.Fatal(err)
		}
		st, _ = r.GetSession(ctx, id)
		if st.RecordingEnabled != flag || st.RecordingAvailable != flag {
			t.Fatalf("%s: enabled=%t available=%t, want %t", id, st.RecordingEnabled, st.RecordingAvailable, flag)
		}
	}
	r.mu.Lock()
	r.recDeflt = false
	r.mu.Unlock()
	if st, _ = r.GetSession(ctx, "loud"); !st.RecordingEnabled {
		t.Fatal("a session's own recording_enabled outranks the default")
	}
}

// A started session's worker bridge records through the session's recorder,
// whose enabled flag is resolved from the definition — per session first, then
// recording.enabled_by_default — each time the session starts.
func TestStartedSessionRecordsPerItsDefinition(t *testing.T) {
	ctx := context.Background()
	r, store := recordingRegistry(t)
	bctx, cancel := context.WithCancel(ctx)
	defer cancel()
	r.SetHubLink(bctx, newHubForTest(), "http://127.0.0.1:1", "")

	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	rec := r.entries["provide-shell"].recorder
	r.mu.Unlock()
	if rec == nil || !rec.isEnabled() {
		t.Fatal("an enabled session records")
	}
	// The dial to port 1 fails, which the reference records as a failed
	// attempt: the recording exists though no connection ever did.
	waitFor(t, "the failed attempt to be recorded", func() bool {
		got, _ := store.GetEntries("provide-shell", recording.Query{Event: "runtime_error"})
		return len(got) > 0
	})
	if _, err := r.StopSession(ctx, "provide-shell"); err != nil {
		t.Fatal(err)
	}

	// With the default turned off, a session with no flag of its own does not
	// record, and the recorder kept across the restart picks that up.
	r.mu.Lock()
	r.recDeflt = false
	r.mu.Unlock()
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	same := r.entries["provide-shell"].recorder
	r.mu.Unlock()
	if same != rec || rec.isEnabled() {
		t.Fatal("a restarted session keeps its recorder and re-reads the flag")
	}
	_, _ = r.StopSession(ctx, "provide-shell")
}

// Without a store the registry builds no recorder, and flushing is a no-op.
func TestNoStoreNoRecorder(t *testing.T) {
	ctx := context.Background()
	r := newTestRegistry(t)
	bctx, cancel := context.WithCancel(ctx)
	defer cancel()
	r.SetHubLink(bctx, newHubForTest(), "http://127.0.0.1:1", "")
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatal(err)
	}
	defer func() { _, _ = r.StopSession(ctx, "provide-shell") }()
	r.mu.Lock()
	built := r.entries["provide-shell"].recorder
	r.mu.Unlock()
	if built != nil {
		t.Fatal("a registry with no recording store built a recorder")
	}
	must(t, r.FlushRecording("provide-shell"))
	must(t, r.FlushRecording("nosuch"))
}

// writeRecordingConfig writes a loopback server config whose one session
// records into a memory store.
func writeRecordingConfig(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "server.toml")
	body := `
[server]
host = "127.0.0.1"
port = 0

[auth]
mode = "dev_token"

[recording]
enabled_by_default = true
store_type = "memory"

[[sessions]]
session_id = "s-telnet"
connector_type = "telnet"
auto_start = false
host = "127.0.0.1"
port = 2323
`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// The whole path, through a real server: a started session attaches to the
// hub, records into the server's store, and the recording routes read it back
// — flushed first, as the reference flushes the runtime before reading.
func TestHostedSessionRecordingIsReadableOverHTTP(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	bundle, err := buildServer(ctx, writeRecordingConfig(t), "", 0, "")
	if err != nil {
		t.Fatalf("buildServer: %v", err)
	}
	defer func() { _ = bundle.engine.Close(context.Background()) }()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = bundle.srv.Serve(ctx, ln) }()
	base := "http://" + ln.Addr().String()

	// The session is not auto_start, so nothing else starts it; the fake
	// connector is still swapped in under the lock the registry reads it with.
	reg := bundle.registry
	reg.mu.Lock()
	reg.connect = func(context.Context, serverconfig.SessionDefinition) (connectors.Connector, error) {
		return newFakeConnector(), nil
	}
	reg.mu.Unlock()
	token := ""
	if tok := bundle.hub.WorkerToken(); tok != nil {
		token = *tok
	}
	reg.SetHubLink(ctx, bundle.hub, base, token)
	if _, err := reg.StartSession(ctx, "s-telnet"); err != nil {
		t.Fatal(err)
	}
	defer func() { _, _ = reg.StopSession(context.Background(), "s-telnet") }()
	waitFor(t, "the worker to attach", func() bool {
		return bundle.hub.HasWorkerSocket("s-telnet") && bundle.hub.HasWorkerHello("s-telnet")
	})

	get := func(path string, out any) {
		t.Helper()
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+path, nil)
		req.Header.Set("Authorization", "Bearer "+bundle.devToken)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = resp.Body.Close() }()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("%s: status %d", path, resp.StatusCode)
		}
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			t.Fatal(err)
		}
	}
	entriesAt := func(query string) []map[string]any {
		var out []map[string]any
		get("/api/sessions/s-telnet/recording/entries"+query, &out)
		return out
	}
	// The default flush interval is 5s and the batch 100: only the flush the
	// route performs can make runtime_started visible this soon.
	started := entriesAt("?event=runtime_started")
	if len(started) != 1 || !reflect.DeepEqual(started[0]["data"], map[string]any{"session_id": "s-telnet"}) {
		t.Fatalf("runtime_started over HTTP = %v", started)
	}
	// The hub asks a newly attached worker for a snapshot; that snapshot is
	// the recording's first "read".
	waitFor(t, "the attach snapshot to be recorded", func() bool { return len(entriesAt("?event=read")) == 1 })
	if all := entriesAt("?offset=0&limit=1"); len(all) != 1 || all[0]["event"] != "log_start" {
		t.Fatalf("offset/limit over HTTP = %v", all)
	}
	var meta map[string]any
	get("/api/sessions/s-telnet/recording", &meta)
	if meta["enabled"] != true || meta["exists"] != true || meta["session_id"] != "s-telnet" {
		t.Fatalf("recording meta over HTTP = %v", meta)
	}
}

// --- annotation -------------------------------------------------------------

// The corpus's annotated script, recorded by a runtime given the reference's
// PatternDetector: a send split mid-word, styled streamed output, a match
// split across frames, an empty frame, a screen matching several categories,
// and input masked at a prompt yet still annotated.
func TestAnnotationMatchesTheReferenceCorpus(t *testing.T) {
	g := loadSessionRecordingGolden(t)
	store := recording.NewInMemoryStore()
	rec := newSessionRecorder(g.SessionID, store, goldenConfig(t, nil), annotation.NewPatternDetector(nil), nil)
	rec.setEnabled(true)
	compareRecorded(t, "annotated", replayScript(t, g, g.AnnotatedScript, rec, store), g.Recorded["annotated"])
}

// annotatedRecorder is a recording recorder with the reference's detector.
func annotatedRecorder(t *testing.T) (*sessionRecorder, *recording.InMemoryStore) {
	t.Helper()
	store := recording.NewInMemoryStore()
	rec := newSessionRecorder("s1", store, testRecordingConfig(), annotation.NewPatternDetector(nil), nil)
	rec.setEnabled(true)
	return rec, store
}

func descriptions(t *testing.T, store recording.Store) []string {
	t.Helper()
	var out []string
	for _, e := range entries(t, store, "annotation") {
		out = append(out, e["data"].(map[string]any)["description"].(string))
	}
	return out
}

const testAWSKey = "AKIA0123456789AB" // pragma: allowlist secret

// Mirrors tests/server/test_output_annotation.py: streamed output is scanned
// through its escape sequences...
func TestStreamedOutputIsScannedThroughItsEscapeSequences(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, map[string]any{"type": "term", "data": "\x1b[12;5H\x1b[38;2;255;176;0m" + testAWSKey + "\x1b[0m"})
	rec.AttemptEnded(nil)
	if got := descriptions(t, store); !reflect.DeepEqual(got, []string{"AWS access key detected in read"}) {
		t.Fatalf("annotations = %v", got)
	}
}

// ...a match split across frames is found once...
func TestAMatchSplitAcrossFramesIsFoundOnce(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, map[string]any{"type": "term", "data": "DROP TA"})
	sendFrame(t, rec, map[string]any{"type": "term", "data": "BLE callers;"})
	rec.AttemptEnded(nil)
	if got := descriptions(t, store); !reflect.DeepEqual(got, []string{"SQL DROP statement detected: DROP TABLE"}) {
		t.Fatalf("annotations = %v", got)
	}
}

// ...and nothing is scanned when nothing is recorded: annotations are
// recording entries, with nowhere to go otherwise.
func TestNothingIsScannedWhenNothingIsRecorded(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.setEnabled(false)
	rec.AttemptStarted()
	sendFrame(t, rec, map[string]any{"type": "term", "data": "DROP TA"})
	rec.setEnabled(true)
	rec.AttemptEnded(nil)
	rec.AttemptStarted()
	// Had the unrecorded half been carried, this would complete it.
	sendFrame(t, rec, map[string]any{"type": "term", "data": "BLE callers;"})
	rec.AttemptEnded(nil)
	if got := descriptions(t, store); len(got) != 0 {
		t.Fatalf("unrecorded output was scanned: %v", got)
	}
}

// The streams and the sequence last the runtime's life, as the reference keeps
// them on HostedSessionRuntime: a partial match carried from one recording
// completes in the next, once per direction, and spans carry on from the
// sequence the first recording reached.
func TestAnnotationStateOutlivesARecording(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, snapshotFrame("$ "))
	must(t, rec.InputReceived("DROP TA"))
	sendFrame(t, rec, map[string]any{"type": "term", "data": "DROP TA"})
	rec.AttemptEnded(nil)

	rec.AttemptStarted()
	must(t, rec.InputReceived("BLE x;"))
	sendFrame(t, rec, map[string]any{"type": "term", "data": "BLE x;"})
	rec.AttemptEnded(nil)

	anns := entries(t, store, "annotation")
	if len(anns) != 2 {
		t.Fatalf("want one DROP TABLE per direction, got %v", anns)
	}
	for _, a := range anns {
		span := a["data"].(map[string]any)["span"].(map[string]any)
		if span["from_seq"] != float64(3) || span["to_seq"] != float64(3) {
			t.Fatalf("span = %v, want seq 3 carried over the restart", span)
		}
	}
}

// A registry's sessions annotate with the detector it was given.
func TestRegistryRecordersShareTheDetector(t *testing.T) {
	r := newTestRegistry(t)
	det := annotation.NewPatternDetector(nil)
	r.SetRecording(recording.NewInMemoryStore(), det)
	r.mu.Lock()
	defer r.mu.Unlock()
	rec := r.recorderFor(r.entries["provide-shell"])
	if rec.detector != det || rec.sendStream == nil || rec.readStream == nil || rec.sendStream == rec.readStream {
		t.Fatal("a session's recorder uses the registry's detector with a stream per direction")
	}
}

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

// A recording write that fails is reported, as the reference lets the
// logger's error out of annotate_session.
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

// A recording write that fails is handed back to the bridge, which ends the
// connection on it; the attempt's end then tries to record runtime_error and
// closes the recording either way. Every observed point reports it.
func TestRecordingWriteFailuresAreReturned(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.FlushBatchSize = 1
	cfg.ControlChannelMode = "wire"
	rec := newSessionRecorder("s1", failingWriteStore{}, cfg, annotation.NewPatternDetector(nil), nil)
	rec.setEnabled(true)
	rec.AttemptStarted()
	steps := map[string]func() error{
		"Connected":       rec.Connected,
		"FrameSent":       func() error { return rec.FrameSent("x", snapshotFrame("$ ")) },
		"WireReceived":    func() error { return rec.WireReceived("x") },
		"ControlReceived": func() error { return rec.ControlReceived(map[string]any{"type": "snapshot_req"}) },
		"InputReceived":   func() error { return rec.InputReceived("x") },
		"flush":           rec.flush,
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

// Each later write in one observed step is skipped once one fails, and the
// sequence is not advanced for a write that did not happen — the first
// raising call ends the reference's method.
func TestAFailedWriteStopsTheStep(t *testing.T) {
	for name, cfg := range map[string]func(*serverconfig.RecordingConfig){
		"snapshot": func(*serverconfig.RecordingConfig) {},
		"wire":     func(c *serverconfig.RecordingConfig) { c.ControlChannelMode = "wire" },
	} {
		t.Run(name, func(t *testing.T) {
			c := testRecordingConfig()
			c.FlushBatchSize = 1
			cfg(&c)
			rec := newSessionRecorder("s1", failingWriteStore{}, c, annotation.NewPatternDetector(nil), nil)
			rec.setEnabled(true)
			rec.AttemptStarted()
			defer rec.AttemptEnded(nil)
			if rec.FrameSent("x", snapshotFrame("DROP TABLE x;")) == nil {
				t.Fatal("no error")
			}
			if rec.InputReceived("DROP TABLE y;") == nil {
				t.Fatal("no error")
			}
			if err := rec.FrameSent("x", map[string]any{"type": "term", "data": "DROP TABLE z;"}); err == nil && name == "wire" {
				t.Fatal("no error")
			}
			rec.mu.Lock()
			defer rec.mu.Unlock()
			if rec.eventSeq != 0 {
				t.Fatalf("eventSeq = %d after writes that failed", rec.eventSeq)
			}
		})
	}
}

// A store that takes the read but not the annotation still ends the step.
func TestAFailedAnnotationWriteIsReturned(t *testing.T) {
	store := &failAfter{InMemoryStore: recording.NewInMemoryStore(), ok: 1}
	cfg := testRecordingConfig()
	cfg.FlushBatchSize = 1
	rec := newSessionRecorder("s1", store, cfg, annotation.NewPatternDetector(nil), nil)
	rec.setEnabled(true)
	rec.AttemptStarted()
	defer rec.AttemptEnded(nil)
	if rec.InputReceived("sudo -i\r") == nil {
		t.Fatal("a failed annotation write was not returned")
	}
}

// failAfter accepts ok appends, then fails every one after.
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

// must fails the test on an unexpected recording error.
func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
