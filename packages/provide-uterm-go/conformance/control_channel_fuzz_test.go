//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Go-side replay of the cross-language differential fuzz corpus.
//
// The corpus lives at the monorepo root (conformance/fuzz/control_channel_fuzz.json),
// not inside this package, because all four ports replay the same bytes. Its
// contract is documented in conformance/fuzz/README.md; this file implements
// that contract for Go and nothing else. In particular it does NOT interpret
// the format — every rule it relies on (base64 of UTF-8 for codec strings, no
// floats in the asserted families, both decode drives recorded separately) is
// stated in that README.
//
// The one thing worth repeating here: every `decode` case records two drives of
// the same byte stream — `chunked` (fed in the recorded chunk boundaries) and
// `single` (fed whole) — and they are NOT required to equal each other. 38 of
// the 192 generated cases differ, because the decoder flushes buffered terminal
// data before it has resolved a trailing DLE. Replaying only one drive proves
// Go parses the same and says nothing about whether it buffers the same, which
// is exactly where a live worker/hub desync would live. Both are driven below.
package conformance

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// fuzzSchema is the only corpus format this replayer understands. A future
// incompatible format bumps the trailing integer; refusing here is deliberate,
// because silently skipping unknown cases is how a port stops proving anything.
const fuzzSchema = "provide-uterm/control-channel-fuzz/1"

// fuzzExpectedCounts pins how many cases each family must yield. A replay that
// silently asserted zero cases would pass, so the counts are checked against
// the corpus's own `counts` block AND against the number of cases each test
// actually asserted.
var fuzzExpectedCounts = map[string]int{
	"encode_data":            96,
	"encode_control":         96,
	"is_control_frame":       128,
	"decode":                 192,
	"regressions":            5,
	"serializer_divergences": 6,
}

// fuzzIDPrefixes guards against a family being replayed with another family's
// cases (which would still "pass" a count check).
var fuzzIDPrefixes = map[string]string{
	"encode_data":            "CCF-ED-",
	"encode_control":         "CCF-EC-",
	"is_control_frame":       "CCF-PR-",
	"decode":                 "CCF-DC-",
	"regressions":            "CCF-REG-",
	"serializer_divergences": "CCF-SD-",
}

type fuzzEvent struct {
	Kind    string         `json:"kind"`
	DataB64 string         `json:"data_b64"`
	Control map[string]any `json:"control"`
}

type fuzzDrive struct {
	Events  []fuzzEvent `json:"events"`
	Error   *string     `json:"error"`
	OnError []string    `json:"on_error"`
}

type fuzzDecodeCase struct {
	ID        string    `json:"id"`
	ChunksB64 []string  `json:"chunks_b64"`
	Finish    bool      `json:"finish"`
	Chunked   fuzzDrive `json:"chunked"`
	Single    fuzzDrive `json:"single"`
	Note      string    `json:"note"`
}

type fuzzCorpus struct {
	Schema    string `json:"schema"`
	Generator string `json:"generator"`
	Seed      int    `json:"seed"`
	Limits    struct {
		HeaderBytes            int `json:"header_bytes"`
		MaxControlPayloadBytes int `json:"max_control_payload_bytes"`
		MaxFrameDepth          int `json:"max_frame_depth"`
	} `json:"limits"`
	Counts     map[string]int `json:"counts"`
	EncodeData []struct {
		ID     string `json:"id"`
		InB64  string `json:"in_b64"`
		OutB64 string `json:"out_b64"`
	} `json:"encode_data"`
	EncodeControl []struct {
		ID      string         `json:"id"`
		Payload map[string]any `json:"payload"`
		OutB64  string         `json:"out_b64"`
	} `json:"encode_control"`
	IsControlFrame []struct {
		ID    string `json:"id"`
		InB64 string `json:"in_b64"`
		Out   bool   `json:"out"`
	} `json:"is_control_frame"`
	Decode                []fuzzDecodeCase `json:"decode"`
	Regressions           []fuzzDecodeCase `json:"regressions"`
	SerializerDivergences []struct {
		ID   string `json:"id"`
		Note string `json:"note"`
		// Kept raw on purpose. The rest of the corpus is decoded with
		// UseNumber, which preserves a number's literal source text — correct
		// for the asserted families (rule 3: integers only) but fatal here,
		// because it would hand Go back CPython's own "0.0" spelling and the
		// float divergence this family exists to pin would vanish. These
		// payloads are decoded natively at use, so 0.0 really is a float64.
		Payload       json.RawMessage `json:"payload"`
		CPythonOutB64 string          `json:"cpython_out_b64"`
	} `json:"serializer_divergences"`
}

// fuzzCorpusPath walks up from the test's working directory (which `go test`
// always sets to the package directory, whatever directory the command was
// invoked from) until it finds the root-level corpus.
//
// UTERM_FUZZ_CORPUS overrides the location. It exists so a deliberately
// corrupted scratch copy can be replayed to prove these assertions actually
// bite, without ever touching the committed corpus. It cannot weaken the suite:
// the envelope, per-family counts and id prefixes are all pinned in this file,
// so a stub or truncated corpus fails rather than silently asserting nothing.
func fuzzCorpusPath(t *testing.T) string {
	t.Helper()
	if override := os.Getenv("UTERM_FUZZ_CORPUS"); override != "" {
		return override
	}
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	rel := filepath.Join("conformance", "fuzz", "control_channel_fuzz.json")
	for i := 0; i < 8; i++ {
		candidate := filepath.Join(dir, rel)
		if _, statErr := os.Stat(candidate); statErr == nil {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	t.Fatalf("could not locate %s above the package directory", rel)
	return ""
}

// loadFuzzCorpus reads and validates the corpus envelope. Numbers are decoded
// with UseNumber so an integer payload value survives as its literal source
// text; re-marshalling a float64 would turn 3386471012 into 3.386471012e+09
// and break encode_control byte-identity for reasons that have nothing to do
// with the codec.
func loadFuzzCorpus(t *testing.T) fuzzCorpus {
	t.Helper()
	path := fuzzCorpusPath(t)
	raw, err := os.ReadFile(path) //nolint:gosec // path resolved from the repo tree
	if err != nil {
		t.Fatalf("read corpus: %v", err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var corpus fuzzCorpus
	if err := dec.Decode(&corpus); err != nil {
		t.Fatalf("corpus invalid: %v", err)
	}
	if corpus.Schema != fuzzSchema {
		t.Fatalf("unrecognised corpus schema %q (this replayer implements %q) — refusing to run", corpus.Schema, fuzzSchema)
	}
	return corpus
}

// fuzzB64 recovers a codec string from a *_b64 field (base64 of the UTF-8
// encoding of the string).
func fuzzB64(t *testing.T, id, field, value string) string {
	t.Helper()
	b, err := base64.StdEncoding.DecodeString(value)
	if err != nil {
		t.Fatalf("case %s: bad base64 in %s: %v", id, field, err)
	}
	return string(b)
}

// checkFuzzFamily asserts a family produced exactly the number of cases the
// replay expects, and that its ids carry the family's prefix. Returns the count
// so each test can report what it actually asserted.
func checkFuzzFamily(t *testing.T, corpus fuzzCorpus, family string, ids []string) int {
	t.Helper()
	want := fuzzExpectedCounts[family]
	if declared, ok := corpus.Counts[family]; !ok || declared != want {
		t.Fatalf("family %s: corpus declares counts=%d (present=%v), replay expects %d", family, declared, ok, want)
	}
	if len(ids) != want {
		t.Fatalf("family %s: corpus yielded %d cases, replay expects %d", family, len(ids), want)
	}
	prefix := fuzzIDPrefixes[family]
	for _, id := range ids {
		if !strings.HasPrefix(id, prefix) {
			t.Fatalf("family %s: case id %q does not start with %q", family, id, prefix)
		}
	}
	return len(ids)
}

// TestFuzzCorpusEnvelope proves the file this port replays is the file the
// contract describes, before a single case is driven through the codec.
func TestFuzzCorpusEnvelope(t *testing.T) {
	corpus := loadFuzzCorpus(t)
	if corpus.Generator != "conformance/fuzz/gen_control_channel_fuzz.py" {
		t.Fatalf("unexpected generator %q", corpus.Generator)
	}
	if corpus.Seed == 0 {
		t.Fatal("corpus declares no seed")
	}
	// The Go port's own controlchannel constants. If the corpus were generated
	// against different limits its recorded rejections would not be replayable.
	if corpus.Limits.HeaderBytes != 11 {
		t.Fatalf("header_bytes=%d, Go uses 11", corpus.Limits.HeaderBytes)
	}
	if corpus.Limits.MaxControlPayloadBytes != 1_048_576 {
		t.Fatalf("max_control_payload_bytes=%d, Go uses 1048576", corpus.Limits.MaxControlPayloadBytes)
	}
	if corpus.Limits.MaxFrameDepth != 32 {
		t.Fatalf("max_frame_depth=%d, Go uses 32", corpus.Limits.MaxFrameDepth)
	}
	total := 0
	for family, want := range fuzzExpectedCounts {
		declared, ok := corpus.Counts[family]
		if !ok {
			t.Fatalf("corpus declares no count for family %s", family)
		}
		if declared != want {
			t.Fatalf("family %s: corpus counts=%d, replay expects %d", family, declared, want)
		}
		total += declared
	}
	if len(corpus.Counts) != len(fuzzExpectedCounts) {
		t.Fatalf("corpus declares %d families, replay knows %d", len(corpus.Counts), len(fuzzExpectedCounts))
	}
	t.Logf("corpus %s seed=%d, %d cases across %d families", corpus.Schema, corpus.Seed, total, len(corpus.Counts))
}

// TestFuzzEncodeData replays encode_data: EncodeTerminalData(in) == out.
func TestFuzzEncodeData(t *testing.T) {
	corpus := loadFuzzCorpus(t)
	ids := make([]string, 0, len(corpus.EncodeData))
	for _, c := range corpus.EncodeData {
		ids = append(ids, c.ID)
		in := fuzzB64(t, c.ID, "in_b64", c.InB64)
		want := fuzzB64(t, c.ID, "out_b64", c.OutB64)
		if got := controlchannel.EncodeTerminalData(in); got != want {
			t.Fatalf("case %s: EncodeTerminalData mismatch\n  go: %q\n  py: %q", c.ID, got, want)
		}
	}
	t.Logf("asserted %d encode_data cases", checkFuzzFamily(t, corpus, "encode_data", ids))
}

// TestFuzzEncodeControl replays encode_control: EncodeControlFrame(payload)
// must be byte-identical to CPython's. Byte-identity is assertable here (unlike
// in the older vectors.json suite) because the corpus emits object keys in
// ascending byte order — Go sorts map keys, the other three preserve insertion
// order, and ascending keys make both rules agree.
func TestFuzzEncodeControl(t *testing.T) {
	corpus := loadFuzzCorpus(t)
	ids := make([]string, 0, len(corpus.EncodeControl))
	for _, c := range corpus.EncodeControl {
		ids = append(ids, c.ID)
		want := fuzzB64(t, c.ID, "out_b64", c.OutB64)
		got, err := controlchannel.EncodeControlFrame(c.Payload)
		if err != nil {
			t.Fatalf("case %s: EncodeControlFrame: %v", c.ID, err)
		}
		if got != want {
			t.Fatalf("case %s: EncodeControlFrame mismatch\n  go: %q\n  py: %q", c.ID, got, want)
		}
	}
	t.Logf("asserted %d encode_control cases", checkFuzzFamily(t, corpus, "encode_control", ids))
}

// TestFuzzIsControlFrame replays the structural predicate.
func TestFuzzIsControlFrame(t *testing.T) {
	corpus := loadFuzzCorpus(t)
	ids := make([]string, 0, len(corpus.IsControlFrame))
	trueCases := 0
	for _, c := range corpus.IsControlFrame {
		ids = append(ids, c.ID)
		if c.Out {
			trueCases++
		}
		in := fuzzB64(t, c.ID, "in_b64", c.InB64)
		if got := controlchannel.IsControlFrame(in); got != c.Out {
			t.Fatalf("case %s: IsControlFrame(%q) = %v, recorded %v", c.ID, in, got, c.Out)
		}
	}
	n := checkFuzzFamily(t, corpus, "is_control_frame", ids)
	t.Logf("asserted %d is_control_frame cases (%d recorded true)", n, trueCases)
}

// driveDecoder replays one drive exactly as conformance/fuzz/README.md tells a
// port to: fresh decoder with an error hook, feed each chunk in order, call
// Finish when the case says so, stop at the first protocol error keeping only
// the events emitted before it.
func driveDecoder(t *testing.T, chunks []string, finish bool) fuzzDrive {
	t.Helper()
	onError := []string{}
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{
		OnError: func(code string) { onError = append(onError, code) },
	})
	drive := fuzzDrive{Events: []fuzzEvent{}, OnError: onError}
	record := func(chunks []controlchannel.Chunk) {
		for _, c := range chunks {
			switch v := c.(type) {
			case controlchannel.DataChunk:
				drive.Events = append(drive.Events, fuzzEvent{
					Kind:    "data",
					DataB64: base64.StdEncoding.EncodeToString([]byte(v.Data)),
				})
			case controlchannel.ControlChunk:
				drive.Events = append(drive.Events, fuzzEvent{Kind: "control", Control: v.Control})
			default:
				t.Fatalf("decoder emitted an unknown chunk type %T", c)
			}
		}
	}
	fail := func(err error) fuzzDrive {
		msg := err.Error()
		drive.Error = &msg
		drive.OnError = onError
		return drive
	}
	for _, chunk := range chunks {
		events, err := dec.Feed(chunk)
		if err != nil {
			return fail(err)
		}
		record(events)
	}
	if finish {
		events, err := dec.Finish()
		if err != nil {
			return fail(err)
		}
		record(events)
	}
	drive.OnError = onError
	return drive
}

// compareDrive asserts one replayed drive against its own recording. Note the
// two drives of a case are compared independently — they are not required to
// equal each other.
func compareDrive(t *testing.T, id, name string, got, want fuzzDrive) {
	t.Helper()
	gotErr, wantErr := "<none>", "<none>"
	if got.Error != nil {
		gotErr = *got.Error
	}
	if want.Error != nil {
		wantErr = *want.Error
	}
	if gotErr != wantErr {
		t.Fatalf("case %s (%s): error mismatch\n  go: %s\n  py: %s", id, name, gotErr, wantErr)
	}
	wantHook := want.OnError
	if wantHook == nil {
		wantHook = []string{}
	}
	if strings.Join(got.OnError, ",") != strings.Join(wantHook, ",") {
		t.Fatalf("case %s (%s): on_error mismatch\n  go: %v\n  py: %v", id, name, got.OnError, wantHook)
	}
	if len(got.Events) != len(want.Events) {
		t.Fatalf("case %s (%s): emitted %d events, recorded %d\n  go: %s\n  py: %s",
			id, name, len(got.Events), len(want.Events), describeEvents(got.Events), describeEvents(want.Events))
	}
	for i := range got.Events {
		g, w := got.Events[i], want.Events[i]
		if g.Kind != w.Kind {
			t.Fatalf("case %s (%s): event %d kind go=%s py=%s", id, name, i, g.Kind, w.Kind)
		}
		if g.Kind == "data" {
			if g.DataB64 != w.DataB64 {
				t.Fatalf("case %s (%s): event %d data mismatch\n  go: %q\n  py: %q",
					id, name, i, fuzzB64(t, id, "data_b64", g.DataB64), fuzzB64(t, id, "data_b64", w.DataB64))
			}
			continue
		}
		// Control payloads are compared structurally (deep equality of the
		// parsed value), never by re-serializing — that is what the corpus's
		// "no floats" rule buys every port.
		if !jsonEqual(g.Control, w.Control) {
			t.Fatalf("case %s (%s): event %d control mismatch\n  go: %#v\n  py: %#v", id, name, i, g.Control, w.Control)
		}
	}
}

func describeEvents(events []fuzzEvent) string {
	parts := make([]string, 0, len(events))
	for _, e := range events {
		if e.Kind == "data" {
			parts = append(parts, "data:"+e.DataB64)
			continue
		}
		raw, _ := json.Marshal(e.Control)
		parts = append(parts, "control:"+string(raw))
	}
	return "[" + strings.Join(parts, " ") + "]"
}

// replayDecodeCases drives every case both ways and returns how many drives
// were asserted.
func replayDecodeCases(t *testing.T, cases []fuzzDecodeCase) (ids []string, drives int) {
	t.Helper()
	for _, c := range cases {
		ids = append(ids, c.ID)
		chunks := make([]string, 0, len(c.ChunksB64))
		for _, encoded := range c.ChunksB64 {
			chunks = append(chunks, fuzzB64(t, c.ID, "chunks_b64", encoded))
		}
		compareDrive(t, c.ID, "chunked", driveDecoder(t, chunks, c.Finish), c.Chunked)
		// The single drive feeds exactly one chunk even when the concatenation
		// is empty — one case has an empty chunks_b64 and its single drive is
		// feed(""), which must behave the same as feeding nothing.
		compareDrive(t, c.ID, "single", driveDecoder(t, []string{strings.Join(chunks, "")}, c.Finish), c.Single)
		drives += 2
	}
	return ids, drives
}

// TestFuzzDecode is the family that matters: the only stateful surface, driven
// both chunked and whole.
func TestFuzzDecode(t *testing.T) {
	corpus := loadFuzzCorpus(t)
	ids, drives := replayDecodeCases(t, corpus.Decode)
	n := checkFuzzFamily(t, corpus, "decode", ids)
	// Guard the guard: if the two drives never diverged, either the corpus
	// stopped recording the buffering behaviour or this replay collapsed them.
	divergent := 0
	for _, c := range corpus.Decode {
		if describeEvents(c.Chunked.Events) != describeEvents(c.Single.Events) {
			divergent++
		}
	}
	if divergent == 0 {
		t.Fatal("no decode case distinguishes the chunked and single drives — the buffering contract is untested")
	}
	t.Logf("asserted %d decode cases (%d drives; %d cases where chunked != single)", n, drives, divergent)
}

// TestFuzzRegressions replays the permanently-numbered hand-written cases.
func TestFuzzRegressions(t *testing.T) {
	corpus := loadFuzzCorpus(t)
	ids, drives := replayDecodeCases(t, corpus.Regressions)
	for _, c := range corpus.Regressions {
		if strings.TrimSpace(c.Note) == "" {
			t.Fatalf("case %s: regression carries no note", c.ID)
		}
	}
	n := checkFuzzFamily(t, corpus, "regressions", ids)
	t.Logf("asserted %d regression cases (%d drives)", n, drives)
}

// goSerializerDivergenceOutputs pins THIS PORT's own encode_control output for
// the inputs where the four runtimes' JSON serializers legitimately disagree.
//
// The corpus records CPython's bytes in cpython_out_b64 and explicitly does NOT
// ask any port to match them; see "serializer_divergences — recorded, not
// asserted" in conformance/fuzz/README.md. Asserting equality with CPython here
// would be asserting a difference away. Instead Go's bytes are pinned below, so
// a change to Go's serializer shows up as a diff in this file, and the test
// logs where Go and CPython part company.
//
// Go's differences from CPython, all expected:
// Go's differences from CPython, all expected and all matching that README's
// table:
//   - CCF-SD-0001: Go writes {"k0":0}; CPython keeps int and float apart and
//     writes {"k0":0.0} (the declared length differs too: 0x08 vs 0x0a).
//   - CCF-SD-0002: Go writes [1,1.5,2]; CPython writes [1.0,1.5,2].
//   - CCF-SD-0003: Go escapes U+2028/U+2029 as \u2028\u2029; CPython emits raw.
//
// CCF-SD-0004 (U+007F), 0005 (U+001F) and 0006 (astral U+1D11E) are cases where
// .NET is the odd one out — Go agrees with CPython byte-for-byte, which is why
// they are pinned to the same bytes rather than left out.
var goSerializerDivergenceOutputs = map[string]string{
	"CCF-SD-0001": "EAIwMDAwMDAwODp7ImswIjowfQ==",
	"CCF-SD-0002": "EAIwMDAwMDAxMDp7ImswIjpbMSwxLjUsMl19",
	"CCF-SD-0003": "EAIwMDAwMDAxNTp7ImswIjoiXHUyMDI4XHUyMDI5In0=",
	"CCF-SD-0004": "EAIwMDAwMDAwYTp7ImswIjoifyJ9",
	"CCF-SD-0005": "EAIwMDAwMDAwZjp7ImswIjoiXHUwMDFmIn0=",
	"CCF-SD-0006": "EAIwMDAwMDAwZDp7ImswIjoi8J2EniJ9",
}

// TestFuzzSerializerDivergences pins Go's own bytes and reports, rather than
// asserts, the difference from CPython.
func TestFuzzSerializerDivergences(t *testing.T) {
	corpus := loadFuzzCorpus(t)
	ids := make([]string, 0, len(corpus.SerializerDivergences))
	differ := 0
	for _, c := range corpus.SerializerDivergences {
		ids = append(ids, c.ID)
		pinned, ok := goSerializerDivergenceOutputs[c.ID]
		if !ok {
			t.Fatalf("case %s: no pinned Go output — a new serializer divergence needs one", c.ID)
		}
		// Decoded natively (no UseNumber): 0.0 becomes a Go float64, which is
		// what a Go caller would actually hand the encoder.
		var payload map[string]any
		if err := json.Unmarshal(c.Payload, &payload); err != nil {
			t.Fatalf("case %s: payload invalid: %v", c.ID, err)
		}
		got, err := controlchannel.EncodeControlFrame(payload)
		if err != nil {
			t.Fatalf("case %s: EncodeControlFrame: %v", c.ID, err)
		}
		want := fuzzB64(t, c.ID, "pinned", pinned)
		if got != want {
			t.Fatalf("case %s: Go's serializer moved\n  now:    %q\n  pinned: %q\n  (%s)", c.ID, got, want, c.Note)
		}
		cpython := fuzzB64(t, c.ID, "cpython_out_b64", c.CPythonOutB64)
		if got != cpython {
			differ++
			t.Logf("case %s: Go %q != CPython %q — recorded, not asserted (%s)", c.ID, got, cpython, c.Note)
		}
	}
	if len(goSerializerDivergenceOutputs) != len(ids) {
		t.Fatalf("pinned %d Go outputs for %d divergence cases", len(goSerializerDivergenceOutputs), len(ids))
	}
	n := checkFuzzFamily(t, corpus, "serializer_divergences", ids)
	t.Logf("pinned %d serializer_divergences cases (%d where Go differs from CPython; none asserted equal)", n, differ)
}
